#!/usr/bin/env bash
# 端到端冒烟测试：在临时 git 仓库里模拟插件钩子 + 技能调用脚本的完整流程。
# 用法：bash tests/smoke.sh        （只需要 python3 和 git，不会碰你的真实仓库和 HOME）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/plugins/light-work/scripts/lw.py"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/lw-smoke.XXXXXX")"
cleanup() { case "$TMP" in */lw-smoke.*) rm -rf "$TMP" ;; esac; }
trap cleanup EXIT

export HOME="$TMP/home"; mkdir -p "$HOME"
REPO="$TMP/repo"; mkdir -p "$REPO"
L="python3 $SCRIPT"
pass=0
ok()   { pass=$((pass + 1)); echo "  ✓ $1"; }
fail() { echo "  ✗ $1"; echo "$2" | sed 's/^/      /'; exit 1; }
expect() { # expect <名称> <输出> <应包含的文本>
  if grep -qF -- "$3" <<<"$2"; then ok "$1"; else fail "$1（缺少：$3）" "$2"; fi
}
hook() { # hook <事件> <json>
  printf '%s' "$2" | python3 "$SCRIPT" hook "$1"
}

cd "$REPO"
git init -q -b main && git config user.email t@t && git config user.name t
mkdir -p src/search
cat > src/search/voice.ts <<'EOF'
export function handleResult(res) {
  if (res === null) return { ok: false, reason: "null" };
  return { ok: true, data: res };
}
export function legacyFallback(x) { return x }
EOF
mkdir -p src/ui
cat > src/ui/page.ts <<'EOF'
import { handleResult } from "../search/voice";
export function onVoice(res) { return handleResult(res); }
EOF
git add -A && git commit -qm init

echo "钩子：Git 工作区判断"
out=$(hook session-start "{\"cwd\":\"$TMP\"}")
[ -z "$out" ] && [ ! -e "$HOME/.light-work/lw.py" ] && ok "非 Git 目录跳过初始化" || fail "非 Git 目录触发" "$out"
out=$(hook session-start "{\"cwd\":\"$REPO\"}")
expect "基准分支读取改动记录摘要" "$out" "## 有效约束"
[ -f .light-work/main.md ] && ok "基准分支自动建改动记录" || fail "缺少基准分支改动记录" ""
out=$($L init); expect "基准分支可直接运行 init" "$out" ".light-work/main.md"
git checkout -q --detach
out=$(hook session-start "{\"cwd\":\"$REPO\"}")
expect "detached HEAD 按当前提交记录" "$out" ".light-work/detached-$(git rev-parse --short HEAD).md"
git checkout -qb feature/voice

echo "钩子：会话开始"
out=$(hook session-start "{\"cwd\":\"$REPO\"}")
expect "注入工作流" "$out" "lw-record"
[ -f .light-work/feature-voice.md ] && ok "自动建改动记录" || fail "自动建改动记录" "$(ls -la .light-work)"
[ -f "$HOME/.light-work/lw.py" ] && ok "安装稳定路径 shim" || fail "shim" ""
git rev-parse -q --verify refs/light-work/start/feature-voice >/dev/null && ok "保存改动记录起点" || fail "改动记录起点" ""

echo "钩子：用户消息 / 编辑前"
out=$(hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"空字符串也别当成功，改一下\"}")
expect "确认需要动手才走记录流程" "$out" "确认需要动手时，先 lw-record"
ledger_before=$(git hash-object .light-work/feature-voice.md)
out=$(hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"为什么要这样改？先分析一下\"}")
expect "提问命中关键词时提示 AI 自行判断" "$out" "纯提问、分析或解释就直接回答，不记条目"
[ "$(git hash-object .light-work/feature-voice.md)" = "$ledger_before" ] && ok "提示本身不写条目" || fail "提问改变改动记录" "$out"
EDIT="{\"cwd\":\"$REPO\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$REPO/src/search/voice.ts\"}}"
out=$(hook pre-edit "$EDIT")
expect "没有 open 条目时提醒" "$out" "还没有 open 条目"
ln -s "$REPO" "$TMP/link"   # macOS 的 /tmp、/var 本身就是符号链接；项目也可能通过链接打开
out=$(hook pre-edit "{\"cwd\":\"$TMP/link\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$TMP/link/src/search/voice.ts\"}}")
expect "通过符号链接路径编辑也能识别" "$out" "还没有 open 条目"

