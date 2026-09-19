# Prompt coach

A Claude Code `UserPromptSubmit` hook that grades your prompt with Jev *before* Claude
sees it. Vague prompts get vague results; this nudges developers toward saying what they
want, where, and how they'll know it's done.

Three outcomes, decided in code from Jev's probabilities:

| outcome | what happens |
|---|---|
| ✅ pass | nothing; the prompt goes through untouched |
| 💡 coach | the prompt goes through; you see a one-line tip; Claude gets a note to ask one clarifying question before large changes |
| ⛔ block | the prompt is cancelled; you see the tip plus a rewrite template, and the block message shows your original text so you can edit and resend |

Blocking only happens on the **first message of a session**. Mid-conversation, short or
reply-shaped prompts ("yes", "ok do it", "fix the bug" after discussing a bug) are
legitimate and pass; the coach only speaks up mid-session for a prompt that is vague
*and* would be vague as an opening message too.

## How to run

The hook is installed for this repo in `.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{
      "type": "command",
      "command": "uv",
      "args": ["run", "--project", "${CLAUDE_PROJECT_DIR}",
               "${CLAUDE_PROJECT_DIR}/examples/prompt-coach/prompt_coach.py"],
      "timeout": 15
    }]}]
  }
}
```

Open a Claude Code session in this repo and type `fix the bug`. To use it everywhere,
copy the block into `~/.claude/settings.json` with the absolute path to this repo in
place of `${CLAUDE_PROJECT_DIR}`.

Try it without the hook:

```
uv run examples/prompt-coach/prompt_coach.py --try "fix the bug"
uv run examples/prompt-coach/prompt_coach.py --try "fix the bug" --follow-up   # as a mid-session prompt
uv run examples/prompt-coach/prompt_coach.py --fixtures                        # grade the samples below
```

Environment:

| var | effect |
|---|---|
| `PROMPT_COACH_MODE` | `block` (default), `warn` (coach only, never block), `off` |
| `PROMPT_COACH_LOG` | path; every judgment is appended as one JSON line, for tuning |

Needs `TYPESAFE_API_KEY` in the repo's `.env`. If Jev is unreachable or the key is bad,
the hook logs one line to stderr and lets the prompt through: it fails open, never
stalls a session.

## Example response

A real session in this repo. First message `build an app`:

```
▗ ▗   ▖ ▖  Claude Code v2.1.278
           Opus 5 (1M context) with high effort · Claude Max
  ▘▘ ▝▝    ~/projects/jev-lab

⏺ UserPromptSubmit operation blocked by hook:
  Prompt coach: this is likely to get a vague result (quality 0.33). Please say
  specifically what you want built or changed, and what it should do; say how you'll
  know it's done (a behavior, a test, an error that disappears).
  Try: <what to change / build> in <file or feature>, so that <expected behavior>.
  Done when <test passes / error gone / example output>.
  Edit your prompt above and send it again.

  Original prompt: build an app
```

The prompt never reached Claude. Rewritten and sent again:

```
❯ build a python script that extracts a json file and writes it to an sql database
UserPromptSubmit says: Prompt coach: consider — pin down the details you're leaving to
guess (which case, what changes); say how you'll know it's done (a behavior, a test, an
error that disappears). (quality 0.54)
```

This time it went through (quality 0.54 is above the block line of 0.35 but under the
coach line of 0.65). The tip is shown to the developer, and Claude separately received a
note that the request is underspecified and should get one clarifying question before
substantial work: which JSON shape, which database, what "done" looks like. A third
version that answers those would pass silently.

Note the tips are specific to what was missing: `build an app` names no *what* and no
*done*; the second prompt has a *what* but leaves the shape of the data and the target
database to guess, so the first tip changed and the second stayed.

The sample prompts, graded (`--fixtures`; `q` is the composite quality, `ms` is Jev's
round trip):

