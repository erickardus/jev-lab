"""Retro: a Claude Code `SessionEnd` hook that grades the session after it ends.

Every coding session leaves a transcript. This hook reads it and looks at the session
from three angles, so that the next one goes better:

  your prompt    was the opening ask specific, bounded, exemplified, concise?
                 -> educate the developer
  repo context   what did Claude have to be told, ask, or rediscover that CLAUDE.md or
                 a skill should have carried?  -> improve the repo's instructions and skills
  the agent      did it loop, re-read, drift, fail edits, or end on an open question?
                 -> tune the harness, or the model

Code parses the transcript and computes every countable fact: repeated tool calls,
re-reads of the same file, failed edits, how long orientation took before the first
edit, skills available vs used, questions Claude had to ask. Jev answers the judgments
code can't make: is this follow-up a correction, and whose fault was it (the prompt, the
repo's documentation, or the agent)? Was the exploration directed or wandering? Did the
final message deliver what was asked? Would that unused skill have applied?

Optionally one Sonnet call turns the findings into text a person can paste: a rewritten
prompt, a CLAUDE.md paragraph, a sharper skill description. Jev decides *whether* and
*what*; the LLM only writes, and only when there is something to write.

Nothing in the transcript's tool output (file contents, command output) is sent to
either model. Jev sees the prompts, the final message, a trace of tool *names and
targets*, and the skill list. Sonnet sees the findings plus the head of CLAUDE.md.

Install (this repo's .claude/settings.json):
  hooks.SessionEnd   -> uv run examples/retro/retro.py            (writes the report)
  hooks.SessionStart -> uv run examples/retro/retro.py --session-start
                        (tells Claude to mention last session's findings in its first reply)

Try it on a transcript:
  uv run examples/retro/retro.py --last                 # newest transcript for this repo
  uv run examples/retro/retro.py --fixture              # a bundled "lost" session
  uv run examples/retro/retro.py --last --facts-only    # no Jev: just what code can count
  uv run examples/retro/retro.py --last --llm           # add the Sonnet rewrite section
  uv run examples/retro/retro.py --trends               # across every session in the ledger

Env:
  RETRO_MODE   on (default) | off
  RETRO_DIR    where reports and the ledger go (default: <repo>/.claude/retro, git-ignored)
  RETRO_LLM    model id for the writing step, e.g. claude-sonnet-5; unset = no LLM call
  RETRO_CARRY  1 (default) | 0   whether SessionStart surfaces the previous retro
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

from jevlab.cost import cost_usd

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

FIXTURE = Path(__file__).with_name("fixtures") / "lost-session.jsonl"
TIMEOUT_S = 8.0

# --- Policy. Everything below is a starting point; tune on your own sessions. ------------
MAX_PROMPT_CHARS = 4000  # of the first prompt / final message that Jev sees
MAX_TRACE_STEPS = 120  # tool steps in the trace Jev grades (middle is elided)
MAX_FOLLOWUPS = 8  # follow-up prompts classified per session
MAX_SKILLS = 30  # unused skills checked for relevance
SKILL_RELEVANT = 0.7  # p(skill applies) at or above -> finding
CORRECTION = 0.55  # p(follow-up is a correction) at or above -> counts as one
LOST_BELOW = 1.0  # directedness Score (0..2) below this -> "got lost"
ORIENTATION_LONG = 15  # explore steps before the first edit -> "long orientation"
REREAD_AT = 3  # same file read this many times -> re-read finding
CHURN_AT = 3  # same file edited this many times -> churn finding

READ_TOOLS = {
    "cat", "head", "tail", "sed", "ls", "grep", "rg", "find", "wc", "tree", "echo", "pwd", "which", "type",
    "stat", "file", "diff", "env", "printenv", "true", "test", "awk", "sort", "uniq", "cut", "jq", "less", "more",
}
GIT_READ = {"log", "status", "diff", "show", "branch", "blame", "ls-files", "rev-parse", "remote", "tag", "describe"}
MUTATING_RE = re.compile(r"(?<![<>])>{1,2}(?!&)|\bsed\s+-i\b|\btee\b|\brm\b|\bmv\b|\bcp\b|\bmkdir\b|\btouch\b|\bchmod\b")
DEVNULL_RE = re.compile(r"\d?&?>{1,2}\s*/dev/null")


# ----------------------------------------------------------------------------- transcript

@dataclass
class Step:
    n: int
    tool: str
    arg: str  # short, human: "jevlab/pr.py", "pytest -q", '"dependency_changes" jevlab/'
    key: str  # canonical identity, for repeat detection
    kind: str  # explore | edit | run | skill | agent | other
    file: str | None = None
    error: bool = False
    ts: str | None = None


@dataclass
class Prompt:
    n: int
    text: str
    ts: str | None
    prev_assistant: str  # last assistant text before this prompt


@dataclass
class Session:
    id: str
    path: str
    title: str = ""
    cwd: str = ""
    model: str = ""
    started: str | None = None
    ended: str | None = None
    prompts: list[Prompt] = field(default_factory=list)
    final_message: str = ""
    steps: list[Step] = field(default_factory=list)
    skills_available: dict[str, str] = field(default_factory=dict)
    skills_used: list[str] = field(default_factory=list)
    agents_used: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)  # slash commands typed by the user
    assistant_turns: int = 0
    sidechain_lines: int = 0
    output_tokens: int = 0
    input_tokens: int = 0  # uncached + cache writes; what the session actually cost to feed

    @property
    def first_prompt(self) -> str:
        return self.prompts[0].text if self.prompts else ""


_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_NOT_A_PROMPT = ("<command-", "<local-command", "<ide_")  # slash-command echoes and IDE state, not things the user typed


def _rel(path: str | None, cwd: str) -> str | None:
    if not path:
        return None
    p = path.replace("\\", "/")
    if cwd and p.startswith(cwd.rstrip("/") + "/"):
        return p[len(cwd.rstrip("/")) + 1 :]
    return p


def _bash_kind(cmd: str) -> str:
    cmd = DEVNULL_RE.sub("", cmd)
    if MUTATING_RE.search(cmd):
        return "run"
    for seg in re.split(r"&&|\|\||;|\|", cmd):
        words = [w for w in seg.strip().split() if not re.match(r"^\w+=", w)]  # drop VAR=... prefixes
        if not words:
            continue
        if words[0] in ("cd", "export") and len(words) > 1:
            continue
        if words[0] == "git" and len(words) > 1 and words[1] in GIT_READ:
            continue
        if words[0] not in READ_TOOLS:
            return "run"
    return "explore"


def _short(s: str, n: int = 90) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def make_step(n: int, name: str, inp: dict, cwd: str, ts: str | None) -> Step:
    inp = inp or {}
    if name in ("Read", "NotebookRead"):
        f = _rel(inp.get("file_path") or inp.get("notebook_path"), cwd)
        span = f":{inp['offset']}" if inp.get("offset") else ""
        return Step(n, name, f"{f}{span}", f"Read {f}{span}", "explore", f, ts=ts)
    if name == "Grep":
        where = _rel(inp.get("path"), cwd) or inp.get("glob") or "."
        arg = f'"{_short(inp.get("pattern", ""), 40)}" in {where}'
        return Step(n, name, arg, f"Grep {inp.get('pattern')} {where}", "explore", ts=ts)
    if name == "Glob":
        return Step(n, name, str(inp.get("pattern", "")), f"Glob {inp.get('pattern')} {inp.get('path')}", "explore", ts=ts)
    if name in ("WebFetch", "WebSearch"):
        arg = _short(inp.get("url") or inp.get("query") or "", 80)
        return Step(n, name, arg, f"{name} {arg}", "explore", ts=ts)
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        f = _rel(inp.get("file_path") or inp.get("notebook_path"), cwd)
        body = inp.get("old_string") or inp.get("content") or json.dumps(inp.get("edits", ""))
        h = hashlib.sha1(str(body).encode()).hexdigest()[:8]
        return Step(n, name, f or "?", f"{name} {f} {h}", "edit", f, ts=ts)
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        return Step(n, name, _short(cmd), f"Bash {' '.join(cmd.split())}", _bash_kind(cmd), ts=ts)
    if name == "Skill":
        s = str(inp.get("skill", ""))
        return Step(n, name, s, f"Skill {s}", "skill", ts=ts)
    if name in ("Agent", "Task"):
        arg = f"{inp.get('subagent_type', 'agent')}: {_short(inp.get('description', ''), 60)}"
        return Step(n, name, arg, f"{name} {arg}", "agent", ts=ts)
    arg = _short(json.dumps(inp), 80)
    return Step(n, name, arg, f"{name} {arg}", "other", ts=ts)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _skill_listing(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^- ([\w:.-]+): (.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def parse_transcript(path: Path) -> Session:
    s = Session(id=path.stem, path=str(path))
    by_id: dict[str, Step] = {}
    seen_msgs: set[str] = set()
    last_text = ""
    cur_msg_id = None
    cur_text_parts: list[str] = []

    def flush_text():
        nonlocal last_text
        if cur_text_parts:
            last_text = "\n".join(cur_text_parts).strip() or last_text

    with path.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("type")
            if t == "ai-title":
                s.title = d.get("aiTitle") or s.title
                continue
            if t == "attachment":
                a = d.get("attachment") or {}
                if a.get("type") == "skill_listing":
                    s.skills_available.update(_skill_listing(a.get("content", "")))
                continue
            if t not in ("user", "assistant"):
                continue
            if d.get("isSidechain"):
                s.sidechain_lines += 1
                continue
            if not s.cwd and d.get("cwd"):
                s.cwd = d["cwd"].replace("\\", "/")
            ts = d.get("timestamp")
            if ts:
                s.started = s.started or ts
                s.ended = ts
            msg = d.get("message") or {}
            content = msg.get("content")

            if t == "user":
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            st = by_id.get(b.get("tool_use_id", ""))
                            if st and b.get("is_error"):
                                st.error = True
                if d.get("isMeta"):
                    continue
                text = _SYSTEM_REMINDER.sub("", _text_of(content)).strip()
                if not text:
                    continue
                m = re.search(r"<command-name>(.*?)</command-name>", text)
                if m:
                    s.commands.append(m.group(1).strip())
                    continue
                if text.startswith(_NOT_A_PROMPT):
                    continue
                flush_text()
                s.prompts.append(Prompt(len(s.prompts), text, ts, last_text))
                continue

            # assistant: one line per content block; usage repeats per block, count once per message
            mid = msg.get("id") or d.get("uuid")
            if mid != cur_msg_id:
                flush_text()
                cur_msg_id, cur_text_parts = mid, []
            if mid not in seen_msgs:
                seen_msgs.add(mid)
                s.assistant_turns += 1
                u = msg.get("usage") or {}
                s.output_tokens += u.get("output_tokens") or 0
                s.input_tokens += (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
                s.model = msg.get("model") or s.model
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    cur_text_parts.append(b["text"])
                elif b.get("type") == "tool_use":
                    st = make_step(len(s.steps) + 1, b.get("name", "?"), b.get("input") or {}, s.cwd, ts)
                    s.steps.append(st)
                    by_id[b.get("id", "")] = st
                    if st.kind == "skill":
                        s.skills_used.append(st.arg)
                    elif st.kind == "agent":
                        s.agents_used.append(st.arg)
    flush_text()
    s.final_message = last_text
    return s


# ----------------------------------------------------------------------------- facts (code)

def _ends_with_question(text: str) -> str | None:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if lines and lines[-1].rstrip("*_` ").endswith("?"):
        return lines[-1]
    return None


def compute_facts(s: Session) -> dict:
    kinds = Counter(st.kind for st in s.steps)
    tools = Counter(st.tool for st in s.steps)
    repeats = [(k, c) for k, c in Counter(st.key for st in s.steps).items() if c >= 2]
    repeats.sort(key=lambda kc: -kc[1])
    reads = Counter(st.file for st in s.steps if st.tool == "Read" and st.file)
    rereads = [(f, c) for f, c in reads.most_common() if c >= REREAD_AT]
    edits = Counter(st.file for st in s.steps if st.kind == "edit" and st.file)
    churn = [(f, c) for f, c in edits.most_common() if c >= CHURN_AT]
    errors = [st for st in s.steps if st.error]
    edit_failures = [st for st in errors if st.kind == "edit"]
    first_edit = next((i for i, st in enumerate(s.steps) if st.kind == "edit"), None)
    before = s.steps[: first_edit if first_edit is not None else len(s.steps)]
    orientation = sum(1 for st in before if st.kind == "explore")
    run, longest = 0, 0
    for st in s.steps:
        run = run + 1 if st.kind == "explore" else 0
        longest = max(longest, run)
    clarifying = []
    for p in s.prompts[1:]:
        q = _ends_with_question(p.prev_assistant)
        if q:
            clarifying.append({"question": _short(q, 200), "answer": _short(p.text, 200), "prompt_n": p.n})
    dur = None
    if s.started and s.ended:
        try:
            a = datetime.fromisoformat(s.started.replace("Z", "+00:00"))
            b = datetime.fromisoformat(s.ended.replace("Z", "+00:00"))
            dur = round((b - a).total_seconds() / 60, 1)
        except ValueError:
            pass
    label = {st.key: st.arg for st in s.steps}
    return {
        "steps": len(s.steps),
        "by_kind": dict(kinds),
        "by_tool": dict(tools.most_common()),
        "prompts": len(s.prompts),
        "assistant_turns": s.assistant_turns,
        "duration_min": dur,
        "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens,
        "first_prompt_words": len(s.first_prompt.split()),
        "repeats": [{"call": label[k], "times": c} for k, c in repeats[:10]],
        "repeated_calls": sum(c - 1 for _, c in repeats),
        "rereads": [{"file": f, "times": c} for f, c in rereads],
        "churn": [{"file": f, "edits": c} for f, c in churn],
        "errors": len(errors),
        "errors_by_tool": dict(Counter(st.tool for st in errors)),
        "edit_failures": [st.arg for st in edit_failures],
        "orientation_steps": orientation,
        "longest_explore_run": longest,
        "files_read": sorted(reads),
        "files_edited": sorted(edits),
        "skills_used": dict(Counter(s.skills_used)),
        "skills_unused": sorted(set(s.skills_available) - set(s.skills_used)),
        "agents_used": s.agents_used,
        "slash_commands": s.commands,
        "clarifying_questions": clarifying,
        "sidechain_lines": s.sidechain_lines,
        "ended_with_question": bool(_ends_with_question(s.final_message)),
    }


# ----------------------------------------------------------------------------- Jev

def prompt_questions() -> dict:
    return {
        "kind": Choice(
            instructions="What kind of message is `prompt`, the first thing a developer sent to a coding assistant?",
            criteria={
                "task": "Asks the assistant to change, create, fix, refactor, test, or run something",
                "question": "Asks for an explanation, opinion, or information; no change is requested",
                "design": "Asks for options, a plan, a design, or a proposal rather than a finished change",
            },
        ),
        "goal": Score(
            instructions="How specifically does `prompt` state what the developer wants: the change to make, the question to answer, or the thing to design?",
            criteria=[
                "It is not clear what is wanted; the request could mean many different things",
                "The general task or topic is clear but key details are left to guess: which behavior, which case, what should be different",
                "The wanted result is stated specifically enough that two engineers would build, answer, or design the same thing",
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
                "true": "It names a file, function, class, module, component, endpoint, command, error message, or feature, or the request is general and needs no location",
                "false": "It refers to code or a problem without saying which one, and the request is not general",
            },
        ),
        "constraints": Noul(
            instructions="`prompt` states constraints or boundaries on the work",
            criteria={
                "true": "It says what not to change, what must keep working, a style or tool to use, a limit on scope, or a compatibility requirement",
                "false": "It sets no boundaries; anything that achieves the goal would be acceptable as far as the prompt says",
            },
        ),
        "example": Noul(
            instructions="`prompt` includes a concrete example, sample, or artifact",
            criteria={
                "true": "It contains an example input or output, a code snippet, an error message or stack trace, a command that reproduces the problem, or a reference to an existing thing to imitate",
                "false": "It describes the situation only in general words",
            },
        ),
        "concise": Score(
            instructions="How concise is `prompt`? Judge signal per word, not length: a long prompt full of specifics is concise; a short one that repeats itself is not.",
            criteria=[
                "Rambling: repeats itself, hedges, thinks out loud, or includes material irrelevant to the request",
                "Mostly to the point with some filler, hedging, or repetition",
                "Every sentence carries information the assistant needs",
            ],
        ),
        "context": Noul(
            instructions="`prompt` explains the purpose or background behind the request",
            criteria={
                "true": "It says why the change is wanted, who it is for, or what larger goal it serves, so trade-offs can be made in the right direction",
                "false": "It gives the instruction with no reason behind it",
            },
        ),
    }


def followup_questions() -> dict:
    return {
        "kind": Choice(
            instructions="`message` is a developer's follow-up to a coding assistant. `assistant_before` is what the assistant said just before it, and `first_prompt` opened the session. What is `message` doing?",
            criteria={
                "answer": "Answers a question the assistant asked, or supplies information the assistant said it needed",
                "correction": "Tells the assistant that what it did or proposed is wrong, not what was wanted, or against how things are done here, and how to do it instead",
                "scope_change": "Asks for something new or different that the first prompt did not ask for",
                "approval": "Approves, picks an option, or says to continue",
                "other": "None of the above: a question, a comment, a thank-you",
            },
        ),
        "cause": Choice(
            instructions="If `message` corrects or informs the assistant, what best explains why the assistant did not already know this?",
            criteria={
                "prompt_missing_info": "The developer is now supplying a requirement, preference, or detail that `first_prompt` could have stated and did not",
                "repo_knowledge": "It is a fact or convention about this codebase or team (which tool to run, where things live, what not to touch, how things are done here) that a newcomer could not know from the prompt and that should be written down in the repo's instructions",
                "agent_error": "The prompt and the codebase gave enough information; the assistant made a mistake, ignored an instruction, or misread the code",
                "not_applicable": "`message` is not a correction and supplies no new information",
            },
        ),
    }


def trace_questions() -> dict:
    return {
        "directed": Score(
            instructions="`trace` lists, in order, the tools a coding assistant used while working on `prompt` (name, and the file, pattern, or command). How directed was the work?",
            criteria=[
                "Wandering: it searched or read broadly, revisited the same places, or worked in areas unrelated to the prompt before finding its footing",
                "Mostly directed, with some backtracking or repeated inspection",
                "Direct: each step follows from the last toward what the prompt asked, with little repetition",
            ],
        ),
        "redundant": Noul(
            instructions="In `trace`, the assistant re-read files or re-ran searches and commands it had already run, without an edit in between that would change their result",
            criteria={
                "true": "The same read, search, or read-only command appears again with nothing in between that could have changed its output",
                "false": "Repeated steps are explained by an edit in between (re-running tests after a fix, re-reading a file just changed), or there are none",
            },
        ),
        "drift": Noul(
            instructions="Judging from `trace` and `prompt`, the assistant spent effort on files or tasks that `prompt` did not ask for",
            criteria={
                "true": "Edits or sustained investigation in areas unrelated to the request, beyond what is needed to understand or verify it",
                "false": "Every area touched is plausibly needed to do or verify what was asked",
            },
        ),
    }


def outcome_questions() -> dict:
    return {
        "delivered": Score(
            instructions="`final_message` is the last thing a coding assistant said in a session that opened with `prompt`. How much of what `prompt` asked for does `final_message` report as done?",
            criteria=[
                "Little or none: it stopped, gave up, or only asked questions",
                "Part of it: some pieces done, others deferred, skipped, or left broken",
                "All of it, as stated: the message reports the whole request completed",
                "All of it, and it says how it was verified: tests run, output shown, behavior checked",
            ],
        ),
        "unresolved": Noul(
            instructions="`final_message` reports something that is not done, failing, skipped, or unverified",
            criteria={
                "true": "It names a failing test, a step it could not run, something left for later, or a result it did not check",
                "false": "It reports no open problems, or only optional follow-ups the developer did not ask for",
            },
        ),
        "asks": Noul(
            instructions="`final_message` ends by asking the developer a question that needs an answer before work can continue",
            criteria={
                "true": "It asks which option to take, for a decision, or for information it needs, and stops there",
                "false": "It ends with a result, a summary, or an optional offer that needs no answer",
            },
        ),
    }


def skill_questions(skills: dict[str, str]) -> dict:
    return {
        name: Noul(
            instructions=f"`prompt` asks for the kind of work that the skill `{name}` in `skills` says it should be used for",
            criteria={
                "true": "The request matches a situation the skill's description names as a reason to use it",
                "false": "The skill's description is about a different kind of work, or the match is only a shared word",
            },
        )
        for name in skills
    }


def build_trace(s: Session) -> list[str]:
    lines = [f"{st.n}. {st.tool} {st.arg}" + (" [error]" if st.error else "") for st in s.steps]
    if len(lines) > MAX_TRACE_STEPS:
        half = MAX_TRACE_STEPS // 2
        lines = lines[:half] + [f"… {len(lines) - 2 * half} steps elided …"] + lines[-half:]
    return lines


@dataclass
class Judgments:
    prompt: dict = field(default_factory=dict)  # kind, goal, done, where, constraints, example, concise, context
    followups: list[dict] = field(default_factory=list)  # {n, kind, kind_conf, cause, cause_conf, text}
    trace: dict = field(default_factory=dict)  # directed, redundant, drift
    outcome: dict = field(default_factory=dict)  # delivered, unresolved, asks
    skills: dict[str, float] = field(default_factory=dict)  # unused skill -> p(applies)
    requests: int = 0
    input_tokens: int = 0
    ms: int = 0
    errors: list[str] = field(default_factory=list)


def judge(s: Session, facts: dict, client: TypeSafeClient, dump: bool = False) -> Judgments:
    j = Judgments()
    t0 = time.perf_counter()
    prompt = s.first_prompt[:MAX_PROMPT_CHARS]

    def ask(name: str, state, questions):
        if dump:
            print(f"--- state for {name} ---\n{json.dumps(state, indent=1)[:6000]}\n", file=sys.stderr)
        resp = client.system_one(state=state, questions=questions, timeout=TIMEOUT_S)
        j.requests += 1
        j.input_tokens += resp.usage.input_tokens or 0
        return resp

    if prompt:
        try:
            r = ask("prompt", {"prompt": prompt}, prompt_questions())
            k = r.choices["kind"]
            j.prompt = {
                "kind": k.choice, "kind_conf": k.confidence,
                "goal": r.scores["goal"].score, "done": r.nouls["done"].noul, "where": r.nouls["where"].noul,
                "constraints": r.nouls["constraints"].noul, "example": r.nouls["example"].noul,
                "concise": r.scores["concise"].score, "context": r.nouls["context"].noul,
            }
        except Exception as e:
            j.errors.append(f"prompt: {type(e).__name__}: {e}")

    for p in s.prompts[1 : 1 + MAX_FOLLOWUPS]:
        try:
            r = ask(
                f"followup {p.n}",
                {"first_prompt": prompt, "assistant_before": p.prev_assistant[-1500:], "message": p.text[:1500]},
                followup_questions(),
            )
            k, c = r.choices["kind"], r.choices["cause"]
            j.followups.append({
                "n": p.n, "text": _short(p.text, 160), "kind": k.choice, "kind_conf": k.confidence,
                "p_correction": k.probabilities.get("correction", 0.0), "cause": c.choice, "cause_conf": c.confidence,
                "cause_probs": {kk: round(v, 2) for kk, v in c.probabilities.items()},
            })
        except Exception as e:
            j.errors.append(f"followup {p.n}: {type(e).__name__}: {e}")

    if s.steps:
        try:
            r = ask("trace", {"prompt": prompt, "trace": build_trace(s)}, trace_questions())
            j.trace = {"directed": r.scores["directed"].score, "directed_conf": r.scores["directed"].confidence,
                       "redundant": r.nouls["redundant"].noul, "drift": r.nouls["drift"].noul}
        except Exception as e:
            j.errors.append(f"trace: {type(e).__name__}: {e}")

    if s.final_message and prompt:
        try:
            r = ask("outcome", {"prompt": prompt, "final_message": s.final_message[-MAX_PROMPT_CHARS:]}, outcome_questions())
            j.outcome = {"delivered": r.scores["delivered"].score, "delivered_conf": r.scores["delivered"].confidence,
                         "unresolved": r.nouls["unresolved"].noul, "asks": r.nouls["asks"].noul}
        except Exception as e:
            j.errors.append(f"outcome: {type(e).__name__}: {e}")

    unused = {n: _short(s.skills_available[n], 240) for n in facts["skills_unused"][:MAX_SKILLS]}
    if unused and prompt:
        try:
            r = ask("skills", {"prompt": prompt, "skills": unused}, skill_questions(unused))
            j.skills = {n: r.nouls[n].noul for n in unused}
        except Exception as e:
            j.errors.append(f"skills: {type(e).__name__}: {e}")

    j.ms = int((time.perf_counter() - t0) * 1000)
    return j


# ----------------------------------------------------------------------------- findings (policy)

@dataclass
class Finding:
    area: str  # prompt | context | agent
    severity: int  # 1 note, 2 worth fixing, 3 this cost the session
    title: str
    evidence: str
    suggestion: str


def find(s: Session, f: dict, j: Judgments) -> list[Finding]:
    out: list[Finding] = []
    P = j.prompt
    is_task = P.get("kind", "task") != "question"

    # --- your prompt -------------------------------------------------------------------
    if P:
        if P["goal"] < 1.0:
            out.append(Finding("prompt", 3, "The opening prompt did not say what you wanted",
                               f"goal {P['goal']:.1f}/2", "Open with the change or answer you want, stated so two engineers would build the same thing."))
        elif P["goal"] < 1.6:
            out.append(Finding("prompt", 2, "The opening prompt left key details to guess",
                               f"goal {P['goal']:.1f}/2", "Pin down the details: which case, what should differ, what stays the same."))
        if is_task and P["done"] < 0.5:
            out.append(Finding("prompt", 2, "No definition of done",
                               f"done {P['done']:.2f}", "Say how you'll know it worked: a test, an error that disappears, an example of the output."))
        if P["where"] < 0.5:
            out.append(Finding("prompt", 2, "The prompt did not say where",
                               f"where {P['where']:.2f}", "Name the file, function, or feature. Every step of orientation Claude spends finding it is a step you could have skipped."))
        if is_task and P["constraints"] < 0.4 and (f["prompts"] > 1 or j.trace.get("drift", 0) > 0.5):
            out.append(Finding("prompt", 1, "No boundaries were set",
                               f"constraints {P['constraints']:.2f}", "Say what must keep working and what not to touch; it is the cheapest way to prevent scope drift."))
        if P["example"] < 0.4 and (f["errors"] > 2 or f["orientation_steps"] > ORIENTATION_LONG):
            out.append(Finding("prompt", 2, "No example, error text, or reproduction in the prompt",
                               f"example {P['example']:.2f}; {f['orientation_steps']} orientation steps, {f['errors']} tool errors",
                               "Paste the failing command and its output, or a sample input and the output you expect."))
        if P["concise"] < 0.8 and f["first_prompt_words"] > 60:
            out.append(Finding("prompt", 1, "The prompt rambles",
                               f"concise {P['concise']:.1f}/2, {f['first_prompt_words']} words",
                               "Dictated prompts are fine, but end with a 3-line summary: what, where, done-when."))

    for c in f["clarifying_questions"]:
        out.append(Finding("prompt", 2, "Claude had to stop and ask",
                           f"Q: {c['question']}  A: {c['answer']}",
                           "That answer belongs in the opening prompt next time; or, if it is a standing fact about this repo, in CLAUDE.md."))

    # --- follow-ups: corrections and who owes the fix ------------------------------------
    for fu in j.followups:
        if fu["kind"] == "correction" or fu["p_correction"] >= CORRECTION:
            ev = f"follow-up {fu['n']}: “{fu['text']}” (p correction {fu['p_correction']:.2f}; cause {fu['cause']} {fu['cause_conf']:.2f})"
            if fu["cause"] == "repo_knowledge":
                out.append(Finding("context", 3, "A correction carried repo knowledge that is not written down", ev,
                                   "Add this to CLAUDE.md (or the relevant skill) so no session has to be told again."))
            elif fu["cause"] == "prompt_missing_info":
                out.append(Finding("prompt", 2, "A correction supplied something the prompt could have said", ev,
                                   "Put this requirement in the opening prompt; it cost a round trip and the work done before it."))
            elif fu["cause"] == "agent_error":
                out.append(Finding("agent", 2, "Claude was corrected on something it had the information for", ev,
                                   "Check whether an instruction was ignored; if it repeats across sessions, the instruction may need to be more prominent or more literal."))
        elif fu["kind"] == "scope_change":
            out.append(Finding("prompt", 1, "The session changed scope mid-way", f"follow-up {fu['n']}: “{fu['text']}”",
                               "Fine, but a new task in a fresh session keeps the context small and the retro honest."))

    # --- the agent: loops, re-reads, lostness, errors -----------------------------------
    if f["repeated_calls"] >= 2:
        top = "; ".join(f"{r['call']} ×{r['times']}" for r in f["repeats"][:4])
        sev = 2 if j.trace.get("redundant", 0) >= 0.6 else 1
        out.append(Finding("agent", sev, "Identical tool calls were repeated", f"{f['repeated_calls']} repeats: {top}",
                           "Re-runs after an edit are normal; identical reads and searches with nothing in between are context churn."))
    if f["rereads"]:
        top = "; ".join(f"{r['file']} ×{r['times']}" for r in f["rereads"][:4])
        out.append(Finding("context", 2, "The same file was read again and again", top,
                           "If this file is central, a two-line description of it in CLAUDE.md saves a re-read per session."))
    if f["edit_failures"]:
        out.append(Finding("agent", 2, "Edits failed", f"{len(f['edit_failures'])} failed edit(s): {', '.join(f['edit_failures'][:4])}",
                           "Usually a stale view of the file (edited by a command, or read too early). Not the prompt's fault."))
    if f["churn"]:
        top = "; ".join(f"{c['file']} ×{c['edits']}" for c in f["churn"][:3])
        out.append(Finding("agent", 1, "One file was edited many times", top, "Many small edits to one file is often a design decided late; a plan step first may help."))
    if f["errors"] >= 4:
        out.append(Finding("agent", 1, "Many tool errors", f"{f['errors']} errors: {f['errors_by_tool']}", "Look for an environment gap (a missing tool or key) that CLAUDE.md or a SessionStart hook could fix once."))
    if j.trace:
        if j.trace["directed"] < LOST_BELOW:
            out.append(Finding("agent", 3, "Claude got lost", f"directedness {j.trace['directed']:.1f}/2 (confidence {j.trace['directed_conf']:.2f}); "
                               f"{f['orientation_steps']} explore steps before the first edit, longest run {f['longest_explore_run']}",
                               "Give it the entry point: name the file and the function, or add a map of the codebase to CLAUDE.md."))
        if j.trace["drift"] >= 0.6:
            out.append(Finding("agent", 2, "Work drifted outside the request", f"drift {j.trace['drift']:.2f}; edited {', '.join(f['files_edited'][:5])}",
                               "Set boundaries in the prompt; a guard hook can also deny edits outside a path."))
    if f["orientation_steps"] > ORIENTATION_LONG and not any(x.title == "Claude got lost" for x in out):
        out.append(Finding("context", 2, "Long orientation before the first edit",
                           f"{f['orientation_steps']} read/search steps; files: {', '.join(f['files_read'][:6])}",
                           "A short architecture section in CLAUDE.md (what lives where, how to run tests) is read for free at session start."))

    # --- skills ----------------------------------------------------------------------
    for name, p in sorted(j.skills.items(), key=lambda kv: -kv[1]):
        if p >= SKILL_RELEVANT:
            out.append(Finding("context", 2, f"Skill `{name}` matched this task and was not used", f"p {p:.2f}",
                               f"Invoke it with /{name.split(':')[-1]} next time; if Claude should have picked it up on its own, its description needs the words your prompt used."))
    if f["skills_used"]:
        out.append(Finding("context", 1, "Skills used", ", ".join(f"{k} ×{v}" for k, v in f["skills_used"].items()), "Good; the retro can't judge their quality, only that they ran."))

    # --- outcome --------------------------------------------------------------------
    O = j.outcome
    if O:
        if O["delivered"] < 1.5:
            out.append(Finding("agent", 3, "The session ended without delivering the request", f"delivered {O['delivered']:.1f}/3",
                               "Read the final message for what blocked it; the prompt findings above are usually the reason."))
        elif O["delivered"] < 2.5:
            out.append(Finding("agent", 1, "Delivered, but not verified in the final message", f"delivered {O['delivered']:.1f}/3",
                               "Ask for the verification in the prompt: 'run the tests and show the output'."))
        if O["unresolved"] >= 0.6:
            out.append(Finding("agent", 2, "The final message reports something unresolved", f"unresolved {O['unresolved']:.2f}", "Start the next session from that sentence."))
        if O["asks"] >= 0.6:
            out.append(Finding("prompt", 2, "The session ended on a question to you", f"asks {O['asks']:.2f}", "Answer it as the first line of the next prompt."))

    out.sort(key=lambda x: (-x.severity, x.area))
    return out


def scorecard(j: Judgments) -> dict:
    """Three 0..10 numbers for the ledger and the one-line verdict."""
    P, T, O = j.prompt, j.trace, j.outcome
    prompt = None
    if P:
        prompt = 10 * (0.35 * P["goal"] / 2 + 0.15 * P["done"] + 0.15 * P["where"] + 0.1 * P["constraints"]
                       + 0.1 * P["example"] + 0.1 * P["concise"] / 2 + 0.05 * P["context"])
    agent = None
    if T:
        agent = 10 * (0.5 * T["directed"] / 2 + 0.25 * (1 - T["redundant"]) + 0.25 * (1 - T["drift"]))
    outcome = None
    if O:
        outcome = 10 * (0.7 * O["delivered"] / 3 + 0.2 * (1 - O["unresolved"]) + 0.1 * (1 - O["asks"]))
    return {"prompt": prompt, "agent": agent, "outcome": outcome}


# ----------------------------------------------------------------------------- LLM (optional)

LLM_SYSTEM = (
    "You turn findings from a coding-session retrospective into text the developer can paste. "
    "You get the opening prompt, the findings with their evidence, the repo's skill list, and the head of its CLAUDE.md. "
    "Write only what the findings support; do not invent facts about the codebase. "
    "prompt_rewrite: the opening prompt rewritten to fix its findings, keeping the developer's intent and voice, under 120 words; "
    "empty string if the prompt had no findings. "
    "claude_md_additions: paragraphs to add to CLAUDE.md, each with the section heading to put it under and the finding that justifies it; "
    "only for findings in the 'context' area that carry a concrete fact. "
    "skill_changes: for a skill flagged as relevant-but-unused, the sentence to add to its description so it triggers on prompts like this one. "
    "note: two sentences to the developer, plain, no praise."
)

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt_rewrite": {"type": "string"},
        "claude_md_additions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"section": {"type": "string"}, "text": {"type": "string"}, "because": {"type": "string"}},
                "required": ["section", "text", "because"],
                "additionalProperties": False,
            },
        },
        "skill_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"skill": {"type": "string"}, "add_to_description": {"type": "string"}},
                "required": ["skill", "add_to_description"],
                "additionalProperties": False,
            },
        },
        "note": {"type": "string"},
    },
    "required": ["prompt_rewrite", "claude_md_additions", "skill_changes", "note"],
    "additionalProperties": False,
}


def llm_write(s: Session, findings: list[Finding], model: str) -> dict | None:
    import anthropic  # imported here so the hook runs without the package configured

    claude_md = ""
    for cand in (Path(s.cwd or ".") / "CLAUDE.md", Path(s.cwd or ".") / ".claude" / "CLAUDE.md"):
        if cand.exists():
            claude_md = cand.read_text(errors="replace")[:3000]
            break
    flagged = {x.title.split("`")[1] for x in findings if x.title.startswith("Skill `")}
    payload = {
        "opening_prompt": s.first_prompt[:MAX_PROMPT_CHARS],
        "findings": [asdict(x) for x in findings if x.severity >= 2],
        "skills": {k: v for k, v in s.skills_available.items() if k in flagged},
        "claude_md_head": claude_md,
    }
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=LLM_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": LLM_SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    out = json.loads(text)
    out["_usage"] = {"model": model, "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return out


# ----------------------------------------------------------------------------- report

AREA_LABEL = {"prompt": "Your prompt", "context": "Repo context", "agent": "The agent"}
SEV = {3: "🔴", 2: "🟠", 1: "🟡"}


def fmt10(x: float | None) -> str:
    return "–" if x is None else f"{x:.1f}/10"


def render_markdown(s: Session, f: dict, j: Judgments, findings: list[Finding], llm: dict | None) -> str:
    card = scorecard(j)
    L = [f"# Retro: {s.title or s.id[:8]}", ""]
    when = (s.started or "")[:16].replace("T", " ")
    L.append(f"{when} · {f['prompts']} prompt(s) · {f['assistant_turns']} assistant turns · {f['steps']} tool steps"
             + (f" · {f['duration_min']} min" if f["duration_min"] is not None else "") + f" · {s.model}")
    L.append("")
    L.append(f"**Prompt {fmt10(card['prompt'])} · Agent {fmt10(card['agent'])} · Outcome {fmt10(card['outcome'])}**")
    L.append("")
    L.append(f"> {_short(s.first_prompt, 300)}")
    L.append("")
    for area in ("prompt", "context", "agent"):
        items = [x for x in findings if x.area == area]
        L.append(f"## {AREA_LABEL[area]}")
        L.append("")
        if not items:
            L.append("Nothing to report.")
        for x in items:
            L.append(f"- {SEV[x.severity]} **{x.title}** — {x.evidence}")
            L.append(f"  - {x.suggestion}")
        L.append("")
    if llm:
        L.append("## Next time, paste this")
        L.append("")
        if llm.get("prompt_rewrite"):
            L.append("**Prompt**")
            L.append("")
            L.append("```")
            L.append(llm["prompt_rewrite"].strip())
            L.append("```")
            L.append("")
        for add in llm.get("claude_md_additions", []):
            L.append(f"**CLAUDE.md → {add['section']}** (because: {add['because']})")
            L.append("")
            L.append("```")
            L.append(add["text"].strip())
            L.append("```")
            L.append("")
        for ch in llm.get("skill_changes", []):
            L.append(f"**Skill `{ch['skill']}` description, add:** {ch['add_to_description']}")
            L.append("")
        if llm.get("note"):
            L.append(llm["note"].strip())
            L.append("")
    L.append("## Facts")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| tool steps | {f['steps']} ({', '.join(f'{k} {v}' for k, v in f['by_kind'].items())}) |")
    L.append(f"| orientation before first edit | {f['orientation_steps']} explore steps (longest run {f['longest_explore_run']}) |")
    L.append(f"| repeated identical calls | {f['repeated_calls']} |")
    L.append(f"| tool errors | {f['errors']} {f['errors_by_tool'] or ''} |")
    L.append(f"| files read / edited | {len(f['files_read'])} / {len(f['files_edited'])} |")
    L.append(f"| skills used / available | {sum(f['skills_used'].values())} / {len(s.skills_available)} |")
    L.append(f"| subagents | {len(f['agents_used'])} |")
    L.append(f"| tokens fed / generated | {f['input_tokens']:,} / {f['output_tokens']:,} |")
    if j.prompt:
        P = j.prompt
        L.append(f"| prompt rubric | goal {P['goal']:.1f}/2 · done {P['done']:.2f} · where {P['where']:.2f} · constraints {P['constraints']:.2f} · example {P['example']:.2f} · concise {P['concise']:.1f}/2 · context {P['context']:.2f} |")
    if j.trace:
        L.append(f"| trace | directed {j.trace['directed']:.1f}/2 · redundant {j.trace['redundant']:.2f} · drift {j.trace['drift']:.2f} |")
    if j.outcome:
        L.append(f"| outcome | delivered {j.outcome['delivered']:.1f}/3 · unresolved {j.outcome['unresolved']:.2f} · asks {j.outcome['asks']:.2f} |")
    if j.followups:
        L.append(f"| follow-ups | {', '.join(f'{x['n']}:{x['kind']}' + (f'/{x['cause']}' if x['kind'] == 'correction' else '') for x in j.followups)} |")
    jev = f"Jev: {j.requests} request(s) · {j.input_tokens:,} input tokens · ${cost_usd(j.input_tokens):.4f} · {j.ms} ms"
    if llm and llm.get("_usage"):
        u = llm["_usage"]
        jev += f" · LLM: {u['model']} {u['input_tokens']:,} in / {u['output_tokens']:,} out"
    L.append(f"| cost of this retro | {jev} |")
    if j.errors:
        L.append(f"| jev errors | {'; '.join(j.errors)} |")
    L.append("")
    return "\n".join(L)


def ledger_row(s: Session, f: dict, j: Judgments, findings: list[Finding]) -> dict:
    causes = Counter(x["cause"] for x in j.followups if x["kind"] == "correction" or x["p_correction"] >= CORRECTION)
    return {
        "ts": s.started, "session": s.id, "title": s.title, "cwd": s.cwd,
        "scores": scorecard(j), "rubric": j.prompt, "trace": j.trace, "outcome": j.outcome,
        "corrections": dict(causes), "clarifying": len(f["clarifying_questions"]),
        "repeated_calls": f["repeated_calls"], "rereads": [r["file"] for r in f["rereads"]],
        "files_read": f["files_read"], "orientation": f["orientation_steps"], "errors": f["errors"],
        "skills_used": f["skills_used"], "skills_flagged": [k for k, v in j.skills.items() if v >= SKILL_RELEVANT],
        "findings": [{"area": x.area, "severity": x.severity, "title": x.title} for x in findings],
        "prompt_words": f["first_prompt_words"], "steps": f["steps"], "jev_tokens": j.input_tokens,
    }


# ----------------------------------------------------------------------------- run

def retro_dir(cwd: str | None = None) -> Path:
    if os.environ.get("RETRO_DIR"):
        return Path(os.environ["RETRO_DIR"]).expanduser()
    base = Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd or ROOT)
    return base / ".claude" / "retro"


def run(path: Path, facts_only: bool = False, llm: str | None = None, dump: bool = False):
    s = parse_transcript(path)
    f = compute_facts(s)
    j = Judgments()
    if not facts_only:
        with TypeSafeClient() as client:
            j = judge(s, f, client, dump=dump)
    findings = find(s, f, j)
    out = None
    if llm and findings:
        try:
            out = llm_write(s, findings, llm)
        except Exception as e:  # the LLM section is a bonus; never lose the report over it
            j.errors.append(f"llm: {type(e).__name__}: {e}")
    return s, f, j, findings, out


def save(s: Session, f: dict, j: Judgments, findings: list[Finding], llm: dict | None, where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    stamp = (s.started or datetime.now(timezone.utc).isoformat())[:16].replace(":", "").replace("T", "-")
    md = where / f"{stamp}-{s.id[:8]}.md"
    md.write_text(render_markdown(s, f, j, findings, llm), encoding="utf-8")
    row = ledger_row(s, f, j, findings)
    with (where / "ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    (where / "latest.json").write_text(json.dumps({
        "report": str(md), "session": s.id, "title": s.title, "cwd": s.cwd, "unread": True,
        "scores": row["scores"],
        "top": [{"area": x.area, "severity": x.severity, "title": x.title, "suggestion": x.suggestion} for x in findings if x.severity >= 2][:4],
        "llm_note": (llm or {}).get("note", ""),
    }, indent=1), encoding="utf-8")
    return md


# ----------------------------------------------------------------------------- hooks

def hook_session_end() -> int:
    if os.environ.get("RETRO_MODE", "on").lower() == "off":
        return 0
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tp = event.get("transcript_path")
    if not tp or not Path(tp).exists():
        return 0
    try:
        s = parse_transcript(Path(tp))
        if s.assistant_turns == 0 or not s.prompts:
            return 0
        f = compute_facts(s)
        with TypeSafeClient() as client:
            j = judge(s, f, client)
        findings = find(s, f, j)
        llm = None
        if (model := os.environ.get("RETRO_LLM")) and findings:
            try:
                llm = llm_write(s, findings, model)
            except Exception as e:
                j.errors.append(f"llm: {type(e).__name__}: {e}")
        md = save(s, f, j, findings, llm, retro_dir(event.get("cwd")))
    except Exception as e:  # fail open: a retro must never break a session's exit
        print(f"retro: skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return 0
    print(json.dumps({"systemMessage": f"retro written: {md}"}))
    return 0


def hook_session_start() -> int:
    if os.environ.get("RETRO_MODE", "on").lower() == "off" or os.environ.get("RETRO_CARRY", "1") == "0":
        return 0
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        event = {}
    latest = retro_dir(event.get("cwd")) / "latest.json"
    if not latest.exists():
        return 0
    try:
        info = json.loads(latest.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    if not info.get("unread") or not info.get("top"):
        return 0
    info["unread"] = False
    latest.write_text(json.dumps(info, indent=1))
    lines = [f"- [{x['area']}] {x['title']}: {x['suggestion']}" for x in info["top"]]
    sc = info.get("scores") or {}
    ctx = (
        f"Retro of the previous session in this repo (“{info.get('title', '')}”; prompt {fmt10(sc.get('prompt'))}, "
        f"agent {fmt10(sc.get('agent'))}, outcome {fmt10(sc.get('outcome'))}). Top findings:\n" + "\n".join(lines)
        + (f"\nNote: {info['llm_note']}" if info.get("llm_note") else "")
        + f"\nFull report: {info['report']}\n"
        "Before starting on the user's request, tell them in at most three plain lines what the retro found and what "
        "one thing would help most this session (a prompt habit, a CLAUDE.md addition, or a skill to invoke). "
        "Offer to apply any CLAUDE.md or skill change; do not apply it unasked. Then proceed with their request."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))
    return 0


# ----------------------------------------------------------------------------- cli

def latest_transcript(cwd: Path) -> Path | None:
    enc = re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
    d = Path.home() / ".claude" / "projects" / enc
    if not d.exists():
        return None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def trends(where: Path, last: int) -> int:
    ledger = where / "ledger.jsonl"
    if not ledger.exists():
        print(f"no ledger at {ledger}")
        return 1
    rows = [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()][-last:]
    n = len(rows)
    print(f"{n} session(s) in {ledger}\n")

    def mean(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    print("Scores (mean)")
    for k in ("prompt", "agent", "outcome"):
        print(f"  {k:8} {fmt10(mean(r['scores'].get(k) for r in rows))}")
    dims = ["goal", "done", "where", "constraints", "example", "concise", "context"]
    rub = {d: mean((r.get("rubric") or {}).get(d) for r in rows) for d in dims}
    scale = {"goal": 2, "concise": 2}
    norm = {d: (v / scale.get(d, 1) if v is not None else None) for d, v in rub.items()}
    print("\nPrompt rubric (mean, normalised 0..1); lowest first")
    for d, v in sorted(norm.items(), key=lambda kv: (kv[1] is None, kv[1])):
        print(f"  {d:12} {'–' if v is None else f'{v:.2f}'}")
    causes = Counter()
    for r in rows:
        causes.update(r.get("corrections") or {})
    print(f"\nCorrections by cause: {dict(causes) or 'none'}   clarifying questions: {sum(r.get('clarifying', 0) for r in rows)}")
    files = Counter()
    for r in rows:
        files.update(set(r.get("files_read") or []))
    hot = [(f_, c) for f_, c in files.most_common(8) if c >= 2]
    print("\nFiles read in 2+ sessions (candidates for a CLAUDE.md map)")
    for f_, c in hot:
        print(f"  {c:2}× {f_}")
    if not hot:
        print("  none")
    flagged = Counter()
    for r in rows:
        flagged.update(r.get("skills_flagged") or [])
    print(f"\nSkills flagged relevant-but-unused: {dict(flagged) or 'none'}")
    areas = Counter()
    for r in rows:
        areas.update(x["area"] for x in r.get("findings", []) if x["severity"] >= 2)
    print(f"Findings (severity ≥ 2) by area: {dict(areas) or 'none'}")
    print(f"\nMean orientation {mean(r.get('orientation') for r in rows):.1f} steps · repeats {mean(r.get('repeated_calls') for r in rows):.1f} · errors {mean(r.get('errors') for r in rows):.1f} per session")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transcript", metavar="PATH", help="a Claude Code transcript .jsonl")
    ap.add_argument("--last", action="store_true", help="the newest transcript for the current directory")
    ap.add_argument("--fixture", action="store_true", help="the bundled sample session")
    ap.add_argument("--facts-only", action="store_true", help="skip Jev; only what code can count")
    ap.add_argument("--llm", nargs="?", const=os.environ.get("RETRO_LLM") or "claude-sonnet-5", metavar="MODEL",
                    help="add the Sonnet-written section (default model: claude-sonnet-5, or $RETRO_LLM)")
    ap.add_argument("--json", action="store_true", help="print the ledger row and findings as JSON")
    ap.add_argument("--save", action="store_true", help="also write the report and ledger like the hook does")
    ap.add_argument("--dump-state", action="store_true", help="print what Jev sees, to stderr")
    ap.add_argument("--trends", nargs="?", const=20, type=int, metavar="N", help="summarise the last N sessions in the ledger")
    ap.add_argument("--session-start", action="store_true", help="(hook) surface the previous retro")
    args = ap.parse_args()

    if args.session_start:
        return hook_session_start()
    if args.trends is not None:
        return trends(retro_dir(), args.trends)
    if not (args.transcript or args.last or args.fixture):
        return hook_session_end()

    if args.fixture:
        path = FIXTURE
    elif args.transcript:
        path = Path(args.transcript).expanduser()
    else:
        path = latest_transcript(Path.cwd())
        if not path:
            print("no transcript found for this directory under ~/.claude/projects", file=sys.stderr)
            return 2
    if not path.exists():
        print(f"not found: {path}", file=sys.stderr)
        return 2

    s, f, j, findings, llm = run(path, facts_only=args.facts_only, llm=args.llm, dump=args.dump_state)
    if args.json:
        print(json.dumps({"ledger": ledger_row(s, f, j, findings), "findings": [asdict(x) for x in findings],
                          "facts": f, "llm": llm}, indent=1, ensure_ascii=False))
    else:
        print(render_markdown(s, f, j, findings, llm))
    if args.save:
        md = save(s, f, j, findings, llm, retro_dir(s.cwd))
        print(f"\nsaved {md}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
