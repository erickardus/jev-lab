"""Find code that writes personal data (PII) somewhere it shouldn't, judged by Jev.

Storing a user's email in the users table is the product. Logging it, sending it to
analytics, or posting it to a third party is usually a leak. This tool looks at each
change (a PR, a diff, or whole files), asks Jev where personal data flows, and applies a
policy in code: which sinks are fine, which are not, and how much protection (hashing,
masking, encryption) changes that.

Pipeline (code owns the workflow, Jev makes the calls):

  units   ── hunks of a diff, or ~40-line windows of a file; noise/binaries dropped
  stage 1 ── per unit, one request: does personal data flow into each SINK, which
             CATEGORIES of personal data, is it PROTECTED, is it SYNTHETIC test data
  stage 2 ── for units that fire: one Noul per line, "this line writes personal data",
             to point at the exact lines
  policy  ── severity = sink x protection x category, in code; test files downgraded

Jev reads each unit in isolation. It sees `logger.info(f"{user.email} logged in")`; it
does not see a payload assembled two functions away, so treat "clean" as "nothing found
in the changed lines", not "no PII leaves this program".

Run:
  uv run examples/pii-writes/pii_writes.py 123 [--repo owner/name]
  uv run examples/pii-writes/pii_writes.py --diff-file f.diff
  uv run examples/pii-writes/pii_writes.py path/to/file.py another/dir/     # whole files
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, Noul

from jevlab.cost import cost_usd, format_usage
from jevlab.pr import FileDiff, PullRequest, PullRequestError, estimate_tokens, load_pr, parse_unified_diff

load_dotenv()

# --- Policy. Starting points; tune on your own code. -------------------------------------
FIRES = 0.6  # p(yes) for a sink / category / line to count
PROTECTED = 0.7  # p(yes) that the data is hashed/masked/encrypted before the write
FIRES_IF_PROTECTED = 0.4  # lower sink bar for lines whose protection is clear (reported as LOW)
SYNTHETIC = 0.7  # p(yes) that the data is fixture/placeholder, not real users
WINDOW_LINES = 40  # file mode: window size
WINDOW_OVERLAP = 8
MAX_LINES_FOR_LOCALIZE = 80  # stage 2 asks one Noul per line; skip localization above this
CONCURRENCY = 8

# Where the data goes. Severity when UNPROTECTED personal data reaches the sink.
SINKS: dict[str, tuple[str, str]] = {
    # id: (severity, what the Noul asks)
    "log": ("high", "a log statement, print/console output, or debug output"),
    "telemetry": ("high", "analytics, metrics, product tracking, or error/crash reporting (e.g. Sentry, Segment, Mixpanel, Datadog)"),
    "third_party": ("high", "an HTTP request or SDK call to an external service or API that is not the application's own database"),
    "file": ("medium", "a file on disk, an export, a CSV/JSON dump, or object storage"),
    "store": ("info", "the application's own database, cache, or message queue"),
    "response": ("info", "an HTTP response body, API payload, or rendered page returned to a client"),
}
# Which kind of personal data. Categories marked sensitive raise "info"/"medium" to "medium"/"high"
# when written unprotected, even to the application's own store.
CATEGORIES: dict[str, tuple[bool, str]] = {
    # id: (sensitive, description)
    "email": (False, "an email address"),
    "name": (False, "a person's name"),
    "phone": (False, "a phone number"),
    "address": (False, "a postal or street address"),
    "gov_id": (True, "a government identifier: SSN, national ID, passport, tax ID, driver's license number"),
    "financial": (True, "financial data: card number, bank account, IBAN, salary, credit score"),
    "health": (True, "health or medical information, or biometric data"),
    "network": (False, "an IP address, device identifier, or precise geolocation"),
    "dob": (True, "date of birth or exact age"),
    "credentials": (True, "a password, session token, API key, or other secret belonging to a user"),
}
SEVERITY_ORDER = {"none": 0, "info": 1, "low": 2, "medium": 3, "high": 4}


@dataclass
class Unit:
    file: str
    lines: list[tuple[int, str]]  # (line number, text with +/-/space marker in diff mode)
    is_test: bool
    kind: str  # hunk | window

    @property
    def id(self) -> str:
        return f"{self.file}:{self.lines[0][0]}-{self.lines[-1][0]}"

    def state_lines(self) -> dict[str, str]:
        return {str(n): t for n, t in self.lines}

    def text(self) -> str:
        return "\n".join(t for _, t in self.lines)


@dataclass
class Finding:
    unit_id: str
    file: str
    lines: list[int]
    sinks: list[str]  # sinks that fired on these lines
    sink_p: dict[str, float]
    categories: list[str]  # categories seen in the surrounding unit (hunk/window)
    sensitive: float
    protected: float
    synthetic: float
    is_test: bool
    severity: str
    reason: str
    excerpt: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------- units

def units_from_files(files: list[FileDiff]) -> list[Unit]:
    out: list[Unit] = []
    for f in files:
        for h in f.hunks:
            lines: list[tuple[int, str]] = []
            removed_n = 0
            for n, marker, text in h.numbered_lines():
                if n is None:
                    # keep removed lines for context but give them a non-line key
                    removed_n += 1
                    lines.append((-removed_n, f"-{text}"))
                else:
                    lines.append((n, f"{marker}{text}"))
            if any(t.startswith("+") for _, t in lines):
                out.append(Unit(file=f.path, lines=lines, is_test=f.is_test, kind="hunk"))
    return out


def units_from_paths(paths: list[str]) -> list[Unit]:
    out: list[Unit] = []
    code_ext = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rb", ".java", ".kt", ".php", ".cs", ".rs", ".swift", ".scala", ".sql"}
    files: list[Path] = []
    for p in paths:
        pp = Path(p)
        files += [x for x in pp.rglob("*") if x.is_file()] if pp.is_dir() else [pp]
    for fp in files:
        if fp.suffix not in code_ext or "node_modules" in fp.parts or ".venv" in fp.parts:
            continue
        try:
            src = fp.read_text().splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        fd = FileDiff(path=str(fp), old_path=None, status="modified")
        start = 0
        while start < len(src):
            chunk = src[start : start + WINDOW_LINES]
            out.append(Unit(file=str(fp), lines=[(start + i + 1, " " + t) for i, t in enumerate(chunk)], is_test=fd.is_test, kind="window"))
            if start + WINDOW_LINES >= len(src):
                break
            start += WINDOW_LINES - WINDOW_OVERLAP
    return out


# ----------------------------------------------------------------------------- questions

PII_DEF = (
    "personal data (information about an identifiable person: name, email, phone, address, "
    "government ID, date of birth, financial, health, biometric, IP address, device ID, "
    "location, or a user's credentials)"
)


def unit_questions(u: Unit) -> dict[str, Noul]:
    added = "added (+) or present" if u.kind == "hunk" else "present"
    qs: dict[str, Noul] = {}
    for sid, (_, what) in SINKS.items():
        qs[f"sink|{sid}"] = Noul(
            instructions=f"In the {added} lines of `code`, {PII_DEF} is written to {what}",
            criteria={
                "true": "A value that is or contains personal data, such as a field named email/name/phone/ssn or a user object, is passed into that kind of write, directly or inside a formatted string, dict, or object",
                "false": "No such write in the lines, or the values written are not personal data (ids, counts, timestamps, status, internal names)",
            },
        )
    for cid, (_, desc) in CATEGORIES.items():
        qs[f"cat|{cid}"] = Noul(
            instructions=f"In the {added} lines of `code`, a value that is {desc} is written, logged, sent, or stored",
            criteria={
                "true": "A variable, field, or literal clearly holding that kind of data is part of what gets written",
                "false": "That kind of data is not part of anything written in these lines",
            },
        )
    qs["protected"] = Noul(
        instructions=f"In the {added} lines of `code`, personal data is hashed, masked, truncated, encrypted, tokenized, or redacted before being written",
        criteria={
            "true": "The written value is the output of a hash, mask, redact, encrypt, or truncate step applied to the personal data",
            "false": "The personal data is written as-is, or nothing personal is written",
        },
    )
    qs["synthetic"] = Noul(
        instructions=f"The personal data in the {added} lines of `code` is a fixture, placeholder, or example value rather than real user data",
        criteria={
            "true": "Hard-coded example values like test@example.com, John Doe, 555-0100, or clearly fake IDs, used in tests or documentation",
            "false": "The data comes from variables, requests, database records, or user input, or there is no personal data",
        },
    )
    return qs


def line_questions(u: Unit, sinks: list[str]) -> dict[str, Noul]:
    """Stage 2: per candidate line, one Noul per fired sink plus sensitivity and protection.

    Sink and severity are decided per line so that `email -> log` and `ssn -> database`
    in the same hunk become two findings, not one blurred one.
    """
    qs: dict[str, Noul] = {}
    for n, t in u.lines:
        if n <= 0 or not (u.kind == "window" or t.startswith("+")):
            continue
        for sid in sinks:
            qs[f"{n}|sink|{sid}"] = Noul(
                instructions=f"`code.{n}` writes {PII_DEF} to {SINKS[sid][1]}, or is part of the literal message, payload, or object passed to such a write",
                criteria={
                    "true": "This line is the write call itself, or a line of the dict/object/string literal that is handed to that write and contains personal data",
                    "false": "This line only reads input into a variable, validates, branches, imports, or writes to a different kind of destination",
                },
            )
        qs[f"{n}|sensitive"] = Noul(
            instructions=f"The personal data written or assembled on `code.{n}` includes a government ID, financial data (card, account, salary), health or biometric data, date of birth, or a user's password/token/secret",
            criteria={
                "true": "A value of one of those kinds is part of what this line writes or assembles",
                "false": "Only less sensitive data (email, name, phone, address, IP) or no personal data",
            },
        )
        qs[f"{n}|protected"] = Noul(
            instructions=f"On `code.{n}`, the personal data being written is hashed, masked, truncated, encrypted, tokenized, or redacted before it is written",
            criteria={
                "true": "The written value is the output of a hash/mask/redact/encrypt/truncate step applied to the personal data",
                "false": "The personal data is written as-is, or the line writes no personal data",
            },
        )
    return qs


# ----------------------------------------------------------------------------- pipeline

def severity_for(sinks: list[str], sensitive: float, protected: float, synthetic: float, is_test: bool) -> tuple[str, str]:
    """Severity = sink x protection x sensitivity, for one group of lines. Policy, in code."""
    sev = max((SINKS[s][0] for s in sinks), key=lambda x: SEVERITY_ORDER[x])
    bits = ["/".join(sinks)]
    if sensitive >= FIRES and SEVERITY_ORDER[sev] < SEVERITY_ORDER["high"]:
        sev = "high" if sev == "medium" else "medium"
        bits.append("sensitive data (ID/financial/health/DOB/credentials)")
    if protected >= PROTECTED:
        sev = "low" if SEVERITY_ORDER[sev] >= SEVERITY_ORDER["medium"] else "info"
        bits.append("protected before write")
    if synthetic >= SYNTHETIC or is_test:
        sev = "info"
        bits.append("fixture data" if synthetic >= SYNTHETIC else "test file")
    return sev, "; ".join(bits)


def group_lines(per_line: dict[int, dict], text_of: dict[int, str]) -> list[list[int]]:
    """A firing line joins the previous finding when it continues the same statement:
    indented deeper than the statement's first line, with no blank line in between."""

    def indent(n: int) -> int:
        t = text_of.get(n, "")
        return len(t) - len(t.lstrip())

    groups: list[list[int]] = []
    for n in sorted(per_line):
        if groups:
            start, last = groups[-1][0], groups[-1][-1]
            between_blank = any(not text_of.get(k, "").strip() for k in range(last + 1, n))
            opened = sum(text_of.get(start, "").count(c) for c in "([{") - sum(text_of.get(start, "").count(c) for c in ")]}")
            if not between_blank and indent(n) > indent(start) and opened > 0:
                groups[-1].append(n)
                continue
        groups.append([n])
    return groups


