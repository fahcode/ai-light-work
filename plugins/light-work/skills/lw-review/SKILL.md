---
name: lw-review
description: Explain code changes with evidence and create reports for one entry, all recorded changes, or a branch merge.
argument-hint: "[C-xxx | all | branch]"
---

# lw-review: Explain what changed and why it matters

Use `lw-check` for a focused regression investigation. Use `lw-review` for a user-facing closeout or merge report.

`LW = python3 ~/.light-work/lw.py`.

## Select the scope

| Invocation | Evidence |
|---|---|
| Manual, no arguments | `$LW review` for the latest entry |
| Manual, a specific entry | `$LW review C-xxx` for that entry's completed rounds |
| Manual, `all` or `a` | `$LW review all` for all recorded completed rounds |
| Manual, `branch` or `b` | `$LW review branch --hunks 10` for merge review |
| From the editing workflow | `$LW review --base <starting tree SHA> --current-entry C-xxx` for the current round |

Read manual arguments from `$ARGUMENTS` or the command text. If a starting tree or checkpoint is missing, explain which diffs cannot be verified. Resume an unfinished round with its original starting tree and entry ID if the worktree has not changed in the meantime.

Use the computed level. Raise it by one level only with a concrete reason. Manual review reports findings and can render a page without changing entries or checkpoints.

## Verify constraints

Check each affected constraint against the selected diff. Cite the source. In automatic review, fix regressions introduced by this round and review again. Bring an actual requirement conflict to the user. Describe a validation gap with the affected scenario and needed evidence.

Resolve the CLI's flags in context. Open candidate callers to confirm real references. Check whether existing tests cover the behavior. Verify changes outside the intended scope and record any necessary files in the current entry.

## Complete the entry during automatic review

Update actual `Files`, executed `Verification`, `Rounds`, and `Level` (maximum across rounds). For bugs, add the confirmed `Root cause` and source location. Change constraints only when the user changes the intent. Use the entry's existing field labels when updating legacy records. Run `$LW lint`. Git normally links commits and settles reviewed entries when the commit matches their checkpoint.

## Prepare the visible Review

Use [the card template](references/cards.md). One entry or round gets the label `Review · C-xxx · Lx`. L0 and L1 use chat, L2 uses chat with optional HTML, and L3 and branch reviews use HTML. For `all`, group related changes and show their evidence, risks, and validation. Branch review includes initial development and worktree changes.

Apply the [shared output language rule](references/output-language.md) to the chat summary and the report.

Make the outcome legible to someone taking over. Start with the result, whether the constraints hold, and anything the user needs to decide. For each important scenario, explain the trigger, key path before and after, observable result, and evidence. Cover meaningful failures, cancellation, retries, compatibility, and shared state when they change the outcome. Use the same range for both versions. Report unrun checks only when they limit a conclusion.

For L2, L3 and branch reviews with meaningful branches, ordering, state changes, or cross-module effects, put one main diagram just after the conclusion. Use a flowchart for decisions, a sequence diagram for ordering, and a dependency diagram for module relationships. L2 chat can use text arrows. A simple change can use a sentence and a source reference.

Render HTML with the same scope as the review: `--scope latest`, `--scope C-xxx`, `--scope all`, or `--scope branch` for manual use. In automatic review pass `--base <starting tree SHA> --current-entry C-xxx`. Set `--lang` to the language chosen for the authored report.

```bash
$LW view --scope C-005 --lang en --title "Task acknowledgment review" <<'LW_REPORT'
## Conclusion

A task is acknowledged after its result is written. A write failure leaves it eligible for retry.

## Key path

```flow
Process a task -> Acknowledge task | Write succeeded
Process a task -> Retain task | Write failed
```

## Before and after

| Scenario | Before | After | Outcome and evidence |
|---|---|---|---|
| Writing a task result fails | The task was acknowledged | The task remains eligible for retry | Cite the real source location in backticks |
LW_REPORT
```

Check that the command succeeded, the file exists, and the header shows the correct scope. Give the final reply a short conclusion and a clickable local report link. If generation or verification fails, deliver the full review in chat and say why the page could not be delivered.

## Record the checkpoint during automatic review

Prepare the visible review and verify any required HTML before running:

```bash
$LW checkpoint --entry C-005 --base <starting tree SHA>
```

Use the same entry ID and starting tree as the review. Deliver the prepared card and report link in the final reply.