echo "技能：lw-record 写条目"
cat >> .light-work/feature-voice.md <<'EOF'
### C-001 · 2026-11-19 · change · settled · null 返回失败
- 意图: 用户："识别失败要返回失败" → res 为 null 时返回失败；老客户端的 legacyFallback 要保留
- 约束:
  - C-001.1 legacyFallback 保留，不能删除
  - C-001.2 res 为 null 时返回 ok:false reason=null
- 假设: 无
- 文件: src/search/voice.ts
- 提交: abc1234
- 验证: ✓ 手测 null → ok:false
- 级别: L0

### C-002 · 2026-11-20 · bug · open · 空字符串不当成功
- 意图: 用户："没说话也显示成功" → 空字符串结果也返回失败
- 约束:
  - C-002.1 res 为空字符串时返回 ok:false reason=empty
- 假设: 无
- 根因: 待查
- 文件: src/search/voice.ts
- 提交: (待提交)
- 验证: ✗ 未验证
- 过程: R1 用户提出
- 级别: 待定
EOF
out=$($L lint); expect "lint 通过" "$out" "改动记录格式正常"
out=$(hook pre-edit "$EDIT")
expect "编辑前注入旧条目的约束" "$out" "C-001.1"
expect "编辑前注入文件约束" "$out" "C-002.1"
out=$(hook pre-edit "$EDIT"); [ -z "$out" ] && ok "同一约束不重复提示" || fail "重复提示" "$out"
sed -i.bak 's/C-001.1 legacyFallback 保留，不能删除/C-001.1 legacyFallback 继续供旧客户端使用/' .light-work/feature-voice.md
rm .light-work/feature-voice.md.bak
out=$(hook pre-edit "$EDIT"); expect "同编号约束更新后再次提示" "$out" "C-001.1 legacyFallback 继续供旧客户端使用"
out=$(hook pre-edit "$EDIT"); [ -z "$out" ] && ok "更新后的相同约束继续去重" || fail "更新后重复提示" "$out"

echo "技能：lw-edit 改代码 → 钩子：结束前"
first_round_base=$($L snapshot)
python3 - <<'EOF'
p = "src/search/voice.ts"; s = open(p).read()
s = s.replace('  return { ok: true, data: res };',
              '  // 空字符串说明没识别到语音，不能当成功\n  if (res === "") return { ok: false, reason: "empty" };\n  return { ok: true, data: res };')
open(p, "w").write(s)
EOF
hook post-edit "$EDIT" >/dev/null
out=$(hook stop "{\"cwd\":\"$REPO\",\"stop_hook_active\":true}"); [ -z "$out" ] && ok "已有 Stop 续跑时保持安静" || fail "续跑出现提醒" "$out"
$L ask >/dev/null
out=$(hook stop "{\"cwd\":\"$REPO\"}"); [ -z "$out" ] && ok "等待冲突确认时保持安静" || fail "等待确认时出现提醒" "$out"
hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"1\"}" >/dev/null
out=$(hook stop "{\"cwd\":\"$REPO\"}")
expect "用户回复后可收到补做提醒" "$out" "需要时可以让助手补做 lw-review"
python3 -c 'import json, sys; assert set(json.loads(sys.argv[1])) == {"systemMessage"}' "$out" &&
  ok "Stop 只输出可忽略的消息" || fail "Stop 输出了额外控制字段" "$out"
out=$(hook stop "{\"cwd\":\"$REPO\"}"); [ -z "$out" ] && ok "同一份改动只提醒一次" || fail "重复提醒" "$out"

echo "技能：lw-review"
out=$($L score --base "$first_round_base"); expect "score 给出级别" "$out" "级别: L"
expect "一层调用方：本轮没改但调用了改过的函数" "$out" "handleResult → src/ui/page.ts"
expect "score 列出要核对的约束" "$out" "C-001.2"
python3 - <<'EOF'
p = ".light-work/feature-voice.md"; s = open(p).read()
s = s.replace("- 根因: 待查", "- 根因: 只判了 null，空字符串落到成功分支 voice.ts:2")
s = s.replace("- 级别: 待定", "- 级别: L0").replace(
    "- 验证: ✗ 未验证",
    "- 验证: ✓ node 手测 '' → ok:false ｜ ✗ fixture-check → 断言失败 ｜ ✗ 未做真机媒体、支付与上报联调")
