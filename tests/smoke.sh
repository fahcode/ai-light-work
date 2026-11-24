#!/usr/bin/env bash
# End-to-end smoke tests for hooks and commands in a temporary Git repository.
# Run with bash tests/smoke.sh. Requires Python 3 and Git.
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
expect() { # expect <name> <output> <expected text>
  if grep -qF -- "$3" <<<"$2"; then ok "$1"; else fail "$1 (missing: $3)" "$2"; fi
}
hook() { # hook <event> <json>
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

echo "Hooks: Git worktree detection"
out=$(hook session-start "{\"cwd\":\"$TMP\"}")
[ -z "$out" ] && [ ! -e "$HOME/.light-work/lw.py" ] && ok "Skip initialization outside Git" || fail "Initialized outside Git" "$out"
out=$(hook session-start "{\"cwd\":\"$REPO\"}")
expect "Show the change record on the base branch" "$out" "## Active constraints"
[ -f .light-work/main.md ] && ok "Create a record for the base branch" || fail "Missing base branch record" ""
out=$($L init); expect "Allow explicit initialization on the base branch" "$out" ".light-work/main.md"
git checkout -q --detach
out=$(hook session-start "{\"cwd\":\"$REPO\"}")
expect "Use the current commit to identify detached HEAD" "$out" ".light-work/detached-$(git rev-parse --short HEAD).md"
git checkout -qb feature/voice

echo "Hooks: session start"
out=$(hook session-start "{\"cwd\":\"$REPO\"}")
expect "Inject the workflow" "$out" "lw-record"
[ -f .light-work/feature-voice.md ] && ok "Create the branch record" || fail "Create the branch record" "$(ls -la .light-work)"
[ -f "$HOME/.light-work/lw.py" ] && ok "Install the CLI at the stable path" || fail "shim" ""
git rev-parse -q --verify refs/light-work/start/feature-voice >/dev/null && ok "Save the starting snapshot" || fail "Missing starting snapshot" ""

echo "Hooks: user prompt and before edit"
out=$(hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"空字符串也别当成功，改一下\"}")
expect "Run the record workflow for edit requests" "$out" "For edits, run lw-record"
ledger_before=$(git hash-object .light-work/feature-voice.md)
out=$(hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"为什么要这样改？先分析一下\"}")
expect "Ask the assistant to interpret edit keywords in questions" "$out" "Answer questions and analysis requests without creating an entry"
[ "$(git hash-object .light-work/feature-voice.md)" = "$ledger_before" ] && ok "Prompt reminders do not create entries" || fail "The question changed the record" "$out"
EDIT="{\"cwd\":\"$REPO\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$REPO/src/search/voice.ts\"}}"
out=$(hook pre-edit "$EDIT")
expect "Remind when there is no open entry" "$out" "No open entry"
ln -s "$REPO" "$TMP/link"   # Cover worktrees opened through symlinks.
out=$(hook pre-edit "{\"cwd\":\"$TMP/link\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$TMP/link/src/search/voice.ts\"}}")
expect "Recognize an edit made through a symlink" "$out" "No open entry"

echo "Skill: record the intent"
cat >> .light-work/feature-voice.md <<'EOF'
### C-001 · 2026-11-19 · change · settled · null 返回失败
- 意图: 用户："识别失败要返回失败" → res 为 null 时返回失败；老客户端的 legacyFallback 要保留
- 约束:
  - C-001.1 legacyFallback 保留，不能删除
  - C-001.2 res 为 null 时返回 ok:false reason=null
  - C-001.3 [全局] 保留旧客户端支持的响应格式。
- 假设: 无
- 文件: src/search/voice.ts
- 提交: abc1234
- 验证: ✓ 手测 null → ok:false
- Level: L0

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
out=$($L lint); expect "Lint accepts the record" "$out" "Change record valid"
out=$(hook pre-edit "$EDIT")
expect "Inject constraints from an earlier entry" "$out" "C-001.1"
expect "Legacy global constraints are labeled" "$out" "C-001.3[global]"
expect "Inject file constraints before editing" "$out" "C-002.1"
out=$(hook pre-edit "$EDIT"); [ -z "$out" ] && ok "Deduplicate an unchanged constraint" || fail "Duplicate reminder" "$out"
sed -i.bak 's/C-001.1 legacyFallback 保留，不能删除/C-001.1 legacyFallback 继续供旧客户端使用/' .light-work/feature-voice.md
rm .light-work/feature-voice.md.bak
out=$(hook pre-edit "$EDIT"); expect "Show changed text for an existing constraint ID" "$out" "C-001.1 legacyFallback 继续供旧客户端使用"
out=$(hook pre-edit "$EDIT"); [ -z "$out" ] && ok "Deduplicate the new constraint text on the next edit" || fail "Duplicate reminder after updating the constraint" "$out"

echo "Skill: edit and finish"
first_round_base=$($L snapshot)
python3 - <<'EOF'
p = "src/search/voice.ts"; s = open(p).read()
s = s.replace('  return { ok: true, data: res };',
              '  // 空字符串说明没识别到语音，不能当成功\n  if (res === "") return { ok: false, reason: "empty" };\n  return { ok: true, data: res };')
open(p, "w").write(s)
EOF
hook post-edit "$EDIT" >/dev/null
out=$(hook stop "{\"cwd\":\"$REPO\",\"stop_hook_active\":true}"); [ -z "$out" ] && ok "Skip a Stop continuation" || fail "Unexpected reminder on Stop continuation" "$out"
$L ask >/dev/null
out=$(hook stop "{\"cwd\":\"$REPO\"}"); [ -z "$out" ] && ok "Skip a reminder while waiting for a decision" || fail "Unexpected reminder while waiting" "$out"
hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"1\"}" >/dev/null
out=$(hook stop "{\"cwd\":\"$REPO\"}")
expect "Resume the reminder after the user replies" "$out" "Run lw-review to finish"
python3 -c 'import json, sys; assert set(json.loads(sys.argv[1])) == {"systemMessage"}' "$out" &&
  ok "Stop emits only the non-blocking message" || fail "Stop emitted extra control fields" "$out"
out=$(hook stop "{\"cwd\":\"$REPO\"}"); [ -z "$out" ] && ok "Remind once for the same worktree state" || fail "Duplicate closeout reminder" "$out"

echo "Skill: review"
out=$($L score --base "$first_round_base"); expect "Score reports the level" "$out" "Level: L"
expect "Report an unchanged direct caller" "$out" "handleResult → src/ui/page.ts"
expect "Score lists constraints to verify" "$out" "C-001.2"
python3 - <<'EOF'
p = ".light-work/feature-voice.md"; s = open(p).read()
s = s.replace("- 根因: 待查", "- 根因: 只判了 null，空字符串落到成功分支 voice.ts:2")
s = s.replace("- 级别: 待定", "- Level: L0").replace(
    "- 验证: ✗ 未验证",
    "- 验证: ✓ node 手测 '' → ok:false ｜ ✗ fixture-check → 断言失败 ｜ ✗ 未做真机媒体、支付与上报联调")
open(p, "w").write(s)
EOF
out=$($L lint); expect "Lint accepts the reviewed entry" "$out" "Change record valid"
out=$($L checkpoint --entry C-002 --base "$first_round_base"); expect "Checkpoint belongs to the reviewed entry" "$out" "entry C-002"
out=$($L context)
expect "Context retains executed failures" "$out" "fixture-check → 断言失败"
grep -qF "未做真机" <<<"$out" && fail "Context included unrun checks" "$out" || ok "Context omits unrun checks"
out=$($L check C-002)
expect "Check retains executed failures" "$out" "fixture-check → 断言失败"
grep -qF "未做真机" <<<"$out" && fail "Check reported an unrun check as a failure" "$out" || ok "Check omits unrun checks"
out=$(hook stop "{\"cwd\":\"$REPO\"}"); [ -z "$out" ] && ok "Skip reminders after closeout" || fail "Unexpected reminder after closeout" "$out"

echo "Edit a previous entry's changes"
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
- Level: L1
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
expect "A removed export is an interface change" "$out" "Removed src/search/voice.ts"
for command in review check; do
  out=$($L "$command" --base "$round_base" --current-entry C-003 --json)
  python3 -c 'import json, sys; d = json.loads(sys.argv[1]); assert d["range"] == "C-003 (current round)" and d["eff_files"] == ["src/search/voice.ts"] and [e["id"] for e in d["entries"]] == ["C-003"]' "$out" &&
    ok "$command internal scope is limited to the current round" || fail "$command mixed unrelated changes into the current round" "$out"
done
$L view --base "$round_base" --current-entry C-003 --title "本轮审阅" </dev/null >/dev/null
page=$(cat .light-work/feature-voice.html)
expect "Current report names the entry and scope" "$page" "C-003 (current round)"
expect "Current report contains Git diff evidence" "$page" 'reason: &quot;fail&quot;'
grep -qF 'src/unrelated.ts' <<<"$page" && fail "Current report included unrelated prior work" "" || ok "Current report contains only current-round evidence"
$L checkpoint --entry C-003 --base "$round_base" >/dev/null
git rev-list --objects refs/light-work/cp/feature-voice > "$TMP/checkpoint-objects"
grep -q "^$round_base " "$TMP/checkpoint-objects" && ok "Checkpoint retains the starting tree" || fail "Checkpoint did not retain the starting tree" ""

echo "Command: deterministic regression evidence"
out=$($L conflicts)
expect "Detect a reverted or rewritten change" "$out" "C-003 removed or rewrote"
expect "Detect a hotspot" "$out" "handleResult ← C-002 → C-003"
out=$($L check); expect "Check defaults to the latest entry" "$out" "C-003 (1 completed round)"
expect "Check shows only the selected entry status" "$out" "Open entries: C-003"
out=$($L check all); expect "All scope includes only assigned checkpoints" "$out" "All recorded changes (2 entries, 2 rounds)"
expect "All scope names entries without checkpoints" "$out" "Missing checkpoints: C-001"
for command in review check; do
  out=$($L "$command"); expect "$command defaults to the latest entry" "$out" "C-003 (1 completed round)"
  out=$($L "$command" C-002); expect "$command accepts an explicit entry" "$out" "C-002 (1 completed round)"
  all=$($L "$command" all --json)
  [ "$all" = "$($L "$command" a --json)" ] && ok "$command a matches all" || fail "$command all alias differs" "$all"
  grep -qF "src/unrelated.ts" <<<"$all" && fail "$command all included unregistered files" "$all" || ok "$command all omits unregistered files"
  branch=$($L "$command" branch --json)
  [ "$branch" = "$($L "$command" b --json)" ] && ok "$command b matches branch" || fail "$command branch alias differs" "$branch"
  expect "$command branch includes unregistered files" "$branch" "src/unrelated.ts"
  from_subdir=$(cd src/search && $L "$command" branch --json)
  [ "$branch" = "$from_subdir" ] && ok "$command branch covers the repository from a subdirectory" || fail "$command narrowed the scope from a subdirectory" "$from_subdir"
  for invalid_scope in brach 全部 全分支; do
    out=$($L "$command" "$invalid_scope" 2>&1) && fail "$command accepted invalid scope $invalid_scope" "$out"
    expect "$command rejects invalid scope $invalid_scope" "$out" "Scope must be C-xxx, all, or branch"
  done
done
out=$($L conflicts --recorded-only); expect "All scope can check reversions across completed entries" "$out" "C-003 removed or rewrote"
historical=$($L check C-003 --json)
printf 'legacyFallback(1);\nhandleResult("later");\n' > src/later.ts
[ "$historical" = "$($L check C-003 --json)" ] && ok "Historical check omits new worktree references" || fail "Worktree changes altered historical references" "$historical"
out=$($L check --base "$round_base" --current-entry C-003)
expect "Current round detects a new reference in the worktree" "$out" "Removed legacyFallback still has 1 reference: src/later.ts"
expect "Check reports the reference snapshot" "$out" "Reference snapshot"
rm src/later.ts
rm src/unrelated.ts

echo "Third-round guidance and verification lint"
python3 - <<'EOF'
p = ".light-work/feature-voice.md"; s = open(p).read()
s = s.replace("- 过程: R1 用户提出\n- Level: L1", "- 过程: R1 用户提出；R2 还是不对；R3 再改\n- Level: L1")
s = s.replace("- 验证: ✗ 未验证\n- 过程: R1 用户提出；R2", "- 验证: ✓ 应该没问题\n- 过程: R1 用户提出；R2")
open(p, "w").write(s)
EOF
out=$(hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"还是不对\"}"); expect "Round three prompts a root-cause review" "$out" "round 3"
out=$($L lint || true); expect "Reject a claim without verification evidence" "$out" "is not evidence"

echo "Commit links, settlement and reports"
sed -i.bak 's/- 验证: ✓ 应该没问题/- 验证: ✗ 未验证/' .light-work/feature-voice.md && rm -f .light-work/feature-voice.md.bak
git add -A && git commit -qm wip
grep -q "C-003 · 2026-11-20 · change · settled" .light-work/feature-voice.md && ok "Settle the entry whose checkpoint matches the commit" || fail "Automatic settlement failed" "$(grep '^###' .light-work/feature-voice.md)"
grep -q "C-002 · 2026-11-20 · bug · open" .light-work/feature-voice.md && ok "Keep a nonmatching entry open even if it shares a file" || fail "The commit settled the wrong entry" "$(grep '^###' .light-work/feature-voice.md)"
out=$($L lint); expect "Lint accepts the settled entry" "$out" "Change record valid"
grep -q "提交: [0-9a-f]\{7\}" .light-work/feature-voice.md && ok "Post-commit records the SHA" || fail "Commit link failed" "$(grep 提交 .light-work/feature-voice.md)"
out=$($L why src/search/voice.ts:2); expect "Why links a line, commit and entry" "$out" "C-003"
expect "Why includes recorded intent" "$out" "Intent:"
out=$($L why src/ui/page.ts:2); expect "Why recognizes code already on the base branch" "$out" "already on the base branch"
out=$(printf '# 改动报告\n> 两次改动\n' | $L view --title "改动报告"); expect "Render the report from stdin" "$out" ".light-work/feature-voice.html"
grep -q "改动报告" .light-work/feature-voice.html && ok "Rendered page contains the report" || fail "Missing report content" ""
sed -i.bak '1s/Change record/变更台账/' .light-work/feature-voice.md && rm -f .light-work/feature-voice.md.bak
cat <<'LW_REPORT' | $L view --scope C-002 --title '审阅 <范围>' >/dev/null
## Key changes

- Inspect empty input at `src/search/voice.ts:3`
- <script>alert('raw')</script>

| Scenario | Before | After | Evidence |
|---|---|---|---|
| Empty input | Enter success branch | Return failure | `src/search/voice.ts:3` |

```flow
Read input
Check result
```

```flow
Check input -> Return empty result | Empty
Check input -> Load details | Valid
```

```sequence
Client -> Search service | Run query
Search service -> Client | Return result `src/search/voice.ts:3`
```

```architecture
Client -> Search service | Query
Search service -> Cache | Read
Search service -> API | Fetch
```
LW_REPORT
page=$(cat .light-work/feature-voice.html)
overview=${page#*id=\"record-summary\">}
overview=${overview%%</details>*}
expect "Report metadata includes executed results" "$overview" "fixture-check → 断言失败"
grep -qF "未做真机" <<<"$overview" && fail "Report metadata included unrun checks" "$overview" || ok "Report metadata omits unrun checks"
expect "Preserve the legacy heading in the raw record" "$page" "# 变更台账 · feature/voice"
grep -q '^# 变更台账' .light-work/feature-voice.md && ok "Keep the legacy file unchanged" || fail "The report modified the legacy record" ""
expect "Render before and after columns" "$page" "<th>Before</th><th>After</th>"
expect "An entry report displays its scope" "$page" 'C-002 (1 completed round)'
expect "Section navigation points to the title" "$page" '<a class="nav-section" href="#section-1">Key changes</a>'
expect "Review sections have distinct anchors" "$page" '<h2 id="section-1">Key changes</h2>'
expect "File evidence has an offline filter" "$page" 'id="file-filter" type="search"'
expect "Evidence links back to the review" "$page" '<a href="#report">Back to review</a>'
expect "A source reference points to a Git diff row" "$page" 'href="#file-1-hunk-1-line-3"><code translate="no">src/search/voice.ts:3</code>'
expect "The linked diff row exists" "$page" 'id="file-1-hunk-1-line-3"'
expect "Render steps offline" "$page" '<ol class="flow">'
expect "Render a branching flowchart offline" "$page" '<figure class="diagram flowchart">'
expect "Group transitions with a common source" "$page" '<li class="group"><span class="node">Check input</span><ul>'
expect "Render a sequence diagram offline" "$page" '<figure class="diagram sequence">'
expect "Render a dependency diagram offline" "$page" '<figure class="diagram architecture">'
expect "Collapse the original record by default" "$page" '<details class="panel" id="ledger-raw">'
expect "Escape HTML in report content" "$page" '&lt;script&gt;alert'
grep -q '<script src=' <<<"$page" && fail "The report used a remote script" "" || ok "The report has no remote scripts"
cat <<'LW_REPORT_ZH' | $L view --scope C-002 --lang zh-Hans --title '空输入审阅' >/dev/null
## 结论

空输入返回失败，见 `src/search/voice.ts:3`。

```flow
读取输入 -> 返回失败 | 为空
```
LW_REPORT_ZH
page=$(cat .light-work/feature-voice.html)
expect "The report declares its chosen language" "$page" '<html lang="zh-Hans">'
expect "Simplified Chinese labels and scope are localized" "$page" "C-002 的 1 轮已收尾改动"
expect "Accessibility labels are localized" "$page" 'aria-label="报告导航"'
expect "File filtering uses the same language" "$page" 'placeholder="筛选文件"'
expect "The diagram caption is localized" "$page" '<figcaption>流程图</figcaption>'
expect "Author text retains the same Git diff anchor" "$page" 'href="#file-1-hunk-1-line-3"><code translate="no">src/search/voice.ts:3</code>'
expect "Diff content stays in its original language" "$page" 'reason: &quot;empty&quot;'
python3 - <<'PY'
from html import unescape
from pathlib import Path
page = Path('.light-work/feature-voice.html').read_text()
record = page.split('id="ledger-raw">', 1)[1].split('<pre translate="no">', 1)[1].split('</pre>', 1)[0]
assert unescape(record) == Path('.light-work/feature-voice.md').read_text()
PY
ok "The original record is rendered verbatim"
$L view --scope all --lang zh-CN </dev/null >/dev/null
page=$(cat .light-work/feature-voice.html)
expect "The Simplified Chinese regional tag selects Chinese labels" "$page" 'placeholder="筛选文件"'
expect "The Simplified Chinese region uses the localized default title" "$page" '<title>改动审阅</title>'
for lang in zh-Hant zh-TW; do
  $L view --scope all --lang "$lang" </dev/null >/dev/null
  page=$(cat .light-work/feature-voice.html)
  expect "$lang retains the report language tag" "$page" "<html lang=\"$lang\">"
  expect "$lang uses the English default title" "$page" '<title lang="en">Change review</title>'
done
printf '## Résumé\n\nRésultat conservé.\n' | $L view --scope C-002 --lang fr-CA --title 'Vérification' >/dev/null
page=$(cat .light-work/feature-voice.html)
expect "Unsupported catalog keeps the authored language tag" "$page" '<html lang="fr-CA">'
expect "Unsupported catalog preserves authored content" "$page" 'Résultat conservé.'
expect "English fallback labels have their own language" "$page" 'aria-label="Report navigation" lang="en"'
previous_page=$(git hash-object .light-work/feature-voice.html)
out=$($L view --scope C-002 --lang 'zh_CN' </dev/null 2>&1) && fail "Malformed tag was accepted" "$out"
expect "Malformed language tags are rejected" "$out" 'Use a language tag'
[ "$previous_page" = "$(git hash-object .light-work/feature-voice.html)" ] && ok "Invalid language leaves the last report intact" || fail "Invalid language overwrote the report" ""
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
expect "Branch navigation displays Unicode paths" "$page" 'src/显示/中文 文件.ts'
expect "A Unicode path with spaces has diff evidence" "$page" '+let value = 1;'
expect "A quoted path has diff evidence" "$page" '+const quotedFile = 1;'
expect "Retain a cited anchor late in a long patch" "$page" '-line-90"><code translate="no">src/long.ts:90</code>'
expect "Mark omitted lines in long excerpts" "$page" 'Omitted'
expect "Show totals for generated files" "$page" '+10000 / −0'
grep -qF 'generatedOnly' <<<"$page" && fail "The report included a generated file's body" "" || ok "Generated files retain only statistics"
$L view --scope all </dev/null >/dev/null
page=$(cat .light-work/feature-voice.html)
expect "The all report retains recorded rounds" "$page" 'All recorded changes (2 entries, 2 rounds)'
grep -qF 'src/显示/中文 文件.ts' <<<"$page" && fail "The all report included unregistered files" "" || ok "The all report omits unregistered files"
rm "src/显示/中文 文件.ts" 'src/显示/带"引号.ts' "src/long.ts" "dist/bundle.js"
[ "$(find .light-work -type d | wc -l | tr -d ' ')" = "1" ] && ok "Keep .light-work flat" || fail "Unexpected .light-work subdirectory" "$(find .light-work)"
git status --porcelain | grep -q ".light-work" && fail "Change records appeared in git status" "$(git status --porcelain)" || ok "Local Git excludes .light-work"

echo "Branch switching follows the current record"
git checkout -qb feature/other
out=$(hook session-start "{\"cwd\":\"$REPO\"}")
[ -f .light-work/feature-other.md ] && ok "Initialize the new branch record" || fail "Missing new branch record" "$(ls .light-work)"
out=$(hook pre-edit "$EDIT"); expect "Do not carry constraints into another branch" "$out" "No open entry"
grep -q "C-001.1" <<<"$out" && fail "Old branch constraints leaked" "$out" || ok "Old branch constraints are absent"
git checkout -qb feature/third
hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"改一下\"}" >/dev/null
[ -f .light-work/feature-third.md ] && ok "Initialize a branch after an in-session checkout" || fail "Missing branch record after checkout" "$(ls .light-work)"
git checkout -q feature/voice
out=$($L context); expect "Restore the original record after switching back" "$out" "C-003"

echo "Codex patch edits and Stop reminders"
git checkout -qb feature/codex
hook session-start "{\"cwd\":\"$REPO\"}" >/dev/null
cat >> .light-work/feature-codex.md <<'EOF'
### C-001 · 2026-11-20 · change · open · Handle missing input
- Intent: Handle an undefined result before calling handleResult.
- Constraints:
  - C-001.1 [global] Undefined input returns ok:false reason=none.
  - C-001.2 Defined values still reach handleResult.
- Assumptions: None
- Files: src/ui/page.ts
- Commit: (pending)
- Verification: ✓ fixture passed | ✗ Not run: error path | ✗ fixture → failed
- Rounds: R1 Requested
- Level: Pending
EOF
codex_edit() { # codex_edit <event> supplies a two-file patch with relative paths.
  python3 -c 'import json,sys; print(json.dumps({"cwd": sys.argv[1], "hook_event_name": "PreToolUse", "tool_name": "apply_patch",
    "tool_input": {"input": "*** Begin Patch\n*** Update File: src/search/voice.ts\n@@\n-x\n+y\n*** Update File: src/ui/page.ts\n@@\n-a\n+b\n*** End Patch\n"}}))' "$REPO" \
    | python3 "$SCRIPT" hook "$1"
}
out=$(codex_edit pre-edit); expect "Inject constraints for apply_patch" "$out" "C-001.1"
expect "English global constraints are labeled" "$out" "C-001.1[global]"
expect "Parse every file in a patch" "$out" "Constraints to preserve when editing src/ui/page.ts"
out=$($L context)
expect "English verification retains executed failures" "$out" "fixture → failed"
grep -qF 'Not run: error path' <<<"$out" && fail "Unrun checks leaked into context" "$out" || ok "English verification omits unrun checks"
echo 'export function onVoice2(res) { return res ?? { ok: false, reason: "none" }; }' >> src/ui/page.ts
codex_edit post-edit >/dev/null
out=$(hook stop "{\"cwd\":\"$REPO\",\"last_assistant_message\":\"改好了，结果如下。\"}")
expect "Remind about unclosed patch edits" "$out" '"systemMessage"'
echo '// 再改一点' >> src/ui/page.ts
out=$(hook stop "{\"cwd\":\"$REPO\"}")
expect "Remind again after another edit" "$out" '"systemMessage"'
out=$($L review 2>&1) && fail "Review used a different diff without a checkpoint" "$out"
expect "Explain why an entry diff is unavailable" "$out" "C-001 has no checkpoint"
out=$($L why src/ui/page.ts:3); expect "Why recognizes an uncommitted line" "$out" "Uncommitted change"
expect "Why finds the open entry" "$out" "C-001 · change · open"

