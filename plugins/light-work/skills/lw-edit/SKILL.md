---
name: lw-edit
description: Make focused changes within the recorded intent and verify the affected behavior.
---

# lw-edit: Make the smallest change that solves the problem

## Before editing

- Read the injected constraints or run `python3 ~/.light-work/lw.py context --files <paths>`.
- For a bug, trace the wrong behavior to its source. Use `python3 ~/.light-work/lw.py why PATH:LINE` to understand why a suspicious line exists.
- Check callers and consumers of the functions, types, and values you plan to change.

If the edit would clearly break an active constraint the user did not address, run `python3 ~/.light-work/lw.py ask` and ask one concise question. Constraints the user is actively revising in the same open entry can be updated there.

```text
This change conflicts with C-003.1: empty input currently returns reason=empty.
1. Use reason=fail and replace C-003.1
2. Keep reason=empty for empty input
3. Leave this part unchanged
```

Record a confirmed replacement as `Related: supersedes C-003.1`. Keep uncertainty in the entry's assumptions and report it only when it affects the conclusion.

## During the change

- Keep every edit tied to the request. Preserve public signatures and compatibility paths unless the request changes them. If they change, update affected callers.
- Read the complete branch when changing a condition. Check the surrounding success and failure paths.
- Follow the project's names, formatting, and error-handling conventions. Avoid incidental refactors, formatting, dependency changes, or new abstractions.

## Comments

When adding or materially changing core logic, give the next reader the context they need to understand the behavior.

- **Responsibility:** At a module, type, or feature entry point, explain its role, boundary, and ownership of state or resources.
- **Contract:** At a public interface or important function, explain meaningful inputs, call conditions, failure behavior, and side effects such as writes, retries, or cleanup.
- **Key paths and reasons:** Near complex logic, explain what a branch, sequence, or state transition does and which product or compatibility rule it protects. For example, acknowledge a task after persistence succeeds. On failure, leave it available for retry.

Use the depth the reader needs. Clear names can explain simple assignments and forwarding. Update comments invalidated by the edit and follow the project's comment style and language. Keep change logs, entry IDs, AI notes, and commented-out implementations out of code comments.

## Finish

Run the least costly relevant verification and record the actual command and result. If the same bug is still unresolved on the third round, revisit the root cause and explain what changed in the approach. Record the confirmed cause with a `PATH:LINE` reference. Hand off to `lw-review` with the starting snapshot and entry ID.