open(p, "w").write(s)
EOF
out=$($L lint); expect "定稿后 lint 通过" "$out" "改动记录格式正常"
out=$($L checkpoint --entry C-002 --base "$first_round_base"); expect "收尾点归属条目" "$out" "归属 C-002"
out=$($L context)
expect "会话摘要保留实际失败" "$out" "fixture-check → 断言失败"
grep -qF "未做真机" <<<"$out" && fail "会话摘要带入未执行项目" "$out" || ok "会话摘要跳过未执行项目"
out=$($L check C-002)
expect "检查保留实际执行失败" "$out" "fixture-check → 断言失败"
grep -qF "未做真机" <<<"$out" && fail "检查把未执行当作历史问题" "$out" || ok "检查跳过未执行项目"
out=$(hook stop "{\"cwd\":\"$REPO\"}"); [ -z "$out" ] && ok "收尾后保持安静" || fail "收尾后仍有提醒" "$out"

echo "第二个需求：改写了上一条的代码"
cat >> .light-work/feature-voice.md <<'EOF'

### C-003 · 2026-11-20 · change · open · 失败统一 reason=fail
- 意图: 用户："后端要求 reason 统一成 fail" → 失败时 reason 一律为 fail
- 约束:
  - C-003.1 任何失败返回 ok:false reason=fail
- 假设: 无
- 文件: src/search/voice.ts
- 提交: (待提交)
- 验证: ✗ 未验证
- 过程: R1 用户提出
- 级别: L1
EOF
printf 'export const existingWork = 1;\n' > src/unrelated.ts
round_base=$($L snapshot)
cat > src/search/voice.ts <<'EOF'
export function handleResult(res) {
  if (res === null || res === "") return { ok: false, reason: "fail" };
  return { ok: true, data: res };
}
EOF
out=$($L review --base "$round_base" --current-entry C-003)
expect "删除导出被识别为接口变化" "$out" "删除 src/search/voice.ts"
for command in review check; do
  out=$($L "$command" --base "$round_base" --current-entry C-003 --json)
  python3 -c 'import json, sys; d = json.loads(sys.argv[1]); assert d["range"] == "C-003 的本轮改动" and d["eff_files"] == ["src/search/voice.ts"] and [e["id"] for e in d["entries"]] == ["C-003"]' "$out" &&
    ok "$command 内部调用只看本轮差异和条目" || fail "$command 内部调用混入其他改动" "$out"
done
$L view --base "$round_base" --current-entry C-003 --title "本轮审阅" </dev/null >/dev/null
page=$(cat .light-work/feature-voice.html)
expect "本轮报告注明条目和范围" "$page" "C-003 的本轮改动"
expect "本轮报告保留 Git diff" "$page" 'reason: &quot;fail&quot;'
grep -qF 'src/unrelated.ts' <<<"$page" && fail "本轮报告混入先前改动" "" || ok "本轮报告只包含本轮证据"
$L checkpoint --entry C-003 --base "$round_base" >/dev/null
git rev-list --objects refs/light-work/cp/feature-voice > "$TMP/checkpoint-objects"
grep -q "^$round_base " "$TMP/checkpoint-objects" && ok "收尾点保留本轮起点 tree" || fail "收尾点未引用起点 tree" ""

