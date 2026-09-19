# PR risk assessment with Jev

Rates a pull request `low | medium | high | critical` using a policy whose
rules are written in plain English and judged by Jev, TypeSafe's System One
model. Jev does not write anything: it reads the diff and answers typed
questions with probabilities. Code owns everything else.

## How to run

```
uv run examples/pr-risk/pr_risk.py 123 --repo owner/name
uv run examples/pr-risk/pr_risk.py https://github.com/owner/name/pull/123 --json
uv run examples/pr-risk/pr_risk.py --body-file examples/pr-risk/fixtures/auth-refactor.md \
                                    --diff-file examples/pr-risk/fixtures/auth-refactor.diff
```

Options: `--policy path.yaml` (default `examples/pr-risk/policy.yaml`),
`--model jev-latest`, `--json`, `--no-localize` (skip the per-file pass),
`--dump-state` (print exactly what Jev sees, to stderr). Needs
`TYPESAFE_API_KEY` in `.env`; live PRs need `gh` logged in.

## Example response

A real run on this repo's PR #1 (two files changed in `examples/pr-risk/`, no tests):

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

Rules (fire at p >= 0.65; '?' marks a near coin-flip)
  id                       risk          p  fired  points  where
  touches-money            high       0.02  no        0.0
  auth-or-permissions      high       0.02  no        0.0
  data-migration           high       0.04  no        0.0
  data-deletion            high       0.03  no        0.0
  hardcoded-secret         critical   0.01  no        0.0
  public-api-contract      high       0.49  no?       0.0
  external-input-handling  medium     0.07  no        0.0
  error-handling-removed   medium     0.05  no        0.0
  concurrency              medium     0.37  no?       0.0
  infra-or-ci              medium     0.02  no        0.0
  tests-weakened           medium     0.03  no        0.0
  deletes-code             medium     0.48  no?       0.0
  copy-only                low        0.01  no        0.0
  logging-only             low        0.01  no        0.0
  feature-flag-guarded     low        0.02  no        0.0

Code-computed facts
  dependency  none
  size        medium (lines: medium, files: small)
  categories  {'config': 1, 'source': 1}
  deleted     none
  tests-only  False   docs-only False

Base judgments (worst chunk)
  blast_radius         1.10/3  conf 0.41  -> Local: a module used by a few callers inside one feature area
  behavior_change      1.78/3  conf 0.64  -> Modifies existing behavior: an existing code path now produces different results, defaults, or side effects
  tests_cover_change   p=0.03  no credit

Score breakdown
   +1.00  size       medium change: 2 files, 122 lines
   +0.37  base       blast radius: local (1.1/3, confidence 0.41)
   +0.59  base       behavior change: modifies existing behavior (1.8/3, confidence 0.64)
       =  1.96 -> LOW

1 request(s) over 1 chunk(s), ~3010 est. state tokens, 5501 input tokens billed · $0.0002
```

Top to bottom:

- **`PR risk: LOW  score 1.96`** — the level is the composite score mapped through
  `thresholds`. The thresholds are printed next to it so the number means something.
- **`Review: NEEDS HUMAN REVIEW`** — the tool is telling you *why it doesn't trust its own
  answer*, not that the PR is dangerous. Here: a high-risk rule sits at p=0.49 (a
  coin-flip: the PR renames things inside `pr_risk.py`, which is a public-API change
  only if something outside the PR calls them), a base judgment has confidence 0.41,
  and the score is 0.04 under the next level. Any one of those would flag it.
- **Top reasons** — the three biggest contributors to the score, in words. This is
  what you'd paste into a review comment.
- **Rules** — every natural-language rule from `policy.yaml` with Jev's probability. This
  is the raw signal *before* policy is applied, so you can see what Jev thought even
  when nothing fired. `no?` marks probabilities inside `uncertain_band`. A rule at 0.02
  is a confident no; one at 0.49 is Jev saying "the text doesn't settle this".
- **Code-computed facts** — things Jev was never asked because code answers them exactly:
  version bumps, size, file categories, deletions.
- **Base judgments** — the two Score questions every PR gets. `1.78/3` is a position on
  the level scale (probability-weighted), and `conf 0.64` says how peaked the
  distribution was. The arrow shows the level the score is nearest to.
- **Score breakdown** — the arithmetic, every term. Change a weight in `policy.yaml` and
  this is where you see the effect; no re-inference needed since the judgments above
  don't change.
- **Last line** — cost. This PR was one request, ~5.5k input tokens, about $0.0002.

For a PR that fires rules, the `where` column names the file(s) each rule fired on
(from the localisation pass), and the breakdown gains a `rule` line per fired rule.

## How it works

```
load PR ──► drop noise files ──► compute facts in code ──► chunk files
   (gh / fixtures)   (lockfiles, binaries)  (size, deps, tests)   (≤5 files, ≤20k tokens)
                                                                        │
              ┌─── one request per chunk: ALL rule Nouls + base Scores ◄┘
              │
              ├─── per-file pass: re-ask only the fired rules, one file at a time
              │
              └─► aggregate in code ──► points ──► level ──► report / JSON
