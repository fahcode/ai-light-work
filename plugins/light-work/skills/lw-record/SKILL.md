---
name: lw-record
description: Record the user's intent and constraints before making authorized changes.
---

# lw-record: Capture intent before editing

Use this skill when the user asks for changes. Answer questions or read code without creating an entry. Then hand the changes to `lw-edit` and the closeout to `lw-review`.

`LW = python3 ~/.light-work/lw.py`. The plugin creates a branch-local record at `.light-work/<branch>.md`. Use `$LW context --files <paths>` for active constraints.

## Choose the entry

- **Same intent, still open:** update the existing entry and append the next round number to `Rounds`.
- **Same intent, already settled:** open a new entry with `Related: relates C-xxx`.
- **New intent:** use `$LW next-id` and open a new entry.
- **Replacing an existing constraint:** use a new entry with `Related: supersedes C-xxx.n`. Keep the earlier entry as written.
- **Formatting, renames, or dependency updates:** use `chore`. Constraints are optional for chores.

Ask before an edit would clearly break an active constraint the user did not address. Use the short conflict format in `lw-edit`. Handle implementation conflicts during editing.

## Write the entry

```markdown
### C-005 · 2026-11-20 · change · open · Show guidance for an empty result
- Intent: The user wants an empty result to offer guidance without entering the error state.
- Constraints:
  - C-005.1 An empty result shows guidance and stays out of the error state.
  - C-005.2 Network errors still enter the error state.
- Assumptions: The guidance uses a fixed message.
- Files: src/search/result.ts, src/search/empty-state.ts
- Verification: —
- Rounds: R1 Requested
- Level: Pending
```

Keep the user's wording where it defines the intent. Prefer one to three testable behavior constraints. List only assumptions used to proceed. Add `Root cause: Pending` for bugs. Use `bug`, `change`, `refactor`, or `chore`. Git backfills `Commit` when it can verify ownership. Keep secrets and personal data out of the record.

New entries use the English field labels above. When updating a legacy entry, keep its existing labels and use the same language for any new fields on that entry. Use the user's language for the entry's content.

After recording the intent and before editing, run `$LW snapshot`. Pass that tree SHA and the entry ID through editing and automatic review. Take a fresh starting snapshot for each round.
