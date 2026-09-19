"""Acceptance-criteria coverage for a pull request, judged by Jev.

Given a PR whose description lists acceptance criteria (checkbox bullets under
an "Acceptance Criteria" heading), decide for each bullet whether the diff
addresses it: fully, partially, not at all, or can't-tell-from-code.

This is a *coverage* check, not a correctness proof. Jev is a System One model:
it makes fast, literal, calibrated judgments over bounded evidence. It is good
at "does this hunk contribute to this criterion?" and "given these hunks, is
the criterion implemented?"; it does not execute code or reason across many
hops. Anything that needs that stays with a person (or a reasoning model).

Pipeline (code owns the workflow, Jev makes the calls):

  parse criteria + diff ──▶ stage 0: is each criterion verifiable from a diff?
                        ──▶ stage 1: relevance fan-out, one Noul per (hunk, criterion)
                        ──▶ stage 2: per criterion, Score coverage over ONLY its evidence
                        ──▶ report

Stage 1 exists because Jev's state budget is 32k tokens and its accuracy drops
with irrelevant context, so we never show the whole diff to one question.

Run:
  uv run examples/ac-coverage/ac_coverage.py 123 [--repo owner/name]
  uv run examples/ac-coverage/ac_coverage.py --body-file f.md --diff-file f.diff
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass, field

from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, Noul, Score

from jevlab.pr import FileDiff, Hunk, PullRequest, estimate_tokens, load_pr

load_dotenv()

# --- Thresholds. Starting points, not rules: tune on your own PRs. -------------------
RELEVANT = 0.6  # Noul p(yes) for a hunk to count as evidence for a criterion
WEAK_RELEVANT = 0.3  # fallback: if nothing clears RELEVANT, take the top few above this
MAX_EVIDENCE_HUNKS = 12  # cap evidence per criterion (stage 2 state size)
UNVERIFIABLE = 0.7  # p(yes) that a criterion can't be judged from a diff
FULL = 1.5  # Score >= FULL  -> implemented        (levels 0..2)
PARTIAL = 0.5  # Score >= PARTIAL -> partial
CONTRADICTS = 0.6  # p(yes) that the evidence does the opposite of the criterion
LOW_CONFIDENCE = 0.5  # below this, ask a human to look
STATE_BUDGET_TOKENS = 20_000  # per request; leaves room for questions under the 32k/64k limits
MAX_HUNKS_PER_REQUEST = 40  # hunks x criteria Nouls per request stays manageable
HUNK_CHAR_CAP = 6_000  # a hunk longer than this is truncated (giant generated blocks)
CONCURRENCY = 8


@dataclass
class Criterion:
    id: str
    text: str
    checked: bool = False


@dataclass
class Evidence:
    hunk_id: str
    file: str
    relevance: float
    diff: str


@dataclass
class Verdict:
    criterion: Criterion
    status: str  # implemented | partial | not_implemented | contradicted | no_evidence | unverifiable
    coverage_score: float | None = None
    coverage_confidence: float | None = None
    coverage_probabilities: dict[int, float] | None = None
    has_tests: float | None = None
    contradicts: float | None = None
    unverifiable: float = 0.0
    needs_review: bool = False
    evidence: list[Evidence] = field(default_factory=list)


# ----------------------------------------------------------------------------- criteria

_AC_HEADING = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*|\*\*)?\s*(acceptance\s+criteria|ac|definition\s+of\s+done|dod)\s*:?\s*(?:\*\*)?\s*$",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[( |x|X)\]\s*)?(.+?)\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")


def parse_criteria(body: str) -> list[Criterion]:
    """Bullets under an 'Acceptance Criteria' heading; else every checkbox in the body."""
    lines = body.splitlines()
    section: list[str] = []
    in_section = False
    for line in lines:
        if _AC_HEADING.match(line):
            in_section = True
            continue
        if in_section:
            if _HEADING.match(line) or (line.strip() and not _BULLET.match(line) and section and not line.startswith(" ")):
                # A new heading or a non-bullet paragraph ends the section.
                if _HEADING.match(line) or not line.strip().startswith(("-", "*", "+")):
                    break
            section.append(line)
    candidates = section if section else lines
    only_checkboxes = not section
    out: list[Criterion] = []
    for line in candidates:
        m = _BULLET.match(line)
        if not m:
            continue
        checked, text = m.group(1), m.group(2).strip()
        if only_checkboxes and checked is None:
            continue
        if len(text) < 6:
            continue
        out.append(Criterion(id=f"ac{len(out) + 1}", text=text, checked=bool(checked and checked.lower() == "x")))
    return out


# ----------------------------------------------------------------------------- packing

def _hunk_text(h: Hunk) -> str:
    t = h.text()
    return t if len(t) <= HUNK_CHAR_CAP else t[:HUNK_CHAR_CAP] + "\n... [truncated]"


def pack_hunks(files: list[FileDiff]) -> list[list[Hunk]]:
    """Greedy: fill requests up to the state token budget, never mixing more than needed."""
    batches: list[list[Hunk]] = []
    cur: list[Hunk] = []
    cur_tokens = 0
    for f in files:
        for h in f.hunks:
            t = estimate_tokens(_hunk_text(h))
            if cur and (cur_tokens + t > STATE_BUDGET_TOKENS or len(cur) >= MAX_HUNKS_PER_REQUEST):
                batches.append(cur)
                cur, cur_tokens = [], 0
            cur.append(h)
            cur_tokens += t
    if cur:
        batches.append(cur)
    return batches


# ----------------------------------------------------------------------------- questions

def verifiability_questions(criteria: list[Criterion]) -> dict[str, Noul]:
    """Stage 0: about the criterion text alone. Runs concurrently with stage 1."""
    return {
        c.id: Noul(
            instructions=(
                f"This acceptance criterion cannot be confirmed by reading a source-code diff alone: "
                f'"{c.text}"'
            ),
            criteria={
                "true": (
                    "It requires running the software or observing it: response time or performance "
                    "numbers, visual appearance or layout, manual QA steps, stakeholder sign-off, "
                    "deployment or monitoring outcomes"
                ),
                "false": (
                    "It describes behavior, validation, data handling, API shape, configuration, or "
                    "tests that a reader can recognize in code changes"
                ),
            },
        )
        for c in criteria
    }


def relevance_questions(hunk_ids: list[str], criteria: list[Criterion]) -> dict[str, Noul]:
    """Stage 1: one literal Noul per (hunk, criterion). They all run in parallel."""
    qs: dict[str, Noul] = {}
    for hid in hunk_ids:
        for c in criteria:
            # "Determines the behavior" rather than "implements": a hunk that does the
            # OPPOSITE of the criterion must still reach stage 2, where the Score's
            # lowest level catches it. Jev reads literally; "implements" excluded it.
            qs[f"{hid}|{c.id}"] = Noul(
                instructions=(
                    f"The code change in `hunks.{hid}.diff` determines how the software behaves in "
                    f'the situation this acceptance criterion describes: "{c.text}"'
                ),
                criteria={
                    "true": (
                        "The added or modified lines implement, configure, validate, test, or otherwise "
                        "decide what happens in that situation, whether or not they satisfy the criterion"
                    ),
                    "false": (
                        "The lines are unrelated to that situation, or only change formatting, imports, "
                        "or comments"
                    ),
                },
            )
    return qs


def coverage_questions(c: Criterion) -> dict[str, Noul | Score]:
    """Stage 2: judged over ONLY the evidence hunks for this criterion."""
    return {
        "coverage": Score(
            instructions=(
                f'How completely do the code changes in `evidence` implement this acceptance criterion: "{c.text}"'
            ),
            criteria=[
                "None of the changes implement the behavior the criterion describes, or they implement the opposite of it",
                "Some of the behavior is implemented, but at least one condition, value, or step stated in the criterion is missing, stubbed, hard-coded, or marked TODO",
                "Every condition, value, and step stated in the criterion is present in the changes",
            ],
        ),
        "contradicts": Noul(
            instructions=(
                f'The code changes in `evidence` make the software behave in a way that conflicts with this acceptance criterion: "{c.text}"'
            ),
            criteria={
                "true": "In the situation the criterion describes, the changed code produces a different outcome than the criterion requires",
                "false": "The changes satisfy the criterion, or do not decide the outcome in that situation at all",
            },
        ),
        "has_tests": Noul(
            instructions=(
                f'`evidence` includes new or modified test code that exercises the behavior in this criterion: "{c.text}"'
            ),
            criteria={
                "true": "A test function, test case, or assertion in the evidence checks the behavior the criterion describes",
                "false": "No test code in the evidence, or the tests present check something else",
            },
        ),
    }


# ----------------------------------------------------------------------------- pipeline

async def run(pr: PullRequest, criteria: list[Criterion], model: str | None, verbose: bool) -> list[Verdict]:
    files = pr.content_files
    hunks_by_id: dict[str, Hunk] = {}
    for f in files:
        for i, h in enumerate(f.hunks):
            hunks_by_id[f"h{len(hunks_by_id)}"] = h
    id_of = {id(h): hid for hid, h in hunks_by_id.items()}

    sem = asyncio.Semaphore(CONCURRENCY)
    usage = {"input": 0, "requests": 0}

    async with AsyncTypeSafeClient(model=model) as client:

        async def ask(state, questions):
            async with sem:
                resp = await client.system_one(state=state, questions=questions)
            usage["input"] += resp.usage.input_tokens
            usage["requests"] += 1
            return resp

        # Stage 0 + stage 1 are independent: fire them together.
        stage0 = ask({"criteria": {c.id: c.text for c in criteria}}, verifiability_questions(criteria))
        stage1 = []
        for batch in pack_hunks(files):
            ids = [id_of[id(h)] for h in batch]
            state = {
                "pr_title": pr.title,
                "hunks": {hid: {"file": hunks_by_id[hid].file, "diff": _hunk_text(hunks_by_id[hid])} for hid in ids},
            }
            stage1.append(ask(state, relevance_questions(ids, criteria)))
        s0, *s1 = await asyncio.gather(stage0, *stage1)
        if verbose:
            print(f"[stage 1] {len(s1)} request(s) over {len(hunks_by_id)} hunk(s) x {len(criteria)} criteria", file=sys.stderr)

        relevance: dict[str, dict[str, float]] = {c.id: {} for c in criteria}  # criterion -> hunk -> p
        for resp in s1:
            for key, ans in resp.nouls.items():
                hid, cid = key.split("|")
                relevance[cid][hid] = ans.noul
        if verbose:
            print("[stage 1] relevance p(hunk determines criterion behavior):", file=sys.stderr)
            for hid, h in hunks_by_id.items():
                row = "  ".join(f"{c.id}={relevance[c.id].get(hid, 0):.2f}" for c in criteria)
                print(f"  {hid:<4} {h.file:<50} {row}", file=sys.stderr)

        # Stage 2: one request per criterion that has evidence.
        verdicts: dict[str, Verdict] = {}
        stage2: dict[str, asyncio.Task] = {}
        for c in criteria:
            ranked = sorted(relevance[c.id].items(), key=lambda kv: -kv[1])
            strong = [(h, p) for h, p in ranked if p >= RELEVANT]
            picked = strong or [(h, p) for h, p in ranked[:3] if p >= WEAK_RELEVANT]
            picked = picked[:MAX_EVIDENCE_HUNKS]
            ev = [Evidence(h, hunks_by_id[h].file, p, _hunk_text(hunks_by_id[h])) for h, p in picked]
            v = Verdict(criterion=c, status="no_evidence", unverifiable=s0.nouls[c.id].noul, evidence=ev)
            verdicts[c.id] = v
            if not ev:
                continue
            # Keep the stage-2 state inside budget: drop lowest-relevance hunks if needed.
            while ev and sum(estimate_tokens(e.diff) for e in ev) > STATE_BUDGET_TOKENS:
                ev.pop()
            state = {
                "criterion": c.text,
                "pr_title": pr.title,
                "evidence": [{"file": e.file, "diff": e.diff} for e in ev],
            }
            stage2[c.id] = asyncio.create_task(ask(state, coverage_questions(c)))

        for cid, task in stage2.items():
            resp = await task
            v = verdicts[cid]
            cov = resp.scores["coverage"]
            v.coverage_score = cov.score
            v.coverage_confidence = cov.confidence
            v.coverage_probabilities = {int(k): round(p, 3) for k, p in cov.probabilities.items()}
            v.has_tests = resp.nouls["has_tests"].noul
            v.contradicts = resp.nouls["contradicts"].noul

    # --- Policy lives here, in code. -----------------------------------------------
    for v in verdicts.values():
        if v.coverage_score is None:
            v.status = "no_evidence"
        elif v.coverage_score >= FULL:
            v.status = "implemented"
        elif v.coverage_score >= PARTIAL:
            v.status = "partial"
        elif v.contradicts is not None and v.contradicts >= CONTRADICTS:
            v.status = "contradicted"
        else:
            v.status = "not_implemented"
        if v.unverifiable >= UNVERIFIABLE and v.status in ("no_evidence", "not_implemented"):
            v.status = "unverifiable"
        v.needs_review = (
            (v.coverage_confidence is not None and v.coverage_confidence < LOW_CONFIDENCE)
            or (v.status == "no_evidence" and any(WEAK_RELEVANT <= p < RELEVANT for p in relevance[v.criterion.id].values()))
            or v.status == "partial"
        )

    if verbose:
        print(f"[usage] {usage['requests']} request(s), {usage['input']:,} input tokens", file=sys.stderr)
    return [verdicts[c.id] for c in criteria]


# ----------------------------------------------------------------------------- report

ICON = {
    "implemented": "✅",
    "partial": "⚠️ ",
    "not_implemented": "❌",
    "contradicted": "⛔",
    "no_evidence": "❌",
    "unverifiable": "🔍",
}
LABEL = {
    "implemented": "implemented",
    "partial": "partial",
    "not_implemented": "not implemented",
    "contradicted": "contradicted by diff",
    "no_evidence": "no evidence in diff",
    "unverifiable": "can't verify from diff",
}


def render(pr: PullRequest, verdicts: list[Verdict]) -> str:
    lines = [f"# AC coverage: {pr.title}" + (f" (#{pr.number})" if pr.number else ""), ""]
    n_ok = sum(v.status == "implemented" for v in verdicts)
    lines.append(f"{n_ok}/{len(verdicts)} criteria implemented  ·  "
                 f"{len(pr.content_files)} files judged, {len(pr.noise_files)} skipped as noise")
    lines.append("")
    lines.append("| # | status | score | conf | tests | criterion |")
    lines.append("|---|--------|-------|------|-------|-----------|")
    for i, v in enumerate(verdicts, 1):
        score = f"{v.coverage_score:.2f}" if v.coverage_score is not None else "–"
        conf = f"{v.coverage_confidence:.2f}" if v.coverage_confidence is not None else "–"
        tests = ("yes" if v.has_tests > 0.6 else "no") if v.has_tests is not None else "–"
        flag = " 👀" if v.needs_review else ""
        lines.append(f"| {i} | {ICON[v.status]} {LABEL[v.status]}{flag} | {score} | {conf} | {tests} | {v.criterion.text} |")
    lines.append("")
    for i, v in enumerate(verdicts, 1):
        if v.status == "implemented" and not v.needs_review:
            continue
        lines.append(f"## {i}. {v.criterion.text}")
        lines.append(f"status: {LABEL[v.status]}" + (f"  ·  p(unverifiable)={v.unverifiable:.2f}" if v.unverifiable > 0.3 else ""))
        if v.contradicts is not None and v.contradicts > 0.3:
            lines.append(f"p(contradicts)={v.contradicts:.2f}")
        if v.coverage_probabilities:
            lines.append(f"coverage levels p: none={v.coverage_probabilities.get(0, 0):.2f} "
                         f"partial={v.coverage_probabilities.get(1, 0):.2f} full={v.coverage_probabilities.get(2, 0):.2f}")
        if v.evidence:
            lines.append("evidence:")
            for e in v.evidence:
                lines.append(f"  - {e.file} {e.hunk_id}  relevance={e.relevance:.2f}")
        else:
            lines.append("evidence: none of the hunks looked related to this criterion")
        lines.append("")
    if any(v.needs_review for v in verdicts):
        lines.append("👀 = low confidence or partial; worth a human look.")
    return "\n".join(lines)


def to_json(pr: PullRequest, verdicts: list[Verdict]) -> str:
    return json.dumps(
        {
            "pr": {"number": pr.number, "title": pr.title, "url": pr.url},
            "files_judged": [f.path for f in pr.content_files],
            "files_skipped": [f.path for f in pr.noise_files],
            "verdicts": [asdict(v) for v in verdicts],
        },
        indent=2,
    )


# ----------------------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ref", nargs="?", help="PR number or URL")
    ap.add_argument("--repo", help="owner/name (when ref is a number outside the repo)")
    ap.add_argument("--body-file", help="local PR body markdown (first line '# title')")
    ap.add_argument("--diff-file", help="local unified diff")
    ap.add_argument("--criteria-file", help="override: one criterion per line")
    ap.add_argument("--model", default=None, help="e.g. jev-1.13.0 to pin (default: SDK default, jev-latest)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    pr = load_pr(args.ref, body_file=args.body_file, diff_file=args.diff_file, repo=args.repo)
    if args.criteria_file:
        criteria = [Criterion(f"ac{i + 1}", t.strip()) for i, t in enumerate(open(args.criteria_file)) if t.strip()]
    else:
        criteria = parse_criteria(pr.body)
    if not criteria:
        print("No acceptance criteria found. Add a '## Acceptance Criteria' section with bullets, "
              "or pass --criteria-file.", file=sys.stderr)
        return 2
    if not pr.content_files:
        print("Diff has no judgeable files (all noise/binary).", file=sys.stderr)
        return 2

    verdicts = asyncio.run(run(pr, criteria, args.model, args.verbose))
    print(to_json(pr, verdicts) if args.json else render(pr, verdicts))
    ok = all(v.status in ("implemented", "unverifiable") for v in verdicts)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
