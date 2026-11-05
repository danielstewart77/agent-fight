---
name: agent-fight-review
description: Review an Azure DevOps pull request as one fighter in the agent-fight consensus loop. Use when asked to review a diff and write structured findings to a JSON file. Produces evidence-backed findings with concrete fixes in the exact schema the coordinator consumes.
---

# Agent-fight PR review

You are one of two independent reviewers. You each review the same pull request,
then argue it out over several rounds until you agree or the round cap is hit.
The prompt you are given is authoritative — it carries the round, the paths, the
rival's findings, and the exact output path. This skill only fixes the schema.

## Output — write JSON to the path the prompt names

A JSON list of objects, each:

```json
{
  "file": "path/relative/to/repo/root.cs",
  "line": 191,
  "severity": "critical|major|minor|nit",
  "claim": "What is wrong and the concrete consequence.",
  "evidence": "The files and lines you read that prove that consequence.",
  "suggestion": "The specific fix.",
  "verdict": "issue",
  "response": ""
}
```

Rules:

- `line` is 1-indexed and must point at the real location of the issue. 0 means
  file-level. In a fight round, return the supplied `finding_id` unchanged.
  It identifies the finding even when its anchor or claim is revised.
- `claim` states the defect and its concrete impact: the failing input or state
  and the resulting wrong behavior.
- `evidence` is what you actually opened and read. A claim about runtime
  behavior — "creates a duplicate", "loads the wrong row", "posts without the
  id" — needs the code at every hop, not the shape of one component. If you
  cannot fill this in, you do not have a finding.
- `suggestion` is an actionable fix, not "consider reviewing this".
- `verdict` is `issue` in round 0. In a fight round it is `agree`, `disagree`,
  `revised`, or `resolved`, and `response` carries your argument — why you
  agree, or the code that proves the rival wrong. Change your mind when they are
  right; hold your ground with evidence when they are not.
- Rounds 0 and 1 run in parallel. In rounds 2+, answer only the assigned open
  items. The coordinator preserves settled findings. An author uses `issue` to
  defend a challenged claim or `resolved` to withdraw it; a challenger uses
  `agree` to accept a defense or `disagree` to maintain the objection.
- `agree` accepts the supplied claim and severity. Use `revised` to propose a
  materially changed claim or severity; the other reviewer must accept it.
- If withdrawing your duplicate, use `resolved` and `duplicate_of` with the
  surviving finding ID. Do not merge distinct defects just because lines match.
- An empty list `[]` is valid in round 0. In later rounds it leaves assigned
  responses unanswered; it is not agreement or evidence that the PR is clean.
- Write **only** the JSON file. That file is the source of truth; anything you
  print to stdout is ignored.

There is no confidence score. Show the code you read instead.
