---
name: lw-record
description: Starts a recorded development round before an authorized edit to code, configuration, tests, or development docs in a Git worktree, including single-file edits.
---

# lw-record: Capture intent before editing

Use this skill when the user wants an actual development edit in a Git worktree. Judge by the request and the file's role. Fixing one function or updating an API reference qualifies. Explaining that function, writing a code example in chat, or polishing an article in the repository stays outside the workflow. Hand the recorded round to `lw-edit` and the closeout to `lw-review`.

`LW = python3 ~/.light-work/lw.py`. Run `$LW init` if the current branch has no record. Use `$LW context` to find the record at the Git root, or `$LW context --files <paths>` to read constraints for the planned edits.

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

After recording or updating the entry and before editing, run `$LW snapshot --entry C-xxx` to activate the round and capture its starting tree. Pass that tree SHA and entry ID through editing and automatic review. Take a fresh snapshot for each new round.

A new user message ends hook activation. When the user is continuing the same unfinished round, use `$LW snapshot --entry C-xxx --base TREE` with the known starting tree if the intervening work still belongs to that round. An open entry alone does not activate hooks.
