---
name: ac-coverage
description: Check whether a pull request's diff covers the acceptance criteria in its description. Use when the user asks if a PR meets/implements its acceptance criteria, asks for AC coverage, or wants to know which requirements a PR is missing. Takes a PR number or URL.
---

# Acceptance-criteria coverage for a PR

Runs `examples/ac-coverage/ac_coverage.py`, which asks Jev (TypeSafe) per criterion
whether the diff implements it, then you explain the gaps.

## Steps

1. From the repo root run (add `--repo owner/name` if the PR is in another repo):

   ```
   uv run examples/ac-coverage/ac_coverage.py <pr> --json
   ```

   Exit `2` with "No acceptance criteria found" means the PR body has no
   `## Acceptance Criteria` bullets. Then either extract the criteria yourself from the
   description/linked issue, write them one per line to a temp file, and rerun with
   `--criteria-file <file>`; or tell the user the PR has no criteria to check.

2. Read `verdicts[]` from the JSON. Each has `status` (`implemented | partial |
   contradicted | not_implemented | no_evidence | unverifiable`), `coverage_score`
   (0–2), `coverage_confidence`, `has_tests`, `contradicts`, `unverifiable`,
   `needs_review`, and `evidence[]` (`file`, `hunk_id`, `relevance`, `diff`).

3. Report to the user as a short table: criterion → status, one line each. Then, for
   every criterion that is not `implemented`, or has `needs_review: true`:
   - `contradicted` / `not_implemented` / `partial`: open the evidence files at the hunks
     listed, read the surrounding code, and say concretely what is missing or wrong
     (e.g. "`forgot_password` returns 404 for unknown emails; the criterion requires the
     same 200 as for known emails").
   - `no_evidence`: say nothing in the diff touches this; ask whether it is out of scope
     or forgotten.
   - `unverifiable`: say it needs a runtime/manual check and suggest how.
   - `implemented` but `has_tests` < 0.5: mention it has no test in the diff.

4. Keep the raw numbers out of the prose except when they matter (a low confidence
   that justifies a "double-check this"). The JSON is the evidence; the explanation is
   yours.

Jev judges coverage, not correctness: an `implemented` verdict means the change
addresses the criterion, not that the code is bug-free. Say so if the user asks whether
the PR is "done".
