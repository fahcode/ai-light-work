# Light Work technical design

[Home](../README.md) · [简体中文](../README.zh-CN.md)

## Responsibilities

| Component | Responsibility |
|---|---|
| `lw.py` | Compute Git ranges, match constraints, find reference hints, score complexity, and render offline reports. |
| `lw-record` | Capture the user's intent and testable constraints before editing. |
| `lw-edit` | Make the requested change and verify the affected behavior. |
| `lw-review` | Explain what changed and complete the round using the selected scope. |
| `lw-check` | Look for regressions, potential conflicts, and gaps in the evidence. |

The CLI uses Python's standard library and Git. Rules are defined in `DEFAULTS`. No extra project configuration is required.

## Local records

A record lives at `.light-work/<branch>.md`. Detached HEAD records use `detached-<short SHA>`. The branch name is converted into a filesystem-safe slug. The directory is excluded using Git's local exclude file.

```markdown
### C-004 · 2026-11-20 · bug · open · Treat empty input as a failed lookup
- Intent: Report an empty lookup as a failure.
- Constraints:
  - C-004.1 Empty input returns reason=empty.
  - C-004.2 [global] Preserve compatibility with the old response schema.
- Files: src/search/result.ts, src/search/result.test.ts
- Root cause: The parser treated an empty string as a successful result.
- Assumptions: None
- Verification: ✓ pnpm test result → passed
- Rounds: R1 Initial fix
- Level: L1
- Related: relates C-002; supersedes C-003.1
```

`Intent` and `Verification` are required. Non-baseline entries need `Level`. A new entry can use `Pending` until review. An entry at L1 or higher needs a constraint unless it is a chore. Settled entries need their file list, and a settled bug must have a root cause. The format also accepts legacy Chinese labels and global markers in the same file. Existing field labels and user-authored text are preserved during routine updates.

`open` entries can be extended within the same intent. Settled entries are historical. `dropped` entries are skipped by active constraint selection. Use `relates` for related work and `supersedes` to replace a constraint or another entry's constraints. Only executed checks belong in `Verification`. Summaries omit descriptions of checks that were not run.

`Files` accepts files and directories. The assistant records expected paths before editing and corrects them at closeout. `Rounds` records iterations such as `R1 ...; R2 ...`. The third round prompts a root-cause review. `Level` retains the highest level reached by the entry. Use `Assumptions` for a decision made without confirmation. A bug's root cause should cite a source location.

## Snapshots and review ranges

| Ref | Purpose |
|---|---|
| `refs/light-work/start/<slug>` | Initial worktree snapshot when the change record is created |
| `refs/light-work/cp/<slug>` | Completed rounds, with an entry ID and starting tree for each round |

Snapshots use a temporary copy of the index and preserve its timestamp so Git can detect rapid same-size edits. Tracked and unignored untracked changes are included. The real index is left alone. A checkpoint keeps the before and after trees addressable. The post-commit hook verifies committed file contents against the checkpoint before linking an entry and settling it.

`review` and `check` use the same scope:

| Scope | Diff |
|---|---|
| No arguments | Completed rounds of the latest entry |
| `C-xxx` | Completed rounds of one entry |
| `all` or `a` | Completed rounds of all recorded entries |
| `branch` or `b` | Merge base to worktree |
| `--base REF --current-entry C-xxx` | One current round from the before-edit tree to the worktree |

Without a checkpoint, an entry's diff is unavailable. A historical reference search uses the last end snapshot in scope. Reference hints come from name matches in Git, which can include unrelated names. Review actual call sites before asserting an impact.

Complexity ranges from L0 to L3. Signals include changed lines, file and module counts, interface changes, configuration, schema, and sensitive areas. Affected constraints are shown at every level without increasing it. Overlap analysis uses checkpoints to find shared file/symbol pairs, lines removed after addition by an earlier entry, and entries with three or more rounds.

| Signal | Level |
|---|---|
| Up to 10 effective lines in one file, or tests, documentation, lockfiles and assets alone | L0 |
| More than 10 effective lines or more than one file | L1 |
| At least 50 lines, more than three files, two modules, interface or configuration changes, relevant keywords, or an isolated sensitive keyword | L2 |
| At least 200 lines, more than eight files, at least three modules, repeated sensitive keywords or one in a path, or schema and migration changes | L3 |

