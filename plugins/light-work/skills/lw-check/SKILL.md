---
name: lw-check
description: Check for regressions and verification gaps in one entry, all recorded changes, or the full branch.
argument-hint: "[C-xxx | all | branch]"
---

# lw-check: Find regressions with evidence

Use this skill for a focused regression check. Use `lw-review` to explain changes or generate a merge report.

## Select the scope

`LW = python3 ~/.light-work/lw.py`. Read manual arguments from `$ARGUMENTS`, or from the command text if the host does not expand it.

| Invocation | Evidence |
|---|---|
| Manual, no arguments | `$LW check` for the latest entry |
| Manual, a specific entry | `$LW check C-xxx` for that entry's completed rounds |
| Manual, `all` or `a` | `$LW check all` for all recorded completed rounds |
| Manual, `branch` or `b` | `$LW check branch` for the full branch, including initial development |
| From the editing workflow | `$LW check --base <starting tree SHA> --current-entry C-xxx` for the current round |

Start from the selected diff. Read unchanged callers and related constraints for context. Inspect behavior-changing files even if the CLI lists them as skipped, such as skill instructions. If the starting tree or checkpoint is missing, explain the evidence gap. For `all`, use `$LW conflicts --recorded-only` for recorded overlap. For `branch`, use `$LW conflicts`.

## Check behavior

1. **Define expectations, then look for a counterexample.** Use the user's intent, active constraints, and established contracts. For behavior changes pick a target scenario and a scenario most likely to regress. Trace input or event through the real entry point, branch or state, and observable result or side effect. For isolated text or styling changes check the relevant display scenario.
2. **Follow the effects.** Verify callers of removed or changed symbols. Also look for changed behavior behind an unchanged signature, such as a consumer of a return value, a state subscription, cleanup, or retries causing duplicate writes. Treat search results as hints and open actual call sites.
3. **Verify what distinguishes success from failure.** Prefer an existing focused test whose assertions would detect the regression. When feasible, use the same input on the before and after versions. Label actual runs, static reasoning, and gaps separately. For historical checks, review the selected snapshots. CLI reference hints use the last end snapshot in scope. Worktree tests apply to the worktree version.

Use reversions, hotspots, and repeated revisions to choose scenarios. Judge active constraints against the selected behavior. Cite `PATH:LINE` for failures and uncertainties, with entry and round if known. Ask about real conflicts between requirements. In branch scope, also inspect unregistered changes. Report relevant verification history and assumptions.

## Report the result

Use the scope in the title and keep the result within one screen. For each key finding give the scenario, expected result, actual result, and evidence. List failures first, then gaps, warnings, and passing scenarios. Mention commands and results that were actually run. Include a missing check only if it limits a conclusion. Focus on behavior.

If the user requests a fix, follow `lw-record` → `lw-edit` → `lw-review`. If a conflict is resolved, record the replacement with `supersedes`. If an unregistered change is confirmed intentional, record it in its entry or a new chore.