echo "Edge cases: deleted files, lockfiles and hook failures"
git add -A && git commit -qm codex
git checkout -qb feature/edge
hook session-start "{\"cwd\":\"$REPO\"}" >/dev/null
echo "lockfileVersion: 9" > pnpm-lock.yaml
out=$($L score); expect "A lockfile-only change is L0" "$out" "Level: L0"
grep -qF "Changed configuration" <<<"$out" && fail "A lockfile was treated as configuration" "$out" || ok "Do not classify a lockfile as configuration"
rm -f pnpm-lock.yaml
git rm -q src/search/voice.ts
out=$($L score); expect "Detect exports removed with an entire file" "$out" "Removed src/search/voice.ts"
expect "Find references to symbols in a removed file" "$out" "Removed handleResult still has"
expect "Use the old line number for a removed file" "$out" "src/search/voice.ts:1-"
git checkout -q HEAD -- src/search/voice.ts
python3 - "$SCRIPT" <<'EOF'
import sys, importlib.util
spec = importlib.util.spec_from_file_location("lw", sys.argv[1]); lw = importlib.util.module_from_spec(spec); spec.loader.exec_module(lw)
lw.note_error("Hook stop", RuntimeError("boom"))
EOF
out=$(hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"看看这个函数\"}"); expect "Report a previous hook failure at the next prompt" "$out" "A previous hook failed ("
expect "Include the source of the hook failure" "$out" "Hook stop: boom"
out=$(hook prompt "{\"cwd\":\"$REPO\",\"prompt\":\"看看这个函数\"}"); grep -qF "A previous hook failed" <<<"$out" && fail "The hook failure notice repeated" "$out" || ok "Report a hook failure once"