USAGE = {"requests": 0, "input": 0, "seconds": 0.0}  # filled by scan(); printed in the footer


async def scan(units: list[Unit], model: str | None, verbose: bool) -> list[Finding]:
    t0 = time.perf_counter()
    sem = asyncio.Semaphore(CONCURRENCY)
    usage = {"input": 0, "requests": 0}
    async with AsyncTypeSafeClient(model=model) as client:

        async def ask(state, questions):
            async with sem:
                resp = await client.system_one(state=state, questions=questions)
            usage["input"] += resp.usage.input_tokens
            usage["requests"] += 1
            return resp

        # Stage 1: every unit in parallel.
        s1 = await asyncio.gather(*(ask({"file": u.file, "code": u.state_lines()}, unit_questions(u)) for u in units))

        findings: list[Finding] = []
        to_localize: list[tuple[Unit, dict, list[str], list[str]]] = []
        for u, resp in zip(units, s1):
            sinks = {k.split("|")[1]: a.noul for k, a in resp.nouls.items() if k.startswith("sink|")}
            cats = {k.split("|")[1]: a.noul for k, a in resp.nouls.items() if k.startswith("cat|")}
            meta = {"protected": resp.nouls["protected"].noul, "synthetic": resp.nouls["synthetic"].noul}
            if verbose:
                top_s = ", ".join(f"{k}={v:.2f}" for k, v in sorted(sinks.items(), key=lambda kv: -kv[1])[:3])
                top_c = ", ".join(f"{k}={v:.2f}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])[:3])
                print(f"[stage 1] {u.id:<44} sinks: {top_s} | cats: {top_c} | prot={meta['protected']:.2f} synth={meta['synthetic']:.2f}", file=sys.stderr)
            fired_sinks = [k for k, p in sinks.items() if p >= FIRES]
            fired_cats = [k for k, p in cats.items() if p >= FIRES]
            if not fired_sinks or not fired_cats:
                continue
            if len(u.lines) > MAX_LINES_FOR_LOCALIZE:
                sev, reason = severity_for(fired_sinks, max(cats[c] for c in fired_cats if CATEGORIES[c][0]) if any(CATEGORIES[c][0] for c in fired_cats) else 0.0, meta["protected"], meta["synthetic"], u.is_test)
                findings.append(Finding(u.id, u.file, [], fired_sinks, sinks, fired_cats, 0.0, meta["protected"], meta["synthetic"], u.is_test, sev, reason + "; unit too large to localize"))
                continue
            to_localize.append((u, meta, fired_sinks, fired_cats))

        # Stage 2: per line, which sink, how sensitive, protected? Only for units that fired.
        s2 = await asyncio.gather(*(ask({"file": u.file, "code": u.state_lines()}, line_questions(u, fired)) for u, _, fired, _ in to_localize))
        for (u, meta, fired_sinks, fired_cats), resp in zip(to_localize, s2):
            per_line: dict[int, dict] = {}
            for key, a in resp.nouls.items():
                n, kind, *rest = key.split("|")
                d = per_line.setdefault(int(n), {"sinks": {}, "sensitive": 0.0, "protected": 0.0})
                if kind == "sink":
                    d["sinks"][rest[0]] = a.noul
                else:
                    d[kind] = a.noul
            if verbose:
                for n, d in sorted(per_line.items()):
                    if max(d["sinks"].values(), default=0) >= 0.3:
                        ps = " ".join(f"{k}={v:.2f}" for k, v in d["sinks"].items())
                        print(f"[stage 2] {u.file}:{n:<5} {ps} sens={d['sensitive']:.2f} prot={d['protected']:.2f}", file=sys.stderr)
            # A hashed/masked write is only half "personal data", and Jev's sink
            # probability hovers around 0.5 for it. Protection itself is unambiguous, so
            # when it is present the sink bar drops and the line is reported as LOW.
            def fires(d: dict) -> tuple[str, ...]:
                bar = FIRES_IF_PROTECTED if d["protected"] >= PROTECTED else FIRES
                return tuple(sorted(s for s, p in d["sinks"].items() if p >= bar))

            hot = {n: {**d, "sinks": fires(d)} for n, d in per_line.items()}
            text_of = {n: t[1:] for n, t in u.lines}  # strip the +/-/space marker, keep indentation
            hot = {n: d for n, d in hot.items() if d["sinks"] and re.sub(r"[\s)\]};,]", "", text_of.get(n, "")) != ""}
            if not hot:  # stage 1 fired but no line cleared the bar: report the unit, best line
                best = max(per_line.items(), key=lambda kv: max(kv[1]["sinks"].values(), default=0))
                sev, reason = severity_for(fired_sinks, best[1]["sensitive"], best[1]["protected"], meta["synthetic"], u.is_test)
                findings.append(Finding(u.id, u.file, [best[0]], fired_sinks, best[1]["sinks"], fired_cats, best[1]["sensitive"], best[1]["protected"], meta["synthetic"], u.is_test, sev, reason + "; low line confidence",
                                        [f"{n:>5}: {t}" for n, t in u.lines if n == best[0]]))
                continue
            for lines in group_lines(hot, text_of):
                group_sinks = sorted({s for n in lines for s in hot[n]["sinks"]})
                sensitive = max(hot[n]["sensitive"] for n in lines)
                protected = min(hot[n]["protected"] for n in lines)
                sev, reason = severity_for(group_sinks, sensitive, protected, meta["synthetic"], u.is_test)
                findings.append(Finding(
                    u.id, u.file, lines, group_sinks, {s: max(per_line[n]["sinks"].get(s, 0) for n in lines) for s in group_sinks},
                    fired_cats, sensitive, protected, meta["synthetic"], u.is_test, sev, reason,
                    [f"{n:>5}: {t}" for n, t in u.lines if n in lines],
                ))

    USAGE.update(requests=usage["requests"], input=usage["input"], seconds=time.perf_counter() - t0)
    return sorted(findings, key=lambda f: (-SEVERITY_ORDER[f.severity], f.file, f.lines[:1]))


# ----------------------------------------------------------------------------- report

ICON = {"high": "🔴", "medium": "🟠", "low": "🟡", "info": "ℹ️ "}


def render(findings: list[Finding], n_units: int, title: str) -> str:
    out = [f"# PII writes: {title}", ""]
    counts = {s: sum(f.severity == s for f in findings) for s in ("high", "medium", "low", "info")}
    out.append(f"{n_units} unit(s) scanned · " + "  ".join(f"{ICON[s]} {c} {s}" for s, c in counts.items() if c) + ("  ·  nothing found" if not findings else ""))
    out.append("")
    for f in findings:
        loc = f"{f.file}:{','.join(map(str, f.lines))}" if f.lines else f.unit_id
        out.append(f"{ICON[f.severity]} {f.severity.upper():<6} {loc}")
        out.append(f"        {f.reason}  ·  in a hunk with: {', '.join(f.categories)}")
        for e in f.excerpt[:6]:
            out.append(f"        {e.rstrip()}")
        out.append("")
    if findings:
        out.append("Severity = sink × protection × category, decided in code (see top of pii_writes.py).")
        out.append("Jev judges each hunk/window alone; data assembled elsewhere and written here is not visible to it.")
    out.append("")
    out.append(f"Jev: {format_usage(USAGE['requests'], USAGE['input'], USAGE['seconds'])}")
    return "\n".join(out)


# ----------------------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", help="PR number/URL, or file/dir paths")
    ap.add_argument("--repo")
    ap.add_argument("--diff-file")
    ap.add_argument("--model", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.diff_file:
        files = parse_unified_diff(Path(args.diff_file).read_text())
        units = units_from_files([f for f in files if not f.is_noise])
        title = Path(args.diff_file).name
    elif args.targets and all(Path(t).exists() for t in args.targets):
        units = units_from_paths(args.targets)
        title = ", ".join(args.targets)
    elif len(args.targets) == 1:
        try:
            pr: PullRequest = load_pr(args.targets[0], repo=args.repo)
        except PullRequestError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        units = units_from_files(pr.content_files)
        title = f"{pr.title} (#{pr.number})"
    else:
        ap.error("give a PR ref, --diff-file, or existing paths")
        return 2
    if not units:
        print("Nothing to scan.", file=sys.stderr)
        return 0

    findings = asyncio.run(scan(units, args.model, args.verbose))
    if args.json:
        print(json.dumps({"title": title, "units": len(units), "findings": [asdict(f) for f in findings],
                          "usage": {"requests": USAGE["requests"], "input_tokens": USAGE["input"], "cost_usd": round(cost_usd(USAGE["input"]), 6), "seconds": round(USAGE["seconds"], 2)}}, indent=2))
    else:
        print(render(findings, len(units), title))
    return 1 if any(f.severity in ("high", "medium") for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
