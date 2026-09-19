# jev-lab

Experiments with [TypeSafe](https://docs.typesafe.ai)'s **Jev**, a "System One" model.
You send it *state* (text or JSON) plus typed *questions*; it returns typed answers with
calibrated probabilities. It does not generate text. Your code owns the workflow; Jev
supplies the small semantic judgments code can't make on its own.

Three primitives:

| | asks | returns |
|---|---|---|
| **Noul** | does a yes/no condition hold? | p(yes) |
| **Choice** | which one of these options? | the option, a probability per option, confidence |
| **Score** | where on these ordered levels? | a position (e.g. 1.78/3), a probability per level, confidence |

Independent questions over the same state go in one request and run in parallel.
Pricing is per input token (~$0.04/Mtok); output is free. Every example here costs well
under a cent per run and finishes in a couple of seconds.

## Setup

```
uv sync
echo 'TYPESAFE_API_KEY=...' > .env
gh auth login          # for the two PR tools
```

If your shell exports a stale `GITHUB_TOKEN`, the PR tools drop it before calling `gh`
so your stored login is used.

## Examples

Each lives in `examples/<name>/` with its own README, and each one that takes a PR also
works offline on fixture files (`--body-file`/`--diff-file`).

### [`triage/`](examples/triage/) — the smallest example

One request, one support ticket, one of each primitive. Start here to see the shapes.

### [`ac-coverage/`](examples/ac-coverage/) — does the PR cover its acceptance criteria?

Reads the `## Acceptance Criteria` bullets from a PR description and tells you, per
bullet, whether the diff addresses it: implemented, partial, contradicted, not
implemented, no evidence, or can't-verify-from-code.

```
$ uv run examples/ac-coverage/ac_coverage.py --body-file examples/ac-coverage/fixtures/password-reset.md \
                                              --diff-file examples/ac-coverage/fixtures/password-reset.diff
3/5 criteria implemented  ·  5 files judged, 1 skipped as noise

| # | status                  | score | conf | tests | criterion |
|---|-------------------------|-------|------|-------|-----------|
| 1 | ✅ implemented          | 1.92  | 0.89 | yes   | A user can request a password reset by submitting their email address to `POST /forgot-password` |
| 2 | ✅ implemented          | 1.92  | 0.88 | no    | A reset token that expires after 1 hour is generated and emailed to the user |
| 3 | ✅ implemented          | 1.91  | 0.87 | yes   | Submitting a valid token and a new password to `POST /reset-password` updates the user's password and invalidates the token |
| 4 | ⛔ contradicted by diff | 0.06  | 0.90 | no    | Requesting a reset for an email that does not exist returns the same success response as for a known email, so accounts cannot be enumerated |
| 5 | ❌ no evidence in diff  | –     | –    | –     | Reset requests are rate limited to 5 per hour per IP address |
```

Three stages: is each criterion judgeable from code at all → which hunks determine the
behavior each criterion describes (one Noul per hunk×criterion, fanned out) → over only
that evidence, how completely is it implemented (a Score) and does it conflict (a Noul).
Splitting it this way keeps every request small, which is what Jev is accurate on.

Claude Code skill: `/ac-coverage <pr>`.

### [`pr-risk/`](examples/pr-risk/) — how risky is this PR?

Rates a PR low / medium / high / critical against a **policy written in natural
language** (`policy.yaml`). Each rule ("the change modifies code that handles payments,
billing, refunds, or pricing") becomes one Noul that Jev answers over the diff. Version
bumps, size, and the scoring arithmetic stay in code, because Jev is bad at arithmetic
and code is perfect at it.

```
$ uv run examples/pr-risk/pr_risk.py 1
PR risk: LOW   score 1.96   (medium >= 2, high >= 4, critical >= 14)
Title:   Test PR risk  (#1)
Files:   2 analyzed, 0 noise dropped · +91/-31 lines · size medium · tests 0 (0%)
Review:  NEEDS HUMAN REVIEW
         - rule `public-api-contract` (high) is near coin-flip at p=0.49
         - a top contributor has low confidence
         - score 1.96 is within 0.5 of the medium threshold (2)

Top reasons
  1. medium change: 2 files, 122 lines
  2. behavior change: modifies existing behavior (1.8/3, confidence 0.64)
  3. blast radius: local (1.1/3, confidence 0.41)
  ...
Score breakdown
   +1.00  size       medium change: 2 files, 122 lines
   +0.37  base       blast radius: local (1.1/3, confidence 0.41)
   +0.59  base       behavior change: modifies existing behavior (1.8/3, confidence 0.64)
       =  1.96 -> LOW

1 request(s) over 1 chunk(s), ~3010 est. state tokens, 5501 input tokens billed
```

The full report lists every rule with its probability, the code-computed facts, and the
arithmetic, so a level is never a black box. `NEEDS HUMAN REVIEW` means the tool
doesn't trust its own answer (a coin-flip on a high rule, low confidence, or a score
near a threshold), not that the PR is dangerous.

Claude Code skill: `/pr-risk <pr>`.

### [`prompt-coach/`](examples/prompt-coach/) — grade the prompt before Claude sees it

A Claude Code `UserPromptSubmit` hook. Jev scores each prompt (how specific is the ask,
does it say what "done" looks like, does it name the code it means, does it lean on an
unexplained "it") in ~200ms. Clear prompts pass silently; vague first messages are
blocked with a targeted tip and a rewrite template; vague mid-session prompts get a
one-line nudge and Claude is told to ask a clarifying question first.

```
| outcome  | q    | kind     | prompt                                           |
| ⛔ block | 0.07 | task     | fix the bug                                      |
| 💡 coach | 0.45 | task     | make the tests pass                              |
| ✅ pass  | 0.86 | question | why does the login page sometimes show a blank … |
| ✅ pass  | 0.96 | task     | Add a --json flag to examples/pr-risk/pr_risk.py…|
```

Installed for this repo in `.claude/settings.json`; try typing `fix the bug` in a new
session here.

### [`pii-writes/`](examples/pii-writes/) — where does personal data flow?

Scans a PR, diff, or files for code that writes personal data to logs, analytics, error
trackers, third parties, files, or the database. [PR #3](https://github.com/erickardus/jev-lab/pull/3)
is a deliberately leaky demo app kept open as a live target: `pii_writes.py 3`. Stage 1 asks per hunk which sinks and
which categories of data are involved; stage 2 asks per line, so `email → log` (high) and
`ssn → database` (medium) in the same hunk are separate findings. Hashed/masked writes
come back low, test fixtures info.

```
🔴 HIGH   app/auth/signup.py:30
        log  ·  in a hunk with: email, name, gov_id, network, dob
           30: +    logger.info("new signup user_id=%s email=%s name=%s ip=%s", user.id, email, full_name, request.remote_addr)
🟠 MEDIUM app/auth/signup.py:25
        store; sensitive data (ID/financial/health/DOB/credentials)
           25: +    user.ssn = ssn
🟡 LOW    app/auth/signup.py:42
        log; protected before write
           42: +    logger.debug("signup fingerprint=%s", email_hash)
```

### [`guard/`](examples/guard/) — a semantic linter that rejects the agent's edit

A `PreToolUse` hook on `Edit`/`Write`. Rules are plain English in `rules.yaml` ("calls
console.log instead of the project logger", "catches everything and silently continues",
"hard-codes a secret"), scoped by path, each one a Noul over the text about to be
written. A firing `deny` rule rejects the edit and Claude sees the rule and the fix, so it
rewrites and retries on its own. Handles what a regex can't: the same `console.log` being
moved rather than added (p=0.15), one inside a comment, or the logger's own source file.

```
Guard rejected this edit. Rule(s) violated:
- no-console-log (p=0.90): Use the project logger: `import { log } from '@/lib/logger'` ...
Rewrite the change so it complies, then retry.
```

### [`village/`](examples/village/) — a tiny world whose people are driven by Jev

Six villagers with roles, traits, needs, and memories, on a top-down map in the browser.
Every few seconds each idle villager gets one `Choice` over the actions available right
now — described in words, never coordinates — batched into one Jev request. Code owns
the map, paths, clock, and effects; an optional LLM writes the dialogue when two of them
meet. Thought bubbles show Jev's top choice and probability; the panel shows the whole
distribution. About 7¢ per real hour.

```
[11:40] Bram → eat_at_tavern (0.70)
[12:36] Mira → talk_to_sela (0.25)
[12:36] Mira and Sela talk about gossip
[13:46] Old Tom and Pip talk about old times
```

## Shared code

`jevlab/pr.py` fetches a PR via `gh` (a missing PR or login is a one-line error, exit 2),
splits the unified diff into files and hunks with line numbers, drops noise (lockfiles,
generated files, binaries) before anything reaches the model, and extracts dependency
version bumps with their major/minor/patch classification. `jevlab/cost.py` turns Jev
usage into dollars; every tool prints its requests, input tokens, and cost.

## What we learned about Jev

These came up in every example and are worth knowing before writing a question.

**It reads literally.** The ac-coverage relevance question first said a hunk
"contributes to implementing" the criterion. A hunk that did the *opposite* of a
criterion was judged, correctly, not to contribute, so the contradiction was never
found. Rewording to "determines how the software behaves in that situation, whether or
not it satisfies the criterion" fixed it. In pr-risk, `data-deletion` fired on an
Alembic `downgrade()` that literally calls `op.drop_table`, and `data-migration` fired
on code that merely assigns ORM fields before `db.add()`. Both were fixed by adding the
boundary case to the rule's `criteria.false`, not by moving a threshold. When a wrong
answer makes you say "but what I meant was…", that sentence is the missing half of the
instruction.

**Level and criteria text must cover every case you'll send.** The prompt-coach goal
Score asked what the developer wants "to happen"; a precise question scored as vague
because a question doesn't ask for anything to happen. One clause in the top level fixed
it.

**Ask the right kind of question for the criterion.** "Has unit tests" is about the PR,
not about software behavior, so no hunk-level question can answer it. ac-coverage now
detects PR-level criteria and judges them against the file list instead.

**Keep state small and relevant.** The context budget is 32k tokens for state plus the
longest question, and accuracy drops with irrelevant material. Both PR tools filter in
code first and split big diffs into several requests rather than one large one.

**Code does the arithmetic.** Version comparison, line counts, test-file ratios, and
score composition are all code. Jev is only asked things that need reading.

**Probabilities are for policy, not for display.** A Noul at 0.49 is Jev saying "the
text doesn't settle this", and the tools surface that as *review this* rather than
rounding it to yes or no. Thresholds in both tools are starting points; tune them on
your own PRs, and keep the raw probabilities visible (`-v`, `--json`) so you can.