echo "命令：/lw-check 的确定性部分"
out=$($L conflicts)
expect "发现回退/改写" "$out" "C-003 删除/改写了 C-002"
expect "发现热点" "$out" "handleResult ← C-002 → C-003"
out=$($L check); expect "check 默认最近一条" "$out" "C-003 的 1 轮修改"
expect "check 只显示所选条目的状态" "$out" "未定稿 open: C-003"
out=$($L check all); expect "全部只包含已归属的收尾轮次" "$out" "全部已记录改动（2 条，2 轮）"
expect "全部说明哪些条目缺少收尾点" "$out" "暂无收尾点: C-001"
for command in review check; do
  out=$($L "$command"); expect "$command 默认最近一条" "$out" "C-003 的 1 轮修改"
  out=$($L "$command" C-002); expect "$command 支持指定条目" "$out" "C-002 的 1 轮修改"
  all=$($L "$command" all --json)
  [ "$all" = "$($L "$command" a --json)" ] && ok "$command a 等价于 all" || fail "$command 的 all 别名不一致" "$all"
  grep -qF "src/unrelated.ts" <<<"$all" && fail "$command all 混入未登记改动" "$all" || ok "$command all 排除未登记改动"
  branch=$($L "$command" branch --json)
  [ "$branch" = "$($L "$command" b --json)" ] && ok "$command b 等价于 branch" || fail "$command 的 branch 别名不一致" "$branch"
  expect "$command branch 包含未登记改动" "$branch" "src/unrelated.ts"
  from_subdir=$(cd src/search && $L "$command" branch --json)
  [ "$branch" = "$from_subdir" ] && ok "$command branch 在子目录仍覆盖全仓库" || fail "$command 在子目录缩小了范围" "$from_subdir"
  for invalid_scope in brach 全部 全分支; do
    out=$($L "$command" "$invalid_scope" 2>&1) && fail "$command 接受了无效范围 $invalid_scope" "$out"
    expect "$command 拒绝无效范围 $invalid_scope" "$out" "范围须为 C-xxx、all 或 branch"
  done
done
out=$($L conflicts --recorded-only); expect "全部检查可核对已收尾条目之间的回退" "$out" "C-003 删除/改写了 C-002"
historical=$($L check C-003 --json)
printf 'legacyFallback(1);\nhandleResult("later");\n' > src/later.ts
[ "$historical" = "$($L check C-003 --json)" ] && ok "历史检查不混入工作区的新引用" || fail "历史引用受工作区影响" "$historical"
out=$($L check --base "$round_base" --current-entry C-003)
expect "本轮检查仍包含工作区的新增引用" "$out" "删除了 legacyFallback，但仍有 1 处引用: src/later.ts"
expect "检查输出引用对应的结束快照" "$out" "引用取自所选范围最后的结束快照"
rm src/later.ts
rm src/unrelated.ts

echo "反复修改提醒 / lint 证据规则"
python3 - <<'EOF'
p = ".light-work/feature-voice.md"; s = open(p).read()
s = s.replace("- 过程: R1 用户提出\n- 级别: L1", "- 过程: R1 用户提出；R2 还是不对；R3 再改\n- 级别: L1")
s = s.replace("- 验证: ✗ 未验证\n- 过程: R1 用户提出；R2", "- 验证: ✓ 应该没问题\n- 过程: R1 用户提出；R2")
open(p, "w").write(s)
EOF
out=$(hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"还是不对\"}"); expect "第 3 轮提醒找根因" "$out" "第 3 轮"
out=$($L lint || true); expect "拒绝没有证据的验证" "$out" "不是证据"

echo "提交回填 / 自动定稿 / 页面"
sed -i.bak 's/- 验证: ✓ 应该没问题/- 验证: ✗ 未验证/' .light-work/feature-voice.md && rm -f .light-work/feature-voice.md.bak
git add -A && git commit -qm wip
grep -q "C-003 · 2026-11-20 · change · settled" .light-work/feature-voice.md && ok "提交与收尾快照相符的条目自动转 settled" || fail "自动定稿" "$(grep '^###' .light-work/feature-voice.md)"
grep -q "C-002 · 2026-11-20 · bug · open" .light-work/feature-voice.md && ok "同文件但快照不符的条目保留 open" || fail "提交误定稿" "$(grep '^###' .light-work/feature-voice.md)"
out=$($L lint); expect "定稿后 lint 仍通过" "$out" "改动记录格式正常"
grep -q "提交: [0-9a-f]\{7\}" .light-work/feature-voice.md && ok "post-commit 回填 SHA" || fail "回填" "$(grep 提交 .light-work/feature-voice.md)"
out=$($L why src/search/voice.ts:2); expect "why：行 → 提交 → 条目" "$out" "C-003"
expect "why：带出意图" "$out" "意图:"
out=$($L why src/ui/page.ts:2); expect "why：基准分支上的代码" "$out" "基准分支上就有的代码"
out=$(printf '# 改动报告\n> 两次改动\n' | $L view --title "改动报告"); expect "从 stdin 生成页面" "$out" ".light-work/feature-voice.html"
grep -q "改动报告" .light-work/feature-voice.html && ok "页面包含报告内容" || fail "页面内容" ""
sed -i.bak '1s/改动记录/变更台账/' .light-work/feature-voice.md && rm -f .light-work/feature-voice.md.bak
cat <<'LW_REPORT' | $L view --scope C-002 --title '审阅 <范围>' >/dev/null
## 关键改动

