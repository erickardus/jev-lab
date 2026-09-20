# Retro

A Claude Code `SessionEnd` hook that grades the session after it ends, from three angles,
and a `SessionStart` hook that makes sure you hear about it.

| angle | question | who should change |
|---|---|---|
| **your prompt** | was the opening ask specific, bounded, exemplified, concise? | you |
| **repo context** | what did Claude have to be told, ask, or rediscover that `CLAUDE.md` or a skill should have carried? | the repo's instructions and skills |
| **the agent** | did it loop, re-read, drift, fail edits, or end on an open question? | the harness, or the model |

Code parses the transcript and counts what can be counted. Jev makes the judgments
code can't: *is this follow-up a correction, and whose fault was it?* Optionally one
Sonnet call writes what a person would paste: a rewritten prompt, a `CLAUDE.md`
paragraph, a sharper skill description. Jev decides whether and what; the LLM only
writes.

## How to run

Installed for this repo in `.claude/settings.json`:

```json
"SessionEnd":   [{ "hooks": [{ "type": "command", "command": "uv",
                  "args": ["run", "--project", "${CLAUDE_PROJECT_DIR}",
                           "${CLAUDE_PROJECT_DIR}/examples/retro/retro.py"], "timeout": 60 }]}],
"SessionStart": [{ "matcher": "startup|resume", "hooks": [{ "type": "command", "command": "uv",
                  "args": ["run", "--project", "${CLAUDE_PROJECT_DIR}",
                           "${CLAUDE_PROJECT_DIR}/examples/retro/retro.py", "--session-start"], "timeout": 10 }]}]
```

When a session ends, the report is written to `.claude/retro/<date>-<session>.md`
(git-ignored) and one JSON line is appended to `.claude/retro/ledger.jsonl`. `SessionEnd`
output is never shown to the user, so the `SessionStart` hook picks the report up: the
first reply of your *next* session in this repo opens with two or three lines on what
the last one found and what would help most, and offers to apply any `CLAUDE.md` or
skill change. It says this once per report.

On demand:

```
uv run examples/retro/retro.py --last              # newest transcript for this directory
uv run examples/retro/retro.py --fixture           # the bundled sample session below
uv run examples/retro/retro.py --last --facts-only # no Jev: only what code can count
uv run examples/retro/retro.py --last --llm        # add the "paste this" section (Sonnet)
uv run examples/retro/retro.py --last --json       # what the /retro skill reads
uv run examples/retro/retro.py --trends            # across every session in the ledger
```

Claude Code skill: `/retro`.

| var | effect |
|---|---|
| `RETRO_MODE` | `on` (default), `off` |
| `RETRO_DIR` | where reports and the ledger go (default `<repo>/.claude/retro`) |
| `RETRO_LLM` | model for the writing step, e.g. `claude-sonnet-5`; unset means no LLM call from the hook |
| `RETRO_CARRY` | `0` stops `SessionStart` from surfacing the previous retro |

Needs `TYPESAFE_API_KEY` in `.env`; `--llm` also needs `ANTHROPIC_API_KEY`. If either
is missing or unreachable the hook logs one line to stderr and exits 0: it fails open.

## Example response

