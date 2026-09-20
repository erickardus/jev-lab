# PR title

A GitHub Action that reads the diff and rewrites the pull request's title with the
kind of change it actually is.

```
address review comments    ->  refactor(pr-risk): address review comments
fix nullpointer            ->  fix(billing): nullpointer
Export reports as CSV      ->  feat: Export reports as CSV
speed up builds            ->  ci: speed up builds
add SSO login              ->  feat(auth)!: add SSO login
```

Jev answers one `Choice` over the diff, *which of these ten kinds of change is this*,
plus a `Noul` for "does this break existing callers" and one for "do the author's own
words describe the change". Everything else is code: the scope comes from the changed
paths, the `!` from the breaking Noul plus a literal marker in the body, the final
string from concatenation, and the decision to touch the title at all from two
thresholds.

## How to run

The workflow lives in [`.github/workflows/pr-title.yml`](../../.github/workflows/pr-title.yml).
To use it in another repository, copy that file plus this directory, and set a
`TYPESAFE_API_KEY` secret. Without the secret the job prints a notice and exits 0,
so it is safe to merge before the key is in place.

```yaml
on:
  pull_request_target:
    types: [opened, reopened, edited, synchronize]

permissions:
  contents: read
  pull-requests: write

    - uses: actions/checkout@v4          # base branch, NOT the PR head
    - uses: astral-sh/setup-uv@v5
    - env:
        TYPESAFE_API_KEY: ${{ secrets.TYPESAFE_API_KEY }}
        GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      run: uv run --frozen examples/pr-title/pr_title.py "$PR" --repo "$GITHUB_REPOSITORY" --apply --labels
```

From a terminal:

```
uv run examples/pr-title/pr_title.py 7 --repo owner/name          # report only
uv run examples/pr-title/pr_title.py 7 --apply --labels           # rewrite the title
uv run examples/pr-title/pr_title.py 7 --check                    # exit 1 if it should change
uv run examples/pr-title/pr_title.py --fixtures                   # the bundled samples
uv run examples/pr-title/pr_title.py --body-file x.md --diff-file x.diff
```

`--check` makes it a required status check instead of an editor: the Action reports
that the title is wrong and the author fixes it, which some teams prefer to a bot
editing their words. Options: `--types path.yaml`, `--json`, `--dump-state`.

## Example response

The bundled samples, four of them borrowed from `pr-risk/fixtures`:

```
|    | p    | current title                                                       | proposed |
|----|------|---------------------------------------------------------------------|----------|
| ✅ | 1.00 | Refactor session middleware to support API tokens                   | feat!: Refactor session middleware to support API tokens |
| ⚠️  | 0.47 | Reword checkout button and empty-cart messages                      | feat: Reword checkout button and empty-cart messages |
| ⚠️  | 0.54 | Upgrade stripe SDK to v12 and migrate charge flow to PaymentIntents | feat!: Upgrade stripe SDK to v12 and migrate charge flow to PaymentIntents |
| 🔒 | 1.00 | chore(deps): bump axios from 1.6.7 to 1.6.8                         | chore(deps): bump axios from 1.6.7 to 1.6.8 |
| ✅ | 1.00 | fix nullpointer                                                     | fix(billing): nullpointer |
| ✅ | 1.00 | Export reports as CSV                                               | feat: Export reports as CSV |
| ✅ | 1.00 | speed up builds                                                     | ci: speed up builds |

7 PRs · 13,190 input tokens · $0.0006 · $0.00008 each
```

✅ rewrites, ⚠️ reports without touching the title, 🔒 leaves a prefix the author
already got right. Four of these rows are more interesting than a green tick.

**The first row contradicts the author.** The PR is called *Refactor session
middleware*, and the diff adds `resolve_principal()`, an `api_tokens` table, a
migration, and bearer-token authentication. That is a new capability, so `feat`, and
`require_role` stops falling through to the anonymous user, so `!`. The question tells
Jev that title and body are the author's intent and the diff is what happened, which
is the only reason this comes out right.

**The two ⚠️ rows are the honest ones.** A copy change scores `feat` 0.47, `style`
0.32, `chore` 0.19. Conventional Commits has no good answer for "reword a button", and
neither does Jev, so nothing is rewritten and the report shows the spread. The Stripe
PR is a dependency bump *and* a rewrite of the charge flow, genuinely two types at
once, and lands at 0.54 against an apply floor of 0.55. Both gates exist so the Action
is silent exactly when a human would have argued about it.

**The 🔒 row is dependabot.** Jev says `build` at 1.00 and `chore` at 0.00, because the
taxonomy reserves `chore` for "nothing else fits". Rewriting every bot PR from
`chore(deps)` to `build(deps)` would be pure noise, so `build` declares
`accept_existing: [chore]` and the title is left alone. A prefix the author already
chose is only replaced when the taxonomy does not recognise it as a way of saying the
same thing.

