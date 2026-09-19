---
name: pr-risk
description: Assess how risky a pull request is (low/medium/high/critical) using Jev and the natural-language policy in examples/pr-risk/policy.yaml. Use when the user asks to rate, assess, or triage the risk of a PR, asks "how risky is PR #N", or wants to know what in a PR needs careful review. Takes a PR number or URL, or --body-file/--diff-file fixtures.
---

# PR risk

1. From the repo root run:
   ```
   uv run examples/pr-risk/pr_risk.py <pr-number-or-url> [--repo owner/name] --json
   ```
   For a local fixture: `--body-file X.md --diff-file Y.diff`. If `gh` auth
   fails, retry with `env -u GITHUB_TOKEN`.

2. Read the JSON: `level`, `score`, `thresholds`, `needs_human_review`,
   `review_flags`, `top_reasons`, `mitigations`, `rules[]` (each with
   `probability`, `fired`, `points`, `files`, `attribution`), `facts`
   (size, dependency bumps, categories), `base` (blast_radius,
   behavior_change, tests_cover_change), `contributions`, `truncated`.

3. Tell the user the level in one line, then the top reasons in plain
   language (say what the rule means, not its id). Quote the probability
   only when it matters (a near-coin-flip or a review flag).

4. For high or critical: for each fired rule list the files from
   `rules[].files`, and open those files' hunks in the diff to point at the
   lines that triggered it. If `attribution` is `chunk`, say the rule fired
   on the group of files rather than one file. Mention the dependency bump
   explicitly when one contributes.

5. If `needs_human_review` is true, say why (the flags) and which judgment
   is uncertain. If `truncated` is true, say the assessment saw partial
   diffs.

To add or change a rule, edit `examples/pr-risk/policy.yaml` (or a file with
`extends:` pointing at it): one plain-English condition per rule, `risk`
level, optional `criteria.true/false` for boundary cases, `aggregate: min`
for "every changed line ..." rules. Re-run to see the new probability in the
rules table. See `examples/pr-risk/README.md`.