```

### Why one Noul per rule

A rule like "the change modifies code that handles payments" is a yes/no
judgment about text, which is exactly what a Noul is for: it returns the
probability the statement is true. Each rule is asked as its own question, and
all rules go in the same request, so they are evaluated in parallel and adding
a rule costs almost nothing. The alternative, one big "how risky is this?"
question, hides several judgments in one answer and cannot be audited. With one
Noul per rule the report can print every probability next to the rule that
produced it.

### Why version math, counting and thresholds are in code

Jev is calibrated for semantic judgment, not arithmetic. It cannot reliably
tell that `12.1.0` is a major bump over `7.4.0`, count changed lines, or compare
a probability to a threshold. So `jevlab.pr.dependency_changes()` parses
manifests and classifies bumps in code, sizes come from line/file counts, and
`policy.yaml` maps those to points. The only thing Jev is asked is what code
cannot compute: what the diff *means*.

### Base judgments

Even with an empty `rules:` list, every PR gets two Scores and one Noul:

- `blast_radius`: isolated → local → shared → core. How much of the system
  depends on the changed code.
- `behavior_change`: none → additive → modifies existing → removes/breaks.
- `tests_cover_change`: the diff includes tests that exercise the changed code
  (a small credit when true).

Each Score is one dimension with levels described as situations, per the docs.
Scores are normalised to 0..1 and weighted (`base:` in the policy).

### Chunking and token budget

Jev's context is 32k tokens for state plus the longest question, and accuracy
drops when state contains detail the question does not need. Files are packed
in path order into chunks of at most 5 files / ~20k estimated tokens (per-file
diffs cap at 12k chars, marked `[truncated ...]`, and the report says so). One
request per chunk runs concurrently through `AsyncTypeSafeClient`.

Per-rule probabilities are combined across chunks in code: `aggregate: max`
(default) for "the change touches X" rules, `aggregate: min` for "every changed
line is X" rules, which are only true of the PR if true of every chunk. Base
Scores take the worst chunk.

### Localisation pass

When a rule fires on a multi-file chunk, the tool re-asks just the fired rules
against each file alone. That gives file-level attribution ("touches-money
fired in billing/charge.py"), and the smaller state usually sharpens the
answer. If no single file passes the threshold but the chunk did, the report
says `(chunk-level)`.

### Scoring

```
score = Σ fired rules (weight × risk_points[risk], or explicit points)
      + risk_points[risk of the largest dependency bump]
      + size points
      + blast_radius_norm × weight + behavior_change_norm × weight
      − tests_cover_change credit
level = critical if score ≥ 14, high if ≥ 4, medium if ≥ 2, else low
```

Defaults: `medium: 2, high: 4, critical: 14` points per fired rule; so one
high rule alone is HIGH, one medium rule alone is MEDIUM, a critical rule alone
is CRITICAL, and stacking three high rules with some base signal reaches
CRITICAL. `needs_human_review` is set when a high/critical rule lands in the
uncertain band (0.35–0.65; rules fire only at ≥ 0.65), a top contributor has low confidence, diffs were
truncated, or the score is within 0.5 of a threshold.


## Fixture results (tuning aids, not proof)

| fixture | level | score | why |
| --- | --- | --- | --- |
| `patch-bump` | LOW | ~1.4 | axios patch bump → 0 pts; only base scores |
| `copy-change` | LOW | ~0.3 | `copy-only` fires (0 pts), behavior change 0.0 |
| `major-bump-payments` | HIGH | ~9.3 | `touches-money` 0.99 + stripe 7→12 major |
| `auth-refactor` | HIGH | ~11.8 | `auth-or-permissions` 0.99, `data-migration` 0.99, `external-input-handling` 0.97; review flag: `public-api-contract` at ~0.54 |

## Writing good rules

Jev answers the question you wrote, not the one you meant. Rules that worked
on these fixtures only after rewording:

- **One condition per rule.** "Touches payments or auth" should be two rules;
  otherwise you cannot tell which fired.
- **Say what counts, in the diff.** "The change adds, removes, or edits code
  that ..." is better than "the PR is about ...". The question already tells
  Jev to judge from the `+`/`-` lines and treat title/body as context.
- **Put boundary cases in `criteria`.** `data-migration` originally fired on a
  file that assigned new values to ORM fields, which literally "changes how
  records are written". Adding "assigning different values to existing fields
  does not count" fixed it. `data-deletion` fired on a migration's
  `downgrade()` that drops the table it creates; a `false` criterion naming
  that case fixed it.
- **Universal rules use `aggregate: min`.** "Every changed line only edits
  text" must be true of every chunk.
- **If code can compute it, do not ask.** Tests-only and docs-only are facts
  from file paths; dependency bumps are parsed; sizes are counted.
- **Use probabilities as evidence, not truth.** A rule near 0.5 is flagged,
  not silently rounded. Fix wording before touching thresholds.

## Customising the policy

Copy the default or write a small file that extends it:

```yaml
extends: ../../examples/pr-risk/policy.yaml    # relative to this file
thresholds: { medium: 2, high: 5, critical: 14 }
rules:
  - id: touches-money            # same id → overrides fields of the default rule
    risk: critical
  - id: logging-only
    enabled: false               # drops a default rule
  - id: pii-export               # a new rule
    description: >-
      The change adds or edits code that writes personal data (names, emails,
      addresses, phone numbers) to a file, export, or third-party service.
    risk: high
    criteria:
      false: Logging a user id alone does not count.
```

Then `uv run examples/pr-risk/pr_risk.py 123 --policy my-policy.yaml`.
Rule fields: `id`, `description`, `risk`, `weight`, `points` (explicit, may be
negative for mitigations), `criteria.true/false`, `aggregate`, `enabled`.
`dependency_rules` and `size_rules` are code-evaluated maps you can retune.
