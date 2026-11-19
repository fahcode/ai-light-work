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
git add -A && git commit -qm init && git checkout -qb feature/voice

echo "钩子：会话开始"
out=$(hook session-start "{\"cwd\":\"$REPO\"}")
expect "注入工作流" "$out" "lw-record"
[ -f .light-work/feature-voice.md ] && ok "自动建台账" || fail "自动建台账" "$(ls -la .light-work)"
[ -f "$HOME/.light-work/lw.py" ] && ok "安装稳定路径 shim" || fail "shim" ""
git rev-parse -q --verify refs/light-work/start/feature-voice >/dev/null && ok "记录台账起点" || fail "台账起点" ""

echo "钩子：用户消息 / 编辑前"
out=$(hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"空字符串也别当成功，改一下\"}")
expect "识别改代码意图" "$out" "先 lw-record"
EDIT="{\"cwd\":\"$REPO\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$REPO/src/search/voice.ts\"}}"
out=$(hook pre-edit "$EDIT")
expect "没有 open 条目时提醒" "$out" "还没有 open 条目"

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
out=$($L lint); expect "lint 通过" "$out" "台账格式正常"
out=$(hook pre-edit "$EDIT")
expect "编辑前注入旧条目的约束" "$out" "C-001.1"
expect "编辑前注入文件约束" "$out" "C-002.1"
out=$(hook pre-edit "$EDIT"); [ -z "$out" ] && ok "同一约束不重复提示" || fail "重复提示" "$out"

echo "技能：lw-edit 改代码 → 钩子：结束前"
python3 - <<'EOF'
p = "src/search/voice.ts"; s = open(p).read()
s = s.replace('  return { ok: true, data: res };',
              '  // 空字符串说明没识别到语音，不能当成功\n  if (res === "") return { ok: false, reason: "empty" };\n  return { ok: true, data: res };')
open(p, "w").write(s)
EOF
hook post-edit "$EDIT" >/dev/null
out=$(hook stop "{\"cwd\":\"$REPO\"}")
expect "未收尾时拦下" "$out" '"decision": "block"'
out=$(hook stop "{\"cwd\":\"$REPO\",\"stop_hook_active\":true}"); [ -z "$out" ] && ok "stop_hook_active 时放行" || fail "防死循环" "$out"
$L ask >/dev/null
out=$(hook stop "{\"cwd\":\"$REPO\"}"); [ -z "$out" ] && ok "冲突闸门等待用户时不拦" || fail "ask 后仍拦" "$out"
hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"1\"}" >/dev/null
out=$(hook stop "{\"cwd\":\"$REPO\"}"); expect "用户回复后恢复收尾检查" "$out" '"decision": "block"'

echo "技能：lw-review"
out=$($L score); expect "score 给出级别" "$out" "级别: L"
expect "score 列出要核对的约束" "$out" "C-001.2"
python3 - <<'EOF'
p = ".light-work/feature-voice.md"; s = open(p).read()
s = s.replace("- 根因: 待查", "- 根因: 只判了 null，空字符串落到成功分支 voice.ts:2")
s = s.replace("- 级别: 待定", "- 级别: L0").replace("- 验证: ✗ 未验证", "- 验证: ✓ node 手测 '' → ok:false ｜ ✗ 真机未跑")
open(p, "w").write(s)
EOF
out=$($L lint); expect "定稿后 lint 通过" "$out" "台账格式正常"
out=$($L checkpoint --entry C-002); expect "收尾点归属条目" "$out" "归属 C-002"
out=$(hook stop "{\"cwd\":\"$REPO\"}"); [ -z "$out" ] && ok "收尾后不再拦" || fail "收尾后仍拦" "$out"

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
cat > src/search/voice.ts <<'EOF'
export function handleResult(res) {
  if (res === null || res === "") return { ok: false, reason: "fail" };
  return { ok: true, data: res };
}
EOF
out=$($L score); expect "删除导出被识别为接口变化" "$out" "删除 src/search/voice.ts"
$L checkpoint --entry C-003 >/dev/null

echo "命令：/lw-check 的确定性部分"
out=$($L conflicts)
expect "发现回退/改写" "$out" "C-003 删除/改写了 C-002"
expect "发现热点" "$out" "handleResult ← C-002 → C-003"
out=$($L check); expect "台账范围核对" "$out" "台账范围"
out=$($L score --entry C-002); expect "单条目 diff" "$out" "C-002 的 1 轮修改"

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
grep -q "C-002 · 2026-11-20 · bug · settled" .light-work/feature-voice.md && ok "提交后自动转 settled" || fail "自动定稿" "$(grep '^###' .light-work/feature-voice.md)"
out=$($L lint); expect "定稿后 lint 仍通过" "$out" "台账格式正常"
grep -q "提交: [0-9a-f]\{7\}" .light-work/feature-voice.md && ok "post-commit 回填 SHA" || fail "回填" "$(grep 提交 .light-work/feature-voice.md)"
out=$(printf '# 台账改动报告\n> 两次改动\n' | $L view --title "台账改动报告"); expect "从 stdin 生成页面" "$out" ".light-work/feature-voice.html"
grep -q "台账改动报告" .light-work/feature-voice.html && ok "页面包含报告内容" || fail "页面内容" ""
[ "$(find .light-work -type d | wc -l | tr -d ' ')" = "1" ] && ok ".light-work 下没有子目录" || fail "出现子目录" "$(find .light-work)"
git status --porcelain | grep -q ".light-work" && fail ".light-work 不应出现在 git status" "$(git status --porcelain)" || ok ".light-work 已被本地 exclude"

echo "切换分支：自动换成另一份台账"
git checkout -qb feature/other
out=$(hook session-start "{\"cwd\":\"$REPO\"}")
[ -f .light-work/feature-other.md ] && ok "新分支自动建自己的台账" || fail "新分支台账" "$(ls .light-work)"
out=$(hook pre-edit "$EDIT"); expect "新分支不带入旧分支的约束" "$out" "还没有 open 条目"
grep -q "C-001.1" <<<"$out" && fail "旧分支约束泄漏" "$out" || ok "旧分支约束没有泄漏"
git checkout -qb feature/third
hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"改一下\"}" >/dev/null
[ -f .light-work/feature-third.md ] && ok "会话中途切分支也自动建台账" || fail "中途切分支" "$(ls .light-work)"
git checkout -q feature/voice
out=$($L context); expect "切回来还是原来的台账" "$out" "C-003"

echo; echo "全部通过（$pass 项）"
