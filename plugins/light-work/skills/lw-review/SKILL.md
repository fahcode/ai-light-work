---
name: lw-review
description: Explains development changes with Git evidence for an edit closeout, a requested change summary, or a branch merge report.
---

# lw-review: Explain what changed and why it matters

Use this skill after `lw-edit` or when the user asks for a change summary or merge report. Use `lw-check` for a focused regression investigation.

`LW = python3 ~/.light-work/lw.py`.

## Select the scope

| Invocation | Evidence |
|---|---|
| Manual, no arguments | `$LW review` for the latest entry |
| Manual, a specific entry | `$LW review C-xxx` for that entry's completed rounds |
| Manual, `all` or `a` | `$LW review all` for all recorded completed rounds |
| Manual, `branch` or `b` | `$LW review branch --hunks 10` for merge review |
| From the editing workflow | `$LW review --base <starting tree SHA> --current-entry C-xxx` for the current round |

Read manual arguments from `$ARGUMENTS` or the command text. If a starting tree or checkpoint is missing, explain which diffs cannot be verified. For automatic closeout, use the entry and starting tree provided by `lw-record`.

Use the computed level. Raise it by one level only with a concrete reason. Manual review reports findings and can render a page without changing entries or checkpoints.

## Verify constraints

Check each affected constraint against the selected diff. Cite the source. In automatic review, fix regressions introduced by this round and review again. Bring an actual requirement conflict to the user. Describe a validation gap with the affected scenario and needed evidence.

Resolve the CLI's flags in context. Open candidate callers to confirm real references. Check whether existing tests cover the behavior. Verify changes outside the intended scope and record any necessary files in the current entry.

In automatic review, add needed comments for changed behavior and non-obvious contracts, and update comments made inaccurate by the edit. Do not require comments for self-explanatory code.

## Complete the entry during automatic review

Update actual `Files`, executed `Verification`, `Rounds`, and `Level` (maximum across rounds). For bugs, add the confirmed `Root cause` and source location. Change constraints only when the user changes the intent. Use the entry's existing field labels when updating legacy records. Run `$LW lint`. Git normally links commits and settles reviewed entries when the commit matches their checkpoint.

## Prepare the visible Review

Use the [card guide](references/cards.md) for delivery by level, evidence, and diagram syntax. For `all`, group related changes by behavior and cover material risks and verification. Branch review includes initial development and worktree changes.

Use the user's requested language for the summary, report, and diagram labels. Otherwise, follow the current conversation. Quote source evidence as supplied.

For each material scenario, help the next maintainer follow the change from trigger to observable result. Use the selected diff for both versions. Cover failure, cancellation, retry, compatibility, or shared state when it changes that result.

### Render HTML when needed

For L3 and branch reviews, render the report and deliver its link. L2 may include HTML when useful. Feed the report Markdown to `$LW view` on stdin and supply the title with `--title`. Use the same range as the review: manual `--scope latest`, `--scope C-xxx`, `--scope all`, or `--scope branch`. Automatic review uses `--base TREE --current-entry C-xxx`. Pass the [report language tag](references/output-language.md) with `--lang`.

Check that the command succeeded, the file exists, and the header shows the correct scope. Give the final reply a short conclusion and a clickable local report link. If generation or verification fails, deliver the full review in chat and say why the page could not be delivered.

## Record the checkpoint during automatic review

Prepare the visible review and verify any required HTML before running:

```bash
$LW checkpoint --entry C-005 --base <starting tree SHA>
```

Use the same entry ID and starting tree as the review. Deliver the prepared card and report link in the final reply.
