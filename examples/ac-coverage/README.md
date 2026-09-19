# Acceptance-criteria coverage

Reads the acceptance criteria from a PR description, reads the diff, and tells you
per bullet whether the change addresses it. Judged by Jev (TypeSafe's System One
model): fast, cheap, calibrated yes/no and graded judgments over small pieces of
evidence. Code owns the workflow; Jev only answers narrow questions.

```
uv run examples/ac-coverage/ac_coverage.py 123 [--repo owner/name] [--json] [-v]
uv run examples/ac-coverage/ac_coverage.py --body-file pr.md --diff-file pr.diff
uv run examples/ac-coverage/ac_coverage.py 123 --criteria-file criteria.txt   # PR has no AC section
```

Exit code is `0` when every criterion is implemented (or can't be judged from code),
`1` otherwise, so it drops into CI.

## Example

```
$ uv run examples/ac-coverage/ac_coverage.py --body-file fixtures/password-reset.md --diff-file fixtures/password-reset.diff

3/5 criteria implemented  ·  5 files judged, 1 skipped as noise

| # | status                  | score | conf | tests | criterion |
|---|-------------------------|-------|------|-------|-----------|
| 1 | ✅ implemented          | 1.92  | 0.89 | yes   | A user can request a password reset by submitting their email address to `POST /forgot-password` |
| 2 | ✅ implemented          | 1.92  | 0.88 | no    | A reset token that expires after 1 hour is generated and emailed to the user |
| 3 | ✅ implemented          | 1.91  | 0.87 | yes   | Submitting a valid token and a new password to `POST /reset-password` updates the user's password and invalidates the token |
| 4 | ⛔ contradicted by diff | 0.06  | 0.90 | no    | Requesting a reset for an email that does not exist returns the same success response as for a known email, so accounts cannot be enumerated |
| 5 | ❌ no evidence in diff  | –     | –    | –     | Reset requests are rate limited to 5 per hour per IP address |
```

The fixture plants exactly that: three criteria done, one contradicted (`abort(404)` on
unknown email), one missing. ~11k input tokens, ~1.5 s, well under a cent.

## How it works

```
PR body ─parse─▶ criteria[]           "## Acceptance Criteria" bullets (or all checkboxes, or --criteria-file)
PR diff ─split─▶ hunks[]              lockfiles / generated / binary dropped in code

stage 0  verifiability     state = criteria           one Noul per criterion:
                                                       "cannot be confirmed from a diff alone"
stage 1  relevance fan-out state = {title, hunks}     one Noul per (hunk, criterion):
                                                       "this hunk determines the behavior the criterion describes"
         ── code picks evidence per criterion: p ≥ 0.6, else top-3 ≥ 0.3, else none ──
stage 2  verdict           state = {criterion,         Score 0/1/2: none-or-opposite / partial / full
         (per criterion)    evidence hunks only}       Noul: contradicts;  Noul: has tests
         ── code maps score+probabilities+confidence to a status and a 👀 flag ──
```

Stages 0 and 1 run concurrently; stage 2 runs one request per criterion, concurrently.
Each request is one `system_one` call with all of its questions batched, which is what
makes it fast and cheap.

### Why this shape

- **Jev's state budget is 32k tokens and irrelevant context costs accuracy.** So the
  whole diff is never shown to one question. Stage 1 finds the evidence; stage 2 judges
  over only that evidence. Big PRs are packed into multiple stage-1 requests.
- **Jev reads literally.** The stage-1 question used to say "contributes to implementing".
  A hunk that did the *opposite* of a criterion was (correctly, literally) judged not to
  contribute, so it never reached stage 2. Now it asks whether the hunk *determines the
  behavior in that situation, whether or not it satisfies the criterion*. When a wrong
  answer makes you say "but what I meant was…", that explanation is the missing half of
  the instruction.
- **Coverage, not correctness.** Jev is good at "is this criterion addressed, and where";
  it does not execute code or chase multi-hop logic. Anything ⚠️/⛔/❌ or 👀 is where a
  person (or Claude, via the skill) should look.
- **Policy is in code.** Thresholds at the top of the script are starting points. Tune on
  your own PRs; the `-v` flag prints the full relevance matrix so you can see what Jev
  thought before the thresholds were applied.

## Statuses

| | meaning |
|---|---|
| ✅ implemented | Score ≥ 1.5 over the evidence |
| ⚠️ partial | 0.5 ≤ Score < 1.5; something stated in the criterion is missing |
| ⛔ contradicted | Score < 0.5 and p(conflicts) ≥ 0.6 — the code does the opposite |
| ❌ not implemented | related code exists but does not implement it |
| ❌ no evidence | no hunk looked related |
| 🔍 can't verify | criterion needs runtime/visual/manual checking (perf, layout, QA) |
| 👀 | low confidence, partial, or borderline relevance: worth a human look |

## Writing criteria Jev can judge

One observable behavior per bullet, stated concretely. Good: "Requests for an unknown
email return the same 200 response as for a known email." Weak: "Handle edge cases
properly." Performance, look-and-feel, and sign-off criteria come back 🔍 by design.