| outcome | q | kind | goal | done | where | ref | ms | prompt |
|---|---|---|---|---|---|---|---|---|
| ⛔ block | 0.08 | task | 0.2 | 0.03 | 0.12 | 0.97 | 485 | fix the bug |
| ⛔ block | 0.05 | task | 0.1 | 0.03 | 0.06 | 0.98 | 177 | it's still broken, can you look at it again? |
| 💡 coach | 0.44 | task | 0.7 | 0.69 | 0.26 | 0.36 | 228 | make the tests pass |
| ✅ pass | 0.21 | continuation | 0.4 | 0.02 | 0.08 | 0.15 | 163 | yes, go ahead with option 2 |
| 💡 coach | 0.38 | task | 0.7 | 0.02 | 0.64 | 0.13 | 174 | refactor auth |
| ✅ pass | 0.87 | question | 1.9 | 0.03 | 0.74 | 0.10 | 201 | why does the login page sometimes show a blank screen after OAuth r... |
| ✅ pass | 0.96 | task | 2.0 | 0.88 | 0.99 | 0.10 | 246 | Add a --json flag to examples/pr-risk/pr_risk.py that prints the fu... |
| ✅ pass | 0.97 | task | 2.0 | 0.95 | 0.99 | 0.12 | 200 | In jevlab/pr.py, dependency_changes() reports the package's own `ve... |
| ⛔ block | 0.33 | task | 1.1 | 0.22 | 0.16 | 0.97 | 183 | the function is slow, optimize it |
| ✅ pass | 0.93 | question | 2.0 | 0.07 | 0.98 | 0.19 | 171 | Explain how the localisation pass in pr_risk.py decides which file ... |

Reading a row: `goal` is a Score 0–2 (how specific is the ask), `done`/`where` are
p(yes) that the prompt says how to recognize completion / names the code it means, `ref`
is p(yes) that it leans on an unexplained "it"/"this". "refactor auth" sits right at the
block/coach boundary: it names *where* but not *what*.

## How it works

One Jev request per prompt with six questions over `{prompt, is_first_message_of_session}`:

| question | primitive | used for |
|---|---|---|
| kind: task / question / continuation / command | Choice | commands always pass; continuations pass mid-session |
| only makes sense as a reply to an earlier message | Noul | mid-session reply exemption |
| how specifically it states what is wanted | Score (3 levels) | the biggest weight in `quality` |
| says how to recognize the work is done | Noul | weighted for tasks, nearly ignored for questions |
| names the file / function / feature, or is general | Noul | |
| leans on an unexplained it / this / the bug | Noul | first messages only |

Everything after that is code: a weighted composite, thresholds (`GOAL_FLOOR`,
`BLOCK_BELOW`, `COACH_BELOW`), the first-message rule, and the tips (each is tied to one low signal, so
the advice says what is actually missing rather than "be more specific").

### Things Jev taught us while building it

- **Level text is matched literally.** The goal Score first asked how specifically the
  prompt states "what the developer wants to happen". A precise *question* ("why does the
  login page show a blank screen after OAuth redirect?") scored 0.4/2: nothing in it asks
  for anything to happen. Adding "…or the question names a specific situation and asks one
  specific thing about it" to the top level fixed it (1.9/2).
- **Reply-shaped is not the same as fine.** "it's still broken, can you look at it
  again?" is correctly judged as a reply (ack 0.98). As a first message that makes it the
  worst possible prompt, not an exempt one; the exemption now applies only mid-session.
- **Additive composites hand out free points.** `build an app` scored 0.33 against a
  block line of 0.35, because "needs no file location" and "no unexplained *it*" are
  both true of a prompt with no content. The primary signal now gates: goal at level 0
  ("not clear what is wanted") blocks a first message no matter what the rest says.
- **Check your own short-circuits.** An early `MIN_CHARS = 12` skipped judging "fix the
  bug" (11 characters). Jev never saw the canonical vague prompt.

## Tuning

Set `PROMPT_COACH_LOG=~/.prompt-coach.jsonl`, use Claude Code normally for a week, then
look at what got blocked and coached. Adjust `WEIGHTS`/thresholds in the script, or
reword a question's criteria when the *judgment* is wrong rather than the threshold.
