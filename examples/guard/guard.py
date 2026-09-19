"""Guard: a semantic linter as a Claude Code `PreToolUse` hook, judged by Jev.

A regular linter matches patterns. This one matches *meaning*: rules are written in
plain English in `rules.yaml` ("calls console.log instead of the project logger",
"catches everything and silently continues", "hard-codes a secret") and Jev answers
each as a yes/no probability over the text an Edit or Write is about to put in a
file. Code scopes rules by path, thresholds the probabilities, and returns the hook
decision: deny (Claude sees why and fixes it), ask (you confirm), or warn.

  Claude calls Edit/Write ──▶ hook ──▶ rules that match the path? none ──▶ allow (no call)
                                     ──▶ one Jev request, one Noul per rule
                                     ──▶ any deny-rule fires ──▶ deny + reason + fix
                                         any ask-rule fires  ──▶ ask
                                         any warn-rule fires ──▶ allow + additionalContext

The hook only sees tool calls: a heredoc written through Bash bypasses it, as does a
file changed by a script. That is the trade for being able to *reject* the write;
`FileChanged` sees everything but can't block.

Install (this repo's .claude/settings.json):
  hooks.PreToolUse  matcher "Edit|Write"  ->  uv run examples/guard/guard.py

Try it:
  uv run examples/guard/guard.py --try 'console.log("hi")' --path src/app.ts
  uv run examples/guard/guard.py --fixtures

Env:
  GUARD_MODE   enforce (default) | warn (never deny/ask, only annotate) | off
  GUARD_RULES  path to a rules file (default: examples/guard/rules.yaml)
  GUARD_LOG    path; when set, every judgment is appended as JSON for tuning
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv
from typesafe_sdk import Noul, TypeSafeClient

from jevlab.cost import cost_usd

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DEFAULT_RULES = Path(__file__).with_name("rules.yaml")
MAX_CODE_CHARS = 60_000  # ~15k tokens; a Write bigger than this is judged on its head
TIMEOUT_S = 8.0
ACTION_RANK = {"deny": 3, "ask": 2, "warn": 1}


@dataclass
class Rule:
    id: str
    description: str
    action: str
    paths: list[str]
    exclude: list[str]
    criteria: dict | None = None
    fix: str = ""
    fire: float | None = None


@dataclass
class Hit:
    rule: str
    p: float
    action: str
    fix: str


def load_rules(path: Path) -> tuple[list[Rule], float]:
    doc = yaml.safe_load(path.read_text()) or {}
    rules = [
        Rule(
            id=r["id"],
            description=" ".join(str(r["description"]).split()),
            action=r.get("action", "deny"),
            paths=r.get("paths", ["**/*"]),
            exclude=r.get("exclude", []),
            criteria={str(k).lower(): " ".join(str(v).split()) for k, v in (r.get("criteria") or {}).items()} or None,  # YAML reads true:/false: as booleans
            fix=r.get("fix", ""),
            fire=r.get("fire"),
        )
        for r in doc.get("rules", [])
        if r.get("enabled", True)
    ]
    return rules, float(doc.get("default_fire", 0.7))


def _match(path: str, globs: list[str]) -> bool:
    # "**/*.ts" should match "src/app.ts" and "app.ts"; fnmatch's * crosses "/", which is
    # fine here: we want loose, forgiving path scoping, not gitignore semantics.
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.replace("**/", "", 1)) for g in globs)


def applicable(rules: list[Rule], rel_path: str) -> list[Rule]:
    return [r for r in rules if _match(rel_path, r.paths) and not _match(rel_path, r.exclude)]


# ----------------------------------------------------------------------------- judge

LAST_USAGE = {"input_tokens": 0}  # set by judge(); read by the hook log and the CLI

def judge(rules: list[Rule], rel_path: str, new_code: str, old_code: str | None, client: TypeSafeClient, default_fire: float) -> tuple[list[Hit], dict[str, float]]:
    state = {"file": rel_path, "new_code": new_code[:MAX_CODE_CHARS]}
    if old_code:
        state["old_code"] = old_code[:MAX_CODE_CHARS]
    what = "`new_code`, the text about to be written to `file`" + (", replacing `old_code`" if old_code else "")
    questions = {
        r.id: Noul(
            instructions=f"In {what}: {r.description}",
            criteria=r.criteria,
        )
        for r in rules
    }
    resp = client.system_one(state=state, questions=questions, timeout=TIMEOUT_S)
    LAST_USAGE["input_tokens"] = resp.usage.input_tokens
    probs = {r.id: resp.nouls[r.id].noul for r in rules}
    hits = [Hit(r.id, probs[r.id], r.action, r.fix) for r in rules if probs[r.id] >= (r.fire or default_fire)]
    hits.sort(key=lambda h: (-ACTION_RANK[h.action], -h.p))
    return hits, probs


def decide(hits: list[Hit], mode: str) -> dict:
    if not hits:
        return {}
    if mode == "warn":
        hits = [Hit(h.rule, h.p, "warn", h.fix) for h in hits]
    top = hits[0].action
    lines = [f"- {h.rule} (p={h.p:.2f}): {h.fix}" if h.fix else f"- {h.rule} (p={h.p:.2f})" for h in hits]
    body = "\n".join(lines)
    if top == "deny":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"Guard rejected this edit. Rule(s) violated:\n{body}\nRewrite the change so it complies, then retry.",
            }
        }
    if top == "ask":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": f"Guard: possible rule violation\n{body}",
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"Guard note on this edit (not blocking):\n{body}",
        }
    }


# ----------------------------------------------------------------------------- hook

def new_and_old(tool_name: str, tool_input: dict) -> tuple[str, str | None] | None:
    if tool_name == "Write":
        return tool_input.get("content", ""), None
    if tool_name == "Edit":
        return tool_input.get("new_string", ""), tool_input.get("old_string") or None
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits", [])
        return "\n\n".join(e.get("new_string", "") for e in edits), "\n\n".join(e.get("old_string", "") for e in edits) or None
    return None


def run_hook() -> int:
    mode = os.environ.get("GUARD_MODE", "enforce").lower()
    if mode == "off":
        return 0
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    pair = new_and_old(event.get("tool_name", ""), event.get("tool_input") or {})
    if pair is None:
        return 0
    new_code, old_code = pair
    if not new_code.strip():
        return 0
    file_path = (event.get("tool_input") or {}).get("file_path", "").replace("\\", "/")
    cwd = (event.get("cwd") or os.getcwd()).replace("\\", "/")
    rel = file_path[len(cwd) + 1 :] if file_path.startswith(cwd + "/") else file_path.lstrip("/")

    try:
        rules, default_fire = load_rules(Path(os.environ.get("GUARD_RULES", DEFAULT_RULES)))
        todo = applicable(rules, rel)
        if not todo:
            return 0
        t0 = time.perf_counter()
        with TypeSafeClient() as client:
            hits, probs = judge(todo, rel, new_code, old_code, client, default_fire)
    except Exception as e:  # fail open: a broken guard must not stall or block the session
        print(f"guard: skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return 0

    if log := os.environ.get("GUARD_LOG"):
        with open(log, "a") as f:
            f.write(json.dumps({"file": rel, "tool": event.get("tool_name"), "probs": probs, "hits": [asdict(h) for h in hits],
                                "ms": int((time.perf_counter() - t0) * 1000), "input_tokens": LAST_USAGE["input_tokens"],
                                "cost_usd": round(cost_usd(LAST_USAGE["input_tokens"]), 6)}) + "\n")
    out = decide(hits, mode)
    if out:
        print(json.dumps(out))
    return 0


# ----------------------------------------------------------------------------- cli

FIXTURES = [
    ("src/app.ts", 'export function start() {\n  console.log("server started on", port);\n}\n', None),
    ("src/app.ts", 'export function start() {\n  log.info("server started", { port });\n}\n', None),
    ("src/app.ts", '  // console.log("debug") — removed, see logger\n  log.debug("x");\n', None),
    ("src/app.ts", '  console.log("still here");\n  doMore();\n', '  console.log("still here");\n'),
    ("src/lib/logger.ts", 'export const log = {\n  info: (...a: unknown[]) => console.log(...a),\n};\n', None),
    ("src/app.test.ts", 'console.log("in a test");\n', None),
    ("jevlab/pr.py", 'def load(x):\n    print("loading", x)\n    return x\n', None),
    ("examples/triage/triage.py", 'print("--- raw answers ---")\n', None),
    ("app/config.py", 'SMTP_PASSWORD = "Sup3r-S3cret-Pr0d!"\n', None),
    ("app/config.py", 'SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]\n', None),
    ("app/util.py", 'try:\n    sync()\nexcept Exception:\n    pass\n', None),
    ("app/util.py", 'try:\n    sync()\nexcept ConnectionError as e:\n    logger.warning("sync failed: %s", e)\n', None),
    ("app/util.py", '# TODO: handle the retry case\nreturn None\n', None),
    ("app/util.py", '# TODO(#412): handle the retry case\nreturn None\n', None),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--try", dest="try_code", metavar="CODE", help="judge this new code")
    ap.add_argument("--path", default="src/app.ts", help="with --try: the file path it would be written to")
    ap.add_argument("--old", help="with --try: the old text being replaced (Edit)")
    ap.add_argument("--fixtures", action="store_true")
    ap.add_argument("--rules", default=None)
    args = ap.parse_args()
    if not (args.try_code or args.fixtures):
        return run_hook()

    rules, default_fire = load_rules(Path(args.rules or os.environ.get("GUARD_RULES", DEFAULT_RULES)))
    cases = [(args.path, args.try_code, args.old)] if args.try_code else FIXTURES
    total_tokens = 0
    with TypeSafeClient() as client:
        print("| decision | rules asked | fired | ms | file | new code |")
        print("|---|---|---|---|---|---|")
        for path, code, old in cases:
            todo = applicable(rules, path)
            if not todo:
                print(f"| ✅ allow (no rules for path) | – | – | 0 | {path} | `{code.strip().splitlines()[0][:50]}` |")
                continue
            t0 = time.perf_counter()
            hits, probs = judge(todo, path, code, old, client, default_fire)
            ms = int((time.perf_counter() - t0) * 1000)
            total_tokens += LAST_USAGE["input_tokens"]
            out = decide(hits, "enforce")
            dec = out.get("hookSpecificOutput", {}).get("permissionDecision", "allow" if not out else "allow+note")
            icon = {"deny": "⛔ deny", "ask": "❓ ask", "allow+note": "💡 allow+note", "allow": "✅ allow"}[dec]
            asked = ", ".join(f"{r.id}={probs[r.id]:.2f}" for r in todo)
            fired = ", ".join(h.rule for h in hits) or "–"
            first = code.strip().splitlines()[0][:50] if code.strip() else ""
            print(f"| {icon} | {asked} | {fired} | {ms} | {path} | `{first}` |")
    print(f"\n{total_tokens:,} input tokens · ${cost_usd(total_tokens):.4f} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
