---
name: retro
description: Retrospective on a Claude Code session using Jev (examples/retro/retro.py). Grades the opening prompt, attributes each correction to the prompt, the repo's instructions, or the agent, and flags loops, re-reads, drift, unused-but-relevant skills, and an unfinished outcome. Use when the user asks how a session went, why it took long, what to put in CLAUDE.md, or types /retro. Takes a transcript path, or uses the newest one for this repo.
---

# Retro

1. From the repo root run:
   ```
   uv run examples/retro/retro.py --last --json
   ```
   With a path: `--transcript <file.jsonl>`. Add `--llm` to get a rewritten
   prompt and CLAUDE.md text drafted by Sonnet (needs `ANTHROPIC_API_KEY`).
   The transcript of the *current* session is incomplete until it ends, so
   `--last` normally means the previous session.

2. Read the JSON: `ledger.scores` (prompt / agent / outcome, 0..10),
   `findings[]` (area `prompt` | `context` | `agent`, severity 1..3, title,
   evidence, suggestion), `facts` (repeats, rereads, edit_failures,
   orientation_steps, clarifying_questions, skills_used / skills_unused),
   `ledger.corrections` (by cause), and `llm` when `--llm` was passed.

3. Tell the user the three scores in one line, then the severity-3 and
   severity-2 findings grouped by area, in plain words. For a `context`
   finding quote the evidence (the correction text, the re-read file) so
   they can see what should be written down.

4. Offer, and only on a yes apply: an addition to `CLAUDE.md` for each
   `context` finding that carries a concrete fact; a sentence added to a
   skill's `description` when the finding says the skill matched but was
   not used. Show the diff before writing.

5. For trends across sessions: `uv run examples/retro/retro.py --trends`.
   The weakest rubric dimension and the files read in several sessions are
   the two most actionable lines.

Thresholds and the rubric live at the top of `examples/retro/retro.py`;
see `examples/retro/README.md`.