A single PR, in full:

```
$ uv run examples/pr-title/pr_title.py --body-file fixtures/null-crash.md --diff-file fixtures/null-crash.diff
fix nullpointer
  would set: fix(billing): nullpointer

type       fix  (confidence 1.00; fix 1.00, style 0.00, chore 0.00, perf 0.00)
scope      billing   [from the changed paths, in code]
breaking   no  (p=0.14)
title text describes the change  (p describes 0.64)

1,683 input tokens · $0.00007
```

## How it works

```
PR ──▶ one request: Choice(type) + Noul(breaking) + Noul(title_describes)
                    state = every file, truncated, under one budget
   ──▶ code: scope from paths · `!` from the Noul or a body marker
             · keep the author's prefix when the taxonomy accepts it
             · two gates decide whether to write
   ──▶ gh pr edit --title ... [--add-label ...]
```

**One request, not many.** `pr-risk` fans one question per rule across chunks, because
missing a single dangerous line is the failure it exists to prevent. A type is a
property of the change as a whole, so this sends every file at once, each truncated to
about 600 tokens, capped at 18k total. When the diff does not fit, `truncated` is set
and the report says so rather than quietly classifying half a PR.

**The taxonomy is the configuration.** [`types.yaml`](types.yaml) holds the ten
Conventional Commits types. Each one becomes a `Choice` option built from two fields:
`description` says what the type is, and `not` names the neighbours it keeps being
confused with. Nothing in the code knows these particular names, so a team with its
own vocabulary replaces the file.

**Code does the rest, because none of it needs reading comprehension.**

| decision | where | why |
|---|---|---|
| the scope in `type(scope):` | code | a common path prefix is a fact, not a judgement |
| `!` for breaking | Noul, or the body | `BREAKING CHANGE` in the body is the author stating it; believe them for free |
| keep the author's prefix | code | `accept_existing` in the taxonomy |
| rewrite or stay quiet | code | two thresholds over the probabilities |
| the final string | code | concatenation |

Tests are ignored while choosing a scope, unless tests are all that changed, because
`src/billing` and `tests/billing` share no directory and would otherwise produce no
scope at all. A scope equal to the type is dropped, so a workflow change is `ci:` and
never `ci(ci):`. A leading word identical to the type is dropped, so `fix nullpointer`
becomes `fix: nullpointer` and not `fix: fix nullpointer`, but only an exact match:
`bump axios from 1.6.7` keeps its verb, because "bump" carries meaning that the
repeated "fix" does not.

**Two gates before writing.** The top type must clear `apply` (0.55) and must beat the
runner-up by `margin` (0.15). Failing either one turns the run into a report. A
truncated diff is noted but does not block, since the shape of a large PR is usually
enough to type it.

### Safety of `pull_request_target`

The workflow uses `pull_request_target` rather than `pull_request`, because a
`pull_request` run from a fork gets a read-only token and could never edit the title.
That event is genuinely dangerous when a workflow builds or runs the PR's code, since
the PR author controls it and the token can write. This one never touches PR code: the
checkout takes the base branch, and the diff arrives through the API as data. Do not
add a step that checks out `github.event.pull_request.head.sha`.

Editing the title fires `edited`, which would re-run the workflow. GitHub does not
start runs for events caused by `GITHUB_TOKEN`, so there is no loop, and the script is
idempotent anyway: a title that already matches is left alone with no API write at all.
Replacing the token with a PAT removes the first guarantee and leaves only the second.

## Limits

- **Two types at once.** A PR that bumps a dependency *and* migrates the code it calls
  is not one type, and the runner-up probability is the only signal that says so. The
  margin gate turns that into silence rather than a coin toss, but the underlying
  question is still single-answer. Splitting the PR is the real fix.
- **The scope is a path, not a domain.** `src/api` and `src/web` produce no scope even
  when a human would say `reports`. Scope only knows the tree.
- **`title_describes` is reported, not acted on.** When the author's wording is filler,
  the run says so and stops. Rewriting the sentence is generation, which Jev does not
  do; the `retro` example shows the seam where an LLM would be handed that job.
- **Labels must already exist.** `gh` refuses an undefined label. That is reported and
  does not fail the run, but the labels in `types.yaml` need creating once.

## Tuning

Run `--fixtures` after every taxonomy edit; it is the cheapest possible regression
test at $0.0006 for seven PRs. When a type comes out wrong, the fix is almost always a
sentence in that type's `not`, naming the neighbour it was confused with, rather than a
change to a threshold. Raise `apply` when the bot is editing titles you disagree with,
and raise `margin` when it picks confidently between two types that were both fair.