Each signal contributes its highest matching level. Effective changes exclude tests and files filtered by rule. Interface signals count removed exports and changed signatures. Warnings for remaining references, missing recognized test changes, deletion-heavy changes, and unregistered files are independent of the level. The CLI shows up to five matching files per symbol for up to eight direct caller hints.

## HTML reports

`view` takes Markdown on stdin and can attach evidence for a selected range. The output is a self-contained `.light-work/<branch>.html`. The CLI escapes raw HTML, renders headings, lists and tables, and makes backticked `PATH:LINE` references clickable where the target exists in the selected diff. Excerpts prioritize cited lines. Large patches and generated files can be summarized.

The page starts with scope, conclusions, and the report's key diagram. Section navigation, filename and path filtering, evidence expansion, and return links work offline. The original record is collapsed by default. Diagrams accept `flow`, `sequence`, and `architecture` blocks. A relationship is `Source -> Target | Optional label`. A simple flow can use one step per line. Mermaid blocks retain their source text. No browser runtime needs to be installed alongside the plugin.

Small reviews use a chat card. L3 and merge reports use HTML with a visible link. Before automatic closeout, verify the generated file and scope. If the file cannot be delivered, give the full review in chat with the reason. Manually rendering a report does not change the ledger or checkpoint.

The assistant supplies `view --lang TAG` from the same language decision as the chat and authored report. The renderer declares that [BCP 47 language tag](https://www.w3.org/International/questions/qa-html-language-declarations) on the page and selects built-in English or Simplified Chinese labels. It uses English labels when a catalog is unavailable or no tag is supplied. Report text and original evidence are rendered as supplied.

## Hooks

| Event | CLI | Behavior |
|---|---|---|
| SessionStart | `hook session-start` | Install the stable CLI copy at `~/.light-work/lw.py` and give the assistant the rule for when to start a development round. |
| UserPromptSubmit | `hook prompt` | End the previous hook activation. The assistant can explicitly reactivate an unfinished development round when resuming it. |
| PreToolUse | `hook pre-edit` | During an active round, show matching constraints and deduplicate unchanged text. |
| Stop | `hook stop` | Compare the active round with its starting tree. Remind once per changed tree and skip while waiting for a decision. |
| Git post-commit | `backfill` | Link a commit when its content identifies the reviewed entry. |

The assistant activates a round through `lw-record` once it has identified an authorized development change. That skill initializes the branch record and uses `snapshot --entry` to bind the worktree's active round to its branch, entry, and starting tree. A matching checkpoint closes the round. Historical records and manual reviews remain available independently. Each worktree has one active round.

PreToolUse recognizes configured editor tools and Codex patch paths. Stop compares Git trees, so it also covers edits made with shell commands during an active round. Git hooks leave source files untouched. Existing `core.hooksPath` configurations are left intact. Hook failures are reported once at the next session start or prompt. Use `LW_DEBUG=1` to inspect a failure directly.

## Command reference

```bash
python3 ~/.light-work/lw.py init
python3 ~/.light-work/lw.py context --files src/example.py
python3 ~/.light-work/lw.py next-id
python3 ~/.light-work/lw.py snapshot --entry C-004
python3 ~/.light-work/lw.py review C-004 --json
python3 ~/.light-work/lw.py check branch
python3 ~/.light-work/lw.py check --base TREE --current-entry C-004
python3 ~/.light-work/lw.py review --base TREE --current-entry C-004
python3 ~/.light-work/lw.py checkpoint --entry C-004 --base TREE
python3 ~/.light-work/lw.py conflicts --recorded-only
python3 ~/.light-work/lw.py why src/example.py:42
python3 ~/.light-work/lw.py view --scope branch --lang en < report.md
python3 ~/.light-work/lw.py ask
python3 ~/.light-work/lw.py lint
python3 ~/.light-work/lw.py stats
python3 ~/.light-work/lw.py backfill
```