The bundled fixture (`fixtures/lost-session.jsonl`) is a session that opens with
`fix the bug in the parser, tests are failing`, has two parsers to choose from, gets a
clarifying question, a failed edit, a test loosened and then corrected by the developer
("we regenerate fixtures with the invoice-fixtures skill, that's how we always do it
here"), and ends with the integration suite not run.

> The report below is the shape the tool produces. This branch was written on a machine
> without a `TYPESAFE_API_KEY`, so the Jev numbers in it come from a stub that stood in for
> Jev during development; the counted facts are real. Run `--fixture` with a key and
> replace this block with what you get.

```
# Retro: Fix failing invoice parser tests

2026-09-18 14:02 · 3 prompt(s) · 30 assistant turns · 26 tool steps · 16.7 min · claude-opus-5

**Prompt 3.2/10 · Agent 4.1/10 · Outcome 7.7/10**

> fix the bug in the parser, tests are failing

## Your prompt

- 🔴 The opening prompt did not say what you wanted — goal 0.3/2
  - Open with the change or answer you want, stated so two engineers would build the same thing.
- 🟠 The prompt did not say where — where 0.30
  - Name the file, function, or feature. Every step of orientation Claude spends finding it is a step you could have skipped.
- 🟠 No example, error text, or reproduction in the prompt — example 0.10; 13 orientation steps, 3 tool errors
  - Paste the failing command and its output, or a sample input and the output you expect.
- 🟠 Claude had to stop and ask — Q: Two parsers match ... which parser do you mean, billing/parser.py or ingest/statement_parser.py?  A: billing/parser.py, the invoice one
  - That answer belongs in the opening prompt next time; or, if it is a standing fact about this repo, in CLAUDE.md.

## Repo context

- 🔴 A correction carried repo knowledge that is not written down — follow-up 2: “no, don't loosen the test. the golden fixtures are stale, you regenerate them with the invoice-fixtures skill, that's how we always do it here” (p correction 0.90; cause repo_knowledge 0.75)
  - Add this to CLAUDE.md (or the relevant skill) so no session has to be told again.
- 🟠 The same file was read again and again — src/acme/billing/parser.py ×5
  - If this file is central, a two-line description of it in CLAUDE.md saves a re-read per session.
- 🟡 Skills used — invoice-fixtures ×1

## The agent

- 🔴 Claude got lost — directedness 0.6/2 (confidence 0.70); 13 explore steps before the first edit, longest run 7
  - Give it the entry point: name the file and the function, or add a map of the codebase to CLAUDE.md.
- 🟠 Identical tool calls were repeated — 7 repeats: src/acme/billing/parser.py ×4; uv run pytest -q ×4; ...
- 🟠 Edits failed — 1 failed edit(s): src/acme/billing/parser.py
  - Usually a stale view of the file (edited by a command, or read too early). Not the prompt's fault.
- 🟠 The final message reports something unresolved — unresolved 0.80
  - Start the next session from that sentence.

## Next time, paste this            (only with --llm / RETRO_LLM)

**CLAUDE.md → Testing** (because: correction carried repo knowledge)
    Run tests with `uv run pytest -q`. When parser tests fail with a fixture mismatch,
    regenerate fixtures with /invoice-fixtures; never loosen assertions.

## Facts
| tool steps                    | 26 (explore 14, run 7, edit 4, skill 1) |
| orientation before first edit | 13 explore steps (longest run 7) |
| repeated identical calls      | 7 |
| tool errors                   | 3 {'Bash': 2, 'Edit': 1} |
| prompt rubric                 | goal 0.3/2 · done 0.70 · where 0.30 · constraints 0.05 · example 0.10 · concise 1.8/2 · context 0.20 |
| trace                         | directed 0.6/2 · redundant 0.85 · drift 0.10 |
| outcome                       | delivered 2.7/3 · unresolved 0.80 · asks 0.05 |
| follow-ups                    | 1:answer, 2:correction/repo_knowledge |
| cost of this retro            | Jev: 6 request(s) · 7,753 input tokens · $0.0003 |
```

Reading it: the three angles disagree on purpose. The outcome is fine (7.7: everything
asked for got done and verified), but it took a clarifying question, a correction, and
13 steps of orientation to get there, and the retro says why: the prompt named neither
the file nor the failing test (*you*), and the fixture-regeneration rule lives only in
someone's head (*the repo*). The one thing that is the agent's own is the failed edit.

And what code alone sees, on the transcript of the session that built this tool
(`--facts-only`, no Jev):

```
| tool steps                    | 19 (explore 7, run 10, other 1, skill 1) |
| orientation before first edit | 7 explore steps (longest run 4) |
| repeated identical calls      | 0 |
| tool errors                   | 0 |
| skills used / available       | 1 / 26 |
| tokens fed / generated        | 131,280 / 41,484 |
```

## How it works

```
transcript.jsonl ──▶ parse (code) ──▶ facts (code) ──▶ Jev: 5 kinds of request ──▶ findings (policy, code)
                                                                                        │
                                                          report.md + ledger.jsonl ◀────┤
                                                                                        └──▶ Sonnet writes the paste-able text (optional)
```

**Parse.** One line per content block; user prompts, assistant text, `tool_use` blocks
with their inputs, `tool_result` blocks with `is_error`, the `skill_listing` attachment,
timestamps, usage. Each tool call becomes a *step* with a kind (`explore`, `edit`,
`run`, `skill`, `agent`) and a canonical key: `Read path`, `Grep pattern path`, `Bash
<normalised command>`, `Edit path <hash of old_string>`. Read-only shell commands
(`cat`, `sed -n`, `git log`, …) count as `explore`; anything with a redirect or a
mutating verb counts as `run`.

**Facts (code).** Identical keys seen twice, files read ≥ 3 times, files edited ≥ 3
times, failed edits, explore steps before the first edit and the longest explore run,
skills available minus skills used, assistant messages that end in `?` right before a
user reply (a clarifying question), duration and tokens.

**Jev.** Five request groups, each over a small state. Nothing from tool *output* is sent.

| request | state | questions |
|---|---|---|
| prompt | the opening prompt | kind (Choice); goal, concise (Score 0–2); done, where, constraints, example, context (Noul) |
| follow-up ×N | opening prompt, assistant's last text, the follow-up | kind: answer / correction / scope_change / approval (Choice); **cause**: prompt_missing_info / repo_knowledge / agent_error (Choice) |
| trace | opening prompt, the numbered tool trace (names and targets) | directed (Score 0–2); redundant, drift (Noul) |
| outcome | opening prompt, final assistant message | delivered (Score 0–3); unresolved, asks (Noul) |
| skills | opening prompt, unused skills with descriptions | one Noul per skill: does the task match what this skill says it is for |

The **cause** question is the heart of it. A correction is a fact the assistant lacked;
the retro asks whether that fact could have been in the prompt, belongs in the repo's
instructions, or was there all along and got ignored. That sends the same follow-up to
three different owners, which is what "improve the prompt or the context or the harness"
needs. Confidence and the full probability distribution are kept in the ledger so you
can see when it was a coin-flip.

**Findings (code).** Each finding ties one low signal to one suggestion, as in
prompt-coach: `where < 0.5` says *name the file*; `example < 0.4` only fires when the
session also showed the cost of not having one (errors or a long orientation). The
three 0–10 scores are weighted composites for the ledger and the one-line verdict;
the findings are the content.

**Sonnet (optional).** One `messages.create` with a JSON schema, given the findings
with severity ≥ 2, the opening prompt, the flagged skills' descriptions, and the head of
`CLAUDE.md`. It writes `prompt_rewrite`, `claude_md_additions` (section + text + which
finding justifies it), `skill_changes`, and a two-sentence note. It is asked to write
only what the findings support and never to invent facts about the codebase, and it is
skipped when there are no findings.

## Ways to wire it

The script is one pipeline; where you plug it in changes what it can do. These are the
options considered, including ones not built.

1. **`SessionEnd` report + `SessionStart` carry-over** (built, default). Silent at the
   end, spoken at the start of the next session by Claude itself, once. Closes the loop
   without a dashboard anyone has to remember to open.
2. **`/retro` on demand** (built). Claude reads the JSON, explains the findings, and
   applies `CLAUDE.md` or skill changes with you watching the diff. This is the safest
   "the retro triggers changes" path: a person is in the loop.
3. **`--trends`** (built). One session says "you didn't name the file"; twenty sessions
   say "`where` is your weakest dimension, `jevlab/pr.py` was read in 14 of them, and
   the `ac-coverage` skill matched four tasks it wasn't used for". The second is what
   actually changes `CLAUDE.md`. The ledger is JSONL; a weekly digest is a cron job away.
4. **Per-turn on `Stop`** (not built; ~1 request per turn). The same trace and follow-up
   questions after every assistant turn give a live "you are looping" nudge as
   `systemMessage`. Cheap with Jev, and the only variant that can help *this* session,
   but it interrupts, so it wants a high threshold and a once-per-session cap.
5. **Self-applying `Stop` hook** (not built). A `Stop` hook may return
   `{"decision": "block", "reason": ...}`, which makes Claude keep going with the reason
   as its instruction. Feed it the `context` findings and Claude proposes the `CLAUDE.md`
   patch before the session ends. Guard with `stop_hook_active` and a per-session marker
   or it loops forever; and it makes the retro's judgment authoritative, which, given
   the coin-flips in the ledger, it should not yet be. Option 2 gets the same effect
   with a human saying yes.
6. **Pair it with prompt-coach.** Coach grades the prompt before the session, retro
   grades what it cost after. The retro's `prompt` findings are the ground truth for
   tuning the coach's thresholds: if prompts the coach passed keep producing clarifying
   questions, its `COACH_BELOW` is too low.
7. **Judge the skills, not just their use** (not built). For a skill that *was* invoked,
   send Jev the steps that followed and ask whether they match what the skill's
   instructions say to do. Skill descriptions get you triggering; this would get you
   compliance.
8. **Team scope.** Transcripts are local, but `--transcript` takes any file, so a shared
   directory of exported transcripts plus `--trends` gives a team view of which repo
   facts are being re-taught to Claude by different people.

## Limits

- **Bash-first sessions hide reads.** When Claude reads with `cat` instead of `Read`,
  the file is not in `files_read` and re-reads are only caught as exact repeated
  commands. Steps are still classified `explore`, so orientation and directedness hold.
- **Subagents are not graded.** Their transcripts live in separate files; the main
  transcript only shows the `Agent` call. `sidechain_lines` is counted, nothing more.
- **"Lost" is judged from the trace, not the reasoning.** Jev sees tool names and
  targets, never file contents or thinking, so a long exploration that was genuinely
  necessary and a wandering one look alike to it. The confidence is printed; treat a
  low one as *look at the transcript*, not as a verdict.
- **The cause question is opinionated.** `repo_knowledge` versus `prompt_missing_info`
  is a judgment about where a fact *should* live. Expect to reword its criteria for your
  team after a week of ledger rows.
- **`SessionEnd` has a budget.** Claude Code gives it 1.5 s by default and raises it to
  the hook's `timeout`, capped at 60 s. Five Jev requests fit easily; a Sonnet call
  usually does; both plus a slow network might not, which is why the report is written
  before anything is printed and the LLM section is skipped rather than failed.

## Tuning

Use it for a week, then read `--trends` and the ledger. Change a threshold at the top of
`retro.py` when a finding fires on sessions that were fine; reword a question's
criteria when the *judgment* is wrong. The `--dump-state` flag prints exactly what Jev
saw, to stderr, for the question you are arguing with.