- 核对 `src/search/voice.ts:3` 的空字符串处理
- <script>alert('raw')</script>

| 场景 | 改前 | 改后 | 证据 |
|---|---|---|---|
| 输入为空字符串 | 进入成功分支 | 返回失败 | `src/search/voice.ts:3` |

```flow
查看输入
检查返回结果
```

```flow
检查输入 -> 空结果 | 为空
检查输入 -> 加载详情 | 有效
```

```sequence
页面 -> 搜索服务 | 查询
搜索服务 -> 页面 | 返回 `src/search/voice.ts:3`
```

```architecture
搜索页 -> 搜索服务 | 查询
搜索服务 -> 缓存 | 读取
搜索服务 -> API | 请求
```
LW_REPORT
page=$(cat .light-work/feature-voice.html)
overview=${page#*id=\"record-summary\">}
overview=${overview%%</details>*}
expect "报告概览显示实际执行结果" "$overview" "fixture-check → 断言失败"
grep -qF "未做真机" <<<"$overview" && fail "报告概览带入未执行项目" "$overview" || ok "报告概览跳过未执行项目"
expect "旧文件用新标题展示" "$page" "# 改动记录 · feature/voice"
grep -q '^# 变更台账' .light-work/feature-voice.md && ok "旧记录文件保持原样" || fail "写入了旧记录文件" ""
expect "改前改后在网页中成对显示" "$page" "<th>改前</th><th>改后</th>"
expect "指定条目报告显示范围" "$page" 'C-002 的 1 轮修改'
expect "审阅章节导航指向对应标题" "$page" '<a class="nav-section" href="#section-1">关键改动</a>'
expect "审阅章节有独立定位" "$page" '<h2 id="section-1">关键改动</h2>'
expect "证据文件提供离线筛选" "$page" 'id="file-filter" type="search"'
expect "证据正文可返回审阅" "$page" '<a href="#report">返回审阅</a>'
expect "路径行号链接到真实补丁行" "$page" 'href="#file-1-hunk-1-line-3"><code>src/search/voice.ts:3</code>'
expect "跳转目标存在" "$page" 'id="file-1-hunk-1-line-3"'
expect "离线步骤图" "$page" '<ol class="flow">'
expect "离线分支流程图" "$page" '<figure class="diagram flowchart">'
expect "同一来源共享流程图节点" "$page" '<li class="group"><span class="node">检查输入</span><ul>'
expect "离线交互图" "$page" '<figure class="diagram sequence">'
expect "离线架构图" "$page" '<figure class="diagram architecture">'
expect "改动记录原文默认折叠" "$page" '<details class="panel" id="ledger-raw">'
expect "标题和正文中的 HTML 被转义" "$page" '&lt;script&gt;alert'
grep -q '<script src=' <<<"$page" && fail "报告依赖远程脚本" "" || ok "报告无需加载远程脚本"
mkdir -p "src/显示"
printf 'let value = 1;\n' > "src/显示/中文 文件.ts"
printf 'const quotedFile = 1;\n' > 'src/显示/带"引号.ts'
mkdir -p dist
python3 - <<'PY'
from pathlib import Path
Path("dist/bundle.js").write_text("generatedOnly = true;\n" * 10000)
Path("src/long.ts").write_text("".join(f"const evidence{i:03d} = {i};\n" for i in range(1, 101)))
PY
printf '%s\n' '核对 `src/long.ts:90` 和 `src/显示/带"引号.ts:1`。' | $L view --scope branch >/dev/null
page=$(cat .light-work/feature-voice.html)
expect "全分支报告导航显示 Unicode 文件" "$page" 'src/显示/中文 文件.ts'
expect "带空格的 Unicode 文件有真实补丁" "$page" '+let value = 1;'
expect "带引号的文件有真实补丁" "$page" '+const quotedFile = 1;'
expect "被引用的后段保留准确的行锚点" "$page" '-line-90"><code>src/long.ts:90</code>'
expect "长补丁标明省略内容" "$page" '省略后续'
expect "生成产物保留增删统计" "$page" '+10000 / −0'
grep -qF 'generatedOnly' <<<"$page" && fail "报告嵌入生成产物正文" "" || ok "生成产物仅保留统计"
$L view --scope all </dev/null >/dev/null
page=$(cat .light-work/feature-voice.html)
expect "全部改动记录报告保留各轮差异" "$page" '全部已记录改动（2 条，2 轮）'
grep -qF 'src/显示/中文 文件.ts' <<<"$page" && fail "全部改动记录报告混入未登记文件" "" || ok "全部改动记录报告排除未登记文件"
rm "src/显示/中文 文件.ts" 'src/显示/带"引号.ts' "src/long.ts" "dist/bundle.js"
[ "$(find .light-work -type d | wc -l | tr -d ' ')" = "1" ] && ok ".light-work 下没有子目录" || fail "出现子目录" "$(find .light-work)"
git status --porcelain | grep -q ".light-work" && fail ".light-work 不应出现在 git status" "$(git status --porcelain)" || ok ".light-work 已被本地 exclude"

echo "切换分支：自动换成另一份改动记录"
git checkout -qb feature/other
out=$(hook session-start "{\"cwd\":\"$REPO\"}")
[ -f .light-work/feature-other.md ] && ok "新分支自动建自己的改动记录" || fail "新分支改动记录" "$(ls .light-work)"
out=$(hook pre-edit "$EDIT"); expect "新分支不带入旧分支的约束" "$out" "还没有 open 条目"
grep -q "C-001.1" <<<"$out" && fail "旧分支约束泄漏" "$out" || ok "旧分支约束没有泄漏"
git checkout -qb feature/third
hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"改一下\"}" >/dev/null
[ -f .light-work/feature-third.md ] && ok "会话中途切分支也自动建改动记录" || fail "中途切分支" "$(ls .light-work)"
git checkout -q feature/voice
out=$($L context); expect "切回来还是原来的改动记录" "$out" "C-003"

echo "Codex：apply_patch 补丁 / Stop 提醒"
git checkout -qb feature/codex
hook session-start "{\"cwd\":\"$REPO\"}" >/dev/null
cat >> .light-work/feature-codex.md <<'EOF'
### C-001 · 2026-11-20 · change · open · 页面层兜底
- 意图: 用户："页面上别直接把 null 丢给 handleResult" → 页面层先兜底
- 约束:
  - C-001.1 onVoice 收到 undefined 时返回 ok:false reason=none
- 假设: 无
- 文件: src/ui/page.ts
- 提交: (待提交)
- 验证: ✗ 未验证
- 过程: R1 用户提出
- 级别: 待定
EOF
codex_edit() { # codex_edit <事件>：模拟 Codex 的 apply_patch（一个补丁改两个文件，相对路径）
  python3 -c 'import json,sys; print(json.dumps({"cwd": sys.argv[1], "hook_event_name": "PreToolUse", "tool_name": "apply_patch",
    "tool_input": {"input": "*** Begin Patch\n*** Update File: src/search/voice.ts\n@@\n-x\n+y\n*** Update File: src/ui/page.ts\n@@\n-a\n+b\n*** End Patch\n"}}))' "$REPO" \
    | python3 "$SCRIPT" hook "$1"
}
out=$(codex_edit pre-edit); expect "apply_patch 也能注入约束" "$out" "C-001.1"
expect "补丁里的每个文件都解析出来" "$out" "改 src/ui/page.ts 前"
echo 'export function onVoice2(res) { return res ?? { ok: false, reason: "none" }; }' >> src/ui/page.ts
codex_edit post-edit >/dev/null
out=$(hook stop "{\"cwd\":\"$REPO\",\"last_assistant_message\":\"改好了，结果如下。\"}")
expect "apply_patch 改动漏收尾时提示" "$out" '"systemMessage"'
echo '// 再改一点' >> src/ui/page.ts
out=$(hook stop "{\"cwd\":\"$REPO\"}")
expect "出现新改动后可再提醒" "$out" '"systemMessage"'
out=$($L review 2>&1) && fail "缺少收尾点时选了其他差异" "$out"
expect "缺少收尾点时说明单条 diff 暂不可用" "$out" "C-001 还没有收尾点"
out=$($L why src/ui/page.ts:3); expect "why：未提交的行归到 open 条目" "$out" "还没提交的改动"
expect "why：未提交的行找到条目" "$out" "C-001 · change · open"

