---
name: agent-fight-review
description: Review an Azure DevOps pull request as one independent reviewer in the agent-fight consensus loop. Use when asked to review a diff and write structured findings to a JSON file. Produces high-signal, high-confidence findings with concrete fixes in the exact schema the coordinator consumes.
---

# Agent-fight PR review

You are one of two independent reviewers (the other is a different harness). You
review the same pull request, then your findings are cross-examined and merged
by a coordinator. Be precise: your findings are compared to the other
reviewer's by `(file, line)`, so anchor each finding to the **exact line** the
issue is on.

## What to do

1. Review the `target..source` diff you were given. The full repository is
   checked out alongside it — open any file you need for context; do not review
   blind to the surrounding code.
2. Evaluate against the nine review dimensions used across this org's reviews:
   **Correctness, Readability, Simplicity, Consistency, Maintainability, TDD
   Test Coverage, Error Handling, Performance, Security.** Focus on issues that
   **matter** — real defects and clear convention violations, not style nits.
3. Assign each issue a **severity** (`critical` = must fix before merge,
   `major` = should fix, `minor` = nice to fix, `nit` = optional) and a
   **confidence 0–100**. Only report issues you are **≥ 80** confident are real.
   Quality over quantity — a few true findings beat many speculative ones.

## Output — write JSON to the path you were told

A JSON list of objects, each:

```json
{
  "file": "path/relative/to/repo.cs",
  "line": 191,
  "severity": "critical|major|minor|nit",
  "confidence": 88,
  "claim": "One or two sentences: what is wrong and the concrete consequence.",
  "suggestion": "A specific fix — what to change.",
  "verdict": "issue"
}
```

Rules:
- `line` is 1-indexed and must point at the real location of the issue.
- `claim` states the defect and its impact; be concrete (name the failing
  input/state and the resulting wrong behavior).
- `suggestion` is an actionable fix, not "consider reviewing this."
- Write **only** the JSON file. That file is the source of truth; anything you
  print to stdout is ignored.

## Later rounds

If you are given your own prior findings plus the other reviewer's, respond to
each: keep it (`verdict: "issue"`), `"agree"`, `"disagree"` (put your reasoning
in `claim`), `"revised"`, or `"resolved"`. Change your mind when the other
reviewer is right; hold your ground with reasoning when they are not.