echo "Backfill separate commits to one file"
git checkout -qb feature/partial
hook session-start "{\"cwd\":\"$REPO\"}" >/dev/null
shared='src/shared settings.ts'
printf 'export const label = "old";\nexport const timeout = 5;\n' > "$shared"
git add -- "$shared" && git commit -qm 'add shared settings'
for entry in C-001 C-002; do
  cat >> .light-work/feature-partial.md <<EOF

### $entry · 2026-11-20 · change · open · Update shared settings
- Intent: Adjust shared settings.
- Files: $shared
- Verification: —
- Level: L0
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
# Simulate a same-size edit whose mtime matches the index timestamp.
os.utime(path, ns=(stamp, stamp))
os.utime('.git/index', ns=(stamp, stamp))
PY
out=$($L checkpoint --entry C-001 --base "$first_base")
expect "Snapshot picks up same-size edits at the index timestamp" "$out" "entry C-001"
out=$(git show ":$shared")
expect "Snapshot leaves the real index intact" "$out" 'label = "old"'
git config --unset core.trustctime
second_base=$($L snapshot)
git add -- "$shared"
printf 'export const label = "new";\nexport const timeout = 8;\n' > "$shared"
$L checkpoint --entry C-002 --base "$second_base" >/dev/null
out=$(git commit -qm 'stage only label' 2>&1)
expect "Link only the staged text change" "$out" "to C-001"
grep -q 'C-002 · 2026-11-20 · change · open' .light-work/feature-partial.md && ok "Keep the uncommitted timeout change open" || fail "A partial commit settled the wrong entry" "$out"
git add -- "$shared"
out=$(git commit -qm 'stage timeout' 2>&1)
expect "Link the next commit to the timeout entry" "$out" "to C-002"
grep -q 'C-002 · 2026-11-20 · change · settled' .light-work/feature-partial.md && ok "Settle the timeout entry after its commit" || fail "The second commit did not settle its entry" "$out"
commit_lines=$(grep -cE '^- Commit: [0-9a-f]{7,40}$' .light-work/feature-partial.md)
[ "$commit_lines" = "2" ] && ok "English entries get English commit fields" || fail "English commit fields" "$commit_lines"

echo; echo "All $pass tests passed"