echo "边界：删除整个文件 / 只改锁文件 / 出错告诉一次"
git add -A && git commit -qm codex
git checkout -qb feature/edge
hook session-start "{\"cwd\":\"$REPO\"}" >/dev/null
echo "lockfileVersion: 9" > pnpm-lock.yaml
out=$($L score); expect "只改锁文件是 L0" "$out" "级别: L0"
grep -qF "改了配置" <<<"$out" && fail "锁文件被当成配置" "$out" || ok "锁文件不算配置"
rm -f pnpm-lock.yaml
git rm -q src/search/voice.ts
out=$($L score); expect "删除整个文件：识别删除导出" "$out" "删除 src/search/voice.ts"
expect "删除整个文件：还有人引用" "$out" "删除了 handleResult"
expect "删除整个文件：片段用旧行号" "$out" "src/search/voice.ts:1-"
git checkout -q HEAD -- src/search/voice.ts
python3 - "$SCRIPT" <<'EOF'
import sys, importlib.util
spec = importlib.util.spec_from_file_location("lw", sys.argv[1]); lw = importlib.util.module_from_spec(spec); spec.loader.exec_module(lw)
lw.note_error("钩子 stop", RuntimeError("boom"))
EOF
out=$(hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"看看这个函数\"}"); expect "钩子出错后下一条消息告诉 AI" "$out" "上次运行出错（"
expect "带上出错的位置" "$out" "钩子 stop: boom"
out=$(hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"看看这个函数\"}"); grep -qF "上次运行出错" <<<"$out" && fail "错误重复提示" "$out" || ok "错误只告诉一次"

