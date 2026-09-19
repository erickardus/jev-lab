"""Prompt coach: a Claude Code `UserPromptSubmit` hook that grades the prompt with Jev.

Vague prompts get vague results. Before Claude sees a prompt, this hook asks Jev a
handful of narrow questions about it (is the goal specific? does it say what "done"
looks like? does it point at the code it means? does it lean on an unexplained
"it"/"this"?) and turns the answers into one of three outcomes:

  pass   nothing happens; the prompt goes through untouched
  coach  the prompt goes through, you get a one-line tip, and Claude gets a note to
         ask a clarifying question before making large changes
  block  the prompt is cancelled and you get the tip plus a rewrite template; the
         block message shows your original text so you can edit and resend

Jev is a System One model: it returns calibrated probabilities, not text, in a few
hundred milliseconds. The policy (what counts as vague enough to block) lives in
this file, in code, so you can tune it on your team's prompts.

Install (already in this repo's .claude/settings.json):
  hooks.UserPromptSubmit -> uv run --project $CLAUDE_PROJECT_DIR examples/prompt-coach/prompt_coach.py

Try it without the hook:
  uv run examples/prompt-coach/prompt_coach.py --try "fix the bug"
  uv run examples/prompt-coach/prompt_coach.py --fixtures        # grade the sample prompts

Env:
  PROMPT_COACH_MODE   block (default) | warn (never block) | off
  PROMPT_COACH_LOG    path; when set, every judgment is appended as JSON for tuning
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

from jevlab.cost import cost_usd

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

# --- Policy. Tune these on your own prompts; `--fixtures` shows the effect. ------------
MIN_CHARS = 3  # shorter than this is never judged; "y", "ok" are cheap to judge and Jev calls them continuations
BLOCK_BELOW = 0.35  # quality < this on a first prompt  -> block
GOAL_FLOOR = 0.5  # goal Score below this (level 0: unclear what is wanted) on a first prompt -> block
COACH_BELOW = 0.65  # quality < this                    -> coach
ACK = 0.7  # p(prompt is an acknowledgement/continuation) above this -> pass silently
WEIGHTS = {"goal": 0.45, "done": 0.25, "where": 0.20, "referent_ok": 0.10}
TIMEOUT_S = 8.0  # Jev usually answers in <1s; never let the hook stall a session


@dataclass
class Judgment:
    prompt: str
    kind: str
    kind_confidence: float
    ack: float
    goal: float  # Score 0..2
    done: float  # Noul
    where: float  # Noul
    referent: float  # Noul: leans on an unexplained it/this/that
    quality: float  # 0..1 composite
    outcome: str  # pass | coach | block
    missing: list[str]
    ms: int
    input_tokens: int = 0


# ----------------------------------------------------------------------------- questions

def questions() -> dict:
    return {
        "kind": Choice(
            instructions="What kind of message is `prompt`, sent by a developer to a coding assistant?",
            criteria={
                "task": "Asks the assistant to change, create, fix, refactor, test, or run something",
                "question": "Asks for an explanation, opinion, or information; no change is requested",
                "continuation": "Replies to something the assistant said: approval, a choice among options, 'continue', 'yes', 'try again', a short correction",
                "command": "A slash command, a bare shell command, or a one-word instruction like 'commit'",
            },
        ),
        "ack": Noul(
            instructions="`prompt` only makes sense as a reply to an earlier message in an ongoing conversation",
            criteria={
                "true": "It approves, picks, corrects, or continues something already under discussion, and would be meaningless as an opening message",
                "false": "It stands on its own as an opening request or question",
            },
        ),
        # Level text covers both tasks and questions: Jev matches levels literally, and
        # "what the developer wants to happen" scored a precise question as vague.
        "goal": Score(
            instructions="How specifically does `prompt` state what the developer wants: the change to make, or the question to answer?",
            criteria=[
                "It is not clear what is wanted; the request could mean many different things (e.g. 'fix the bug', 'make it better', 'look at this')",
                "The general task or topic is clear but key details are left to guess: which behavior, which case, what should be different",
                "The wanted change is stated specifically enough that two engineers would build the same thing, or the question names a specific situation and asks one specific thing about it",
            ],
        ),
        "done": Noul(
            instructions="`prompt` says how to recognize that the work is complete or correct",
            criteria={
                "true": "It names an expected behavior, output, test, error that should disappear, constraint that must hold, or example of the result",
                "false": "Nothing in it says what the finished result should look like or do",
            },
        ),
        "where": Noul(
            instructions="`prompt` identifies where in the codebase or which thing it is about",
            criteria={
                "true": "It names a file, function, class, module, component, endpoint, command, error message, or feature, or the question is general and needs no location",
                "false": "It refers to code or a problem without saying which one, and the request is not general",
            },
        ),
        "referent": Noul(
            instructions="`prompt` depends on a word like 'it', 'this', 'that', 'the bug', 'the error', or 'the function' whose meaning is not given in the prompt itself",
            criteria={
                "true": "Understanding the request requires knowing what such a word points to, and the prompt does not say",
                "false": "Every such word is explained in the prompt, or no such word is used",
            },
        ),
    }


# ----------------------------------------------------------------------------- judge

def judge(prompt: str, is_first: bool, client: TypeSafeClient) -> Judgment:
    t0 = time.perf_counter()
    resp = client.system_one(
        state={"prompt": prompt, "is_first_message_of_session": is_first},
        questions=questions(),
        timeout=TIMEOUT_S,
    )
    kind = resp.choices["kind"]
    ack = resp.nouls["ack"].noul
    goal = resp.scores["goal"].score
    done = resp.nouls["done"].noul
    where = resp.nouls["where"].noul
    referent = resp.nouls["referent"].noul

    # --- Policy in code. -------------------------------------------------------------
    # Questions need a clear goal, not necessarily a "done" criterion; tasks need both.
    w = dict(WEIGHTS)
    if kind.choice == "question":
        w["done"], w["goal"] = 0.05, w["goal"] + 0.20
    quality = (
        w["goal"] * (goal / 2)
        + w["done"] * done
        + w["where"] * where
        + w["referent_ok"] * (1 - referent)
    ) / sum(w.values())

    missing: list[str] = []
    if goal < 1.0:
        missing.append("say specifically what you want built or changed, and what it should do")
    elif goal < 1.6:
        missing.append("pin down the details you're leaving to guess (which case, what changes)")
    if done < 0.5 and kind.choice == "task":
        missing.append("say how you'll know it's done (a behavior, a test, an error that disappears)")
    if where < 0.5:
        missing.append("name the file, function, or feature you mean")
    if referent > 0.6 and is_first:
        missing.append("spell out what 'it'/'this' refers to; this is the first message, there's no context yet")

    # A reply-shaped message is fine mid-conversation. As the FIRST message of a session
    # there is nothing to reply to, so it gets judged like any other prompt.
    if kind.choice == "command" or (not is_first and (kind.choice == "continuation" or ack >= ACK)):
        outcome = "pass"
    elif is_first and (goal < GOAL_FLOOR or quality < BLOCK_BELOW):
        # Gate on the primary signal. An additive composite hands "build an app" free
        # points for having no unexplained "it" and needing no file, and it lands 0.02
        # under the block line. Goal at level 0 means "not clear what is wanted": block.
        outcome = "block"
    elif quality < COACH_BELOW:
        outcome = "coach"
    else:
        outcome = "pass"

    return Judgment(
        prompt=prompt,
        kind=kind.choice,
        kind_confidence=kind.confidence,
        ack=ack,
        goal=goal,
        done=done,
        where=where,
        referent=referent,
        quality=quality,
        outcome=outcome,
        missing=missing,
        ms=int((time.perf_counter() - t0) * 1000),
        input_tokens=resp.usage.input_tokens,
    )


# ----------------------------------------------------------------------------- hook I/O

TEMPLATE = (
    "Try: <what to change / build> in <file or feature>, so that <expected behavior>. "
    "Done when <test passes / error gone / example output>."
)


def hook_output(j: Judgment, mode: str) -> dict:
    tips = "; ".join(j.missing) or "be more specific about the outcome you expect"
    if j.outcome == "block" and mode == "block":
        return {
            "decision": "block",
            "reason": (
                f"Prompt coach: this is likely to get a vague result (quality {j.quality:.2f}). "
                f"Please {tips}.\n{TEMPLATE}\n"
                "Edit your prompt above and send it again."
            ),
        }
    if j.outcome in ("coach", "block"):
        return {
            "systemMessage": f"Prompt coach: consider — {tips}. (quality {j.quality:.2f})",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    "Prompt coach: the user's request is underspecified "
                    f"({tips}). Before making substantial changes, ask one concise "
                    "clarifying question that resolves this, unless the codebase makes the intent obvious."
                ),
            },
        }
    return {}


def is_first_prompt(transcript_path: str | None) -> bool:
    if not transcript_path:
        return True
    try:
        p = Path(transcript_path)
        if not p.exists():
            return True
        with p.open() as f:
            for line in f:
                if '"type":"assistant"' in line or '"type": "assistant"' in line:
                    return False
        return True
    except OSError:
        return True


def run_hook() -> int:
    mode = os.environ.get("PROMPT_COACH_MODE", "block").lower()
    if mode == "off":
        return 0
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    prompt = (event.get("prompt") or "").strip()
    if len(prompt) < MIN_CHARS or prompt.startswith("/") or prompt.startswith("!"):
        return 0
    try:
        with TypeSafeClient() as client:
            j = judge(prompt, is_first_prompt(event.get("transcript_path")), client)
    except Exception as e:  # never stall or break a session because the coach failed
        print(f"prompt-coach: skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return 0
    if log := os.environ.get("PROMPT_COACH_LOG"):
        with open(log, "a") as f:
            f.write(json.dumps({"session_id": event.get("session_id"), **asdict(j)}) + "\n")
    out = hook_output(j, mode)
    if out:
        print(json.dumps(out))
    return 0


# ----------------------------------------------------------------------------- cli

FIXTURES = [
    ("fix the bug", True),
    ("it's still broken, can you look at it again?", True),
    ("make the tests pass", True),
    ("yes, go ahead with option 2", False),
    ("refactor auth", True),
    ("why does the login page sometimes show a blank screen after OAuth redirect?", True),
    ("Add a --json flag to examples/pr-risk/pr_risk.py that prints the full report as JSON instead of the table. Keep the exit code behavior the same.", True),
    ("In jevlab/pr.py, dependency_changes() reports the package's own `version` field as a minor bump. Skip that key so pyproject/package.json version bumps don't count. Add a test with a package.json diff that bumps version 1.2.3 -> 1.3.0 and asserts no dependency change is returned.", True),
    ("the function is slow, optimize it", True),
    ("Explain how the localisation pass in pr_risk.py decides which file a rule fired on.", True),
]


def render_table(js: list[Judgment]) -> str:
    rows = ["| outcome | q | kind | goal | done | where | ref | ms | prompt |", "|---|---|---|---|---|---|---|---|---|"]
    for j in js:
        icon = {"pass": "✅ pass", "coach": "💡 coach", "block": "⛔ block"}[j.outcome]
        p = j.prompt if len(j.prompt) <= 70 else j.prompt[:67] + "..."
        rows.append(
            f"| {icon} | {j.quality:.2f} | {j.kind} | {j.goal:.1f} | {j.done:.2f} | {j.where:.2f} | {j.referent:.2f} | {j.ms} | {p} |"
        )
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--try", dest="try_prompt", metavar="PROMPT", help="grade one prompt and show the judgment")
    ap.add_argument("--follow-up", action="store_true", help="with --try: treat as a mid-session prompt, not the first")
    ap.add_argument("--fixtures", action="store_true", help="grade the built-in sample prompts")
    args = ap.parse_args()

    if not (args.try_prompt or args.fixtures):
        return run_hook()

    with TypeSafeClient() as client:
        if args.try_prompt:
            j = judge(args.try_prompt, not args.follow_up, client)
            print(render_table([j]))
            print()
            print(f"missing: {j.missing or '-'}")
            out = hook_output(j, os.environ.get("PROMPT_COACH_MODE", "block").lower())
            print("hook output:", json.dumps(out, indent=2) if out else "(none; prompt passes silently)")
        else:
            js = [judge(p, first, client) for p, first in FIXTURES]
            print(render_table(js))
            tok = sum(j.input_tokens for j in js)
            print(f"\n{len(js)} prompts · {tok:,} input tokens · ${cost_usd(tok):.4f} total · ${cost_usd(tok) / len(js):.5f} per prompt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
