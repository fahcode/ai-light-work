# Changelog

## light-work 0.6.10 · 2026-11-24

- Use the language requested by the user or inferred from the conversation for Check and Review output, including report text and diagrams.
- Keep offline report controls in English or Simplified Chinese, with English as the fallback. Preserve code, diffs, and other source evidence as supplied.

## light-work 0.6.9 · 2026-11-24

- Made English the default for the plugin, CLI, hooks, review reports and documentation. The Chinese README remains available.
- Added English change record fields while preserving compatibility with legacy Chinese records and their checkpoints.
- Reworked reviews around triggers, before and after paths, outcomes and evidence. Complex behavior gets a lead diagram.
- Redesigned the offline report with section navigation, file filtering, and direct navigation between explanations and diff evidence.

## light-work 0.6.8 · 2026-11-23

- Expanded the comment guidance in `lw-edit` to cover responsibilities, contracts, key paths and rationale. Update comments invalidated by changed behavior.

## light-work 0.6.7 · 2026-11-23

- Labeled single-entry and current-round Review cards with the entry and level. L0 and L1 use chat. L2 can include an evidence page.
- L3 and branch reviews deliver a link to scoped HTML. If rendering or validation fails, give the full review in chat and explain the failure.

## light-work 0.6.6 · 2026-11-23

- `lw-check` traces the target and preserved behaviors through real entry points and distinguishes executed checks from static reasoning and validation gaps.
- Historical reference searches use the selected ending snapshot and display its version.
- Commit backfill verifies ownership against the checkpoint when changes to the same file are committed separately.
- Changed constraint text is shown again. Reviews verify constraints against the user's intent.
- Fixed snapshots missing rapid same-size edits.

## light-work 0.6.5 · 2026-11-20

- Unified the change record terminology. Legacy records stayed readable.
- Summaries now show executed checks. Review reports describe before and after behavior and related effects.

## light-work 0.6.4 · 2026-11-20

- Added scope, file statistics, and links from conclusions to real Git diff lines.
- Made navigation, diffs, and flow, sequence, and dependency diagrams available offline. The original record is collapsed.
- Prioritized cited lines and fixed diff navigation for quoted paths.

## light-work 0.6.3 · 2026-11-20

- Unified scope arguments for `lw-review` and `lw-check`. Internal calls use the current round.
- Combined change and merge reporting in `lw-review`. The standalone `lw-report` and `lw-branch` commands were removed.

## light-work 0.6.2 · 2026-11-20

- Made edit intent contextual. Questions do not create entries.
- Enabled the workflow on base branches and detached HEAD.
- Made the Stop reminder non-blocking and deduplicated per worktree state.

## light-work 0.6.1 · 2026-11-20

- Fixed hook path handling through symlinks, whole-file deletion diffs, and lockfiles being classified as configuration.
- Added one-time hook failure notices and atomic installation of the shared CLI.

## light-work 0.6.0 · 2026-11-20

- Added Codex marketplace support and patch-based edit handling.
- Added `lw.py why PATH:LINE` to trace change intent and constraints from Git blame.
- Added direct caller hints and reduced unnecessary Stop reminders.

## light-work 0.5.0 · 2026-11-20

- Removed automatically inferred baseline constraints, simplified entry fields, and added local HTML for complex reviews.
- Clarified the project's scope and feature mapping in the README.

## light-work 0.4.2 · 2026-11-20

- Flattened storage to one record per branch. Chat cards are not stored separately.
- Rendered reports from stdin to the branch's HTML file.

## light-work 0.4.1 · 2026-11-20

- Simplified the edit skill, generated a fresh handoff from the branch record, and documented how handoff and change records relate.

## light-work 0.4.0 · 2026-11-20

- Standardized plugin and skill names, removed configuration and pause switches, and initialized branch records on demand.
- Settled reviewed entries after commit. Added conflict confirmation and expanded smoke coverage.

## 0.3.0 · 2026-11-20

- Introduced the plugin marketplace, focused editing, root-cause and evidence checks, third-round guidance, and end-to-end smoke tests.

## 0.2.0

- Packaged the record, edit, and closeout skills with hooks and manual commands.
- Linked checkpoints to entries, added overlap and reversion checks, and separated entry reports from branch summaries.

## 0.1.0

- Added change records, complexity scoring, reference hints, and unregistered change detection.
