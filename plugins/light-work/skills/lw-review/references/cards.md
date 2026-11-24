# Review card template

Use `Review · C-xxx · Lx` for a single entry or round. Adjust depth to what the next maintainer needs to understand the behavior.

| Level | Content | Delivery |
|---|---|---|
| L0 | Outcome and verification in one concise statement | Chat |
| L1 | Outcome and one key source location, plus any material risk | Chat |
| L2 | Before and after behavior, impact, and evidence | Chat, with HTML if useful |
| L3 | Key path, reading order, material risks, and evidence | Verified HTML with a short chat summary and link |

Branch merge reviews also use HTML and a link. If HTML generation or verification fails, give the full review in chat and explain the failure.

## Outcome and evidence

- Lead with what changed, what was verified, and any decision the user needs to make.
- Explain the trigger, before and after paths, and observable result. Show causal links when changes share state, data, or a trigger.
- Put evidence near the claim. Use backticks around real `PATH:LINE` references so the HTML can link to scoped Git diff rows.
- State what was actually run. Mention unrun checks when they limit the conclusion.

## Diagrams

For substantial branches, ordering, state changes, or dependencies in L2, L3 and branch reviews, use one main diagram after the conclusion. Give each node a real entry point, action, or outcome. Label transitions with actual conditions. Node and transition labels can cite `PATH:LINE`.

Each relationship uses one line in the form `Source -> Target | Optional label`. A flow with no branches can also use one step per line.

```flow
Process a task -> Acknowledge task | Write succeeded
Process a task -> Retain task | Write failed
```

```sequence
Worker -> Store | Write result
Store -> Worker | Confirm write
Worker -> Queue | Acknowledge task
```

```architecture
Worker -> Store | Persist results
Worker -> Queue | Claim and acknowledge tasks
```