echo "提交回填：同文件的两次局部提交"
git checkout -qb feature/partial
hook session-start "{\"cwd\":\"$REPO\"}" >/dev/null
shared='src/shared settings.ts'
printf 'export const label = "old";\nexport const timeout = 5;\n' > "$shared"
git add -- "$shared" && git commit -qm 'add shared settings'
for entry in C-001 C-002; do
  cat >> .light-work/feature-partial.md <<EOF

### $entry · 2026-11-20 · change · open · 设置调整
- 意图: 调整共享配置
- 文件: $shared
- 验证: —
- 级别: L0
EOF
done
first_base=$($L snapshot)
git config core.trustctime false
python3 - "$shared" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
stamp = path.stat().st_mtime_ns
path.write_text(path.read_text().replace('"old"', '"new"'))
# 模拟等长改写，文件时间与索引时间相同，Git 需要重新读文件内容。
os.utime(path, ns=(stamp, stamp))
os.utime('.git/index', ns=(stamp, stamp))
PY
out=$($L checkpoint --entry C-001 --base "$first_base")
expect "快照捕获索引同一时间的等长改写" "$out" "归属 C-001"
out=$(git show ":$shared")
expect "快照保持真实索引原样" "$out" 'label = "old"'
git config --unset core.trustctime
second_base=$($L snapshot)
git add -- "$shared"
printf 'export const label = "new";\nexport const timeout = 8;\n' > "$shared"
$L checkpoint --entry C-002 --base "$second_base" >/dev/null
out=$(git commit -qm 'stage only label' 2>&1)
expect "只回填实际提交的文案条目" "$out" "已关联到 C-001"
grep -q 'C-002 · 2026-11-20 · change · open' .light-work/feature-partial.md && ok "仍未提交的超时条目保持 open" || fail "局部提交误定稿" "$out"
git add -- "$shared"
out=$(git commit -qm 'stage timeout' 2>&1)
expect "下一次提交能回填超时条目" "$out" "已关联到 C-002"
grep -q 'C-002 · 2026-11-20 · change · settled' .light-work/feature-partial.md && ok "超时提交后正常定稿" || fail "第二次提交未定稿" "$out"

echo; echo "全部通过（$pass 项）"
