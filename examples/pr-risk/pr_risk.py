"""Assess the risk of a pull request with Jev and a natural-language policy.

The policy (`policy.yaml`) holds risk rules written in plain English. Each rule
becomes ONE Noul question to Jev ("is this statement true of the diff?"), so
adding a rule is editing YAML, not code. Everything that is arithmetic stays in
code: version bumps, line/file counts, test ratios, weights, thresholds and the
final level. Jev is a System One model: excellent at "does this diff touch
payments?", unreliable at "is 12.1 a major bump over 7.4". Splitting the work
this way keeps the judgments where each side is strong, and keeps the policy
auditable (every probability, weight and point is printed).

Big PRs are split into chunks of a few files each, one request per chunk, all
rule Nouls in parallel inside the request. Per-rule probabilities are combined
across chunks in code (max for "touches X" rules, min for "every line is X").

Run from the repo root (reads TYPESAFE_API_KEY from .env):
  uv run examples/pr-risk/pr_risk.py 123 --repo owner/name
  uv run examples/pr-risk/pr_risk.py https://github.com/owner/name/pull/123 --json
  uv run examples/pr-risk/pr_risk.py --body-file fixtures/x.md --diff-file fixtures/x.diff
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, Noul, Score

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jevlab.pr import FileDiff, PullRequest, dependency_changes, estimate_tokens, load_pr  # noqa: E402

load_dotenv()

HERE = Path(__file__).resolve().parent
DEFAULT_POLICY = HERE / "policy.yaml"
LEVELS = ["low", "medium", "high", "critical"]

# --- Token budgeting -------------------------------------------------------
# Jev allows 32k tokens for state + longest question. We stay well under that
# so accuracy doesn't degrade from a state full of detail: ~20k tokens of diff
# per request, at most a few files per chunk (irrelevant files are distractors).
MAX_CHUNK_TOKENS = 20_000
MAX_FILE_CHARS = 12_000  # ~3k tokens; longer diffs are truncated with a marker
MAX_BODY_CHARS = 3_000
MAX_FILES_PER_CHUNK = 5
MAX_CONCURRENCY = 4


# =========================================================================== policy


@dataclass
class Rule:
    id: str
    description: str
    risk: str = "medium"
    weight: float = 1.0
    points: float | None = None  # explicit override (may be negative)
    criteria: dict[str, str] | None = None
    aggregate: str = "max"  # max | min across chunks
    enabled: bool = True

    def question(self) -> Noul:
        # Structured instructions: the rule text is the `statement`, and the
        # question tells Jev exactly what to judge it against. Jev reads
        # literally, so we point it at the diff lines and say what title/body are.
        instructions = {
            "question": "Is `statement` true of the code changes shown in `files`?",
            "statement": self.description,
            "judge_from": (
                "The added (+) and removed (-) lines in each `files[].diff`. "
                "`title` and `body` describe intent and are context only."
            ),
        }
        criteria = None
        if self.criteria:
            criteria = {"true": self.criteria.get("true"), "false": self.criteria.get("false")}
            criteria = {k: v for k, v in criteria.items() if v}
        return Noul(instructions=instructions, criteria=criteria or None)


@dataclass
class Policy:
    raw: dict[str, Any]
    rules: list[Rule]
    path: Path

    @property
    def thresholds(self) -> dict[str, float]:
        return self.raw["thresholds"]

    @property
    def risk_points(self) -> dict[str, float]:
        return self.raw["risk_points"]

    def rule_points(self, rule: Rule) -> float:
        base = rule.points if rule.points is not None else self.risk_points.get(rule.risk, 0)
        return float(base) * float(rule.weight)

    def level_for(self, score: float) -> str:
        t = self.thresholds
        if score >= t.get("critical", float("inf")):
            return "critical"
        if score >= t.get("high", float("inf")):
            return "high"
        if score >= t.get("medium", float("inf")):
            return "medium"
        return "low"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def load_policy(path: Path) -> Policy:
    """Load a policy, resolving `extends:` so users only write their deltas.

    Rules merge by `id` (child wins; `enabled: false` drops a rule). Every other
    top-level key in the child replaces the parent's value wholesale.
    """
    data = _load_yaml(path)
    parent_ref = data.pop("extends", None)
    if parent_ref:
        parent = load_policy((path.parent / parent_ref).resolve())
        merged = dict(parent.raw)
        merged.update({k: v for k, v in data.items() if k != "rules"})
        by_id = {r.id: r for r in parent.rules}
        for r in _parse_rules(data.get("rules", [])):
            by_id[r.id] = r
        return Policy(raw=merged, rules=[r for r in by_id.values() if r.enabled], path=path)
    return Policy(raw=data, rules=[r for r in _parse_rules(data.get("rules", [])) if r.enabled], path=path)


def _parse_rules(items: list[dict[str, Any]]) -> list[Rule]:
    rules = []
    for item in items:
        if "id" not in item or "description" not in item:
            raise ValueError(f"rule needs `id` and `description`: {item}")
        if item.get("risk", "medium") not in LEVELS:
            raise ValueError(f"rule {item['id']}: risk must be one of {LEVELS}")
        if item.get("aggregate", "max") not in ("max", "min"):
            raise ValueError(f"rule {item['id']}: aggregate must be max or min")
        rules.append(
            Rule(
                id=item["id"],
                description=" ".join(str(item["description"]).split()),
                risk=item.get("risk", "medium"),
                weight=float(item.get("weight", 1.0)),
                points=item.get("points"),
                criteria=item.get("criteria"),
                aggregate=item.get("aggregate", "max"),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return rules


# =========================================================================== base questions
# These run on every PR even with an empty rule list, so risk is discoverable
# from base criteria alone. One dimension per Score; situations, not degrees.

BLAST_RADIUS = [
    "Isolated: a leaf file such as a single UI component, script, test, document, or one config value, with no other code depending on it",
    "Local: a module used by a few callers inside one feature area",
    "Shared: a helper, base class, middleware, data model, or dependency used by several features",
    "Core: cross-cutting behavior such as the request pipeline, authentication, the data layer, build, or runtime configuration of the whole system",
]
BEHAVIOR_CHANGE = [
    "No runtime behavior change: only comments, documentation, formatting, user-visible text, or tests change",
    "Additive: new code paths, options, or fields; existing behavior is unchanged when the new code is not used",
    "Modifies existing behavior: an existing code path now produces different results, defaults, or side effects",
    "Removes or breaks existing behavior: functionality is removed, or existing callers must change to keep working",
]

BASE_QUESTIONS = {
    "blast_radius": Score(
        instructions={
            "question": "How much of the system depends on the code changed in `files`?",
            "judge_from": "File paths, imports, and what the changed functions or definitions are, in `files[].diff`.",
        },
        criteria=BLAST_RADIUS,
    ),
    "behavior_change": Score(
        instructions={
            "question": "How do the changes in `files` affect the runtime behavior of code that already existed before this change?",
            "judge_from": "The added (+) and removed (-) lines in `files[].diff`.",
        },
        criteria=BEHAVIOR_CHANGE,
    ),
    "tests_cover_change": Noul(
        instructions={
            "question": "Is `statement` true of the code changes shown in `files`?",
            "statement": "The diff adds or modifies test code that exercises the non-test code changed in the same diff.",
        },
        criteria={
            "true": "A changed test file contains new or modified test cases that call or assert on the changed non-test functions or behavior.",
            "false": "No test file is changed, or the changed tests are unrelated to the non-test changes, or the diff contains only tests.",
        },
    ),
}


# =========================================================================== code-computed facts

_DOC_EXT = re.compile(r"\.(md|mdx|rst|txt|adoc)$", re.I)
_CONFIG_PATTERN = re.compile(
    r"(^|/)(\.github/|\.gitlab-ci|\.circleci/|Dockerfile|docker-compose|\.env|Makefile|Jenkinsfile|"
    r"[^/]*\.(ya?ml|toml|ini|cfg|conf|tf|hcl|json))",
    re.I,
)


def categorize(f: FileDiff) -> str:
    if f.is_test:
        return "test"
    if f.is_manifest:
        return "manifest"
    if _DOC_EXT.search(f.path):
        return "docs"
    if _CONFIG_PATTERN.search(f.path):
        return "config"
    return "source"


def size_category(value: int, thresholds: dict[str, int]) -> str:
    if value < thresholds["small"]:
        return "small"
    if value < thresholds["medium"]:
        return "medium"
    return "large"


def compute_facts(pr: PullRequest, policy: Policy) -> dict[str, Any]:
    files = pr.content_files
    added = sum(f.added for f in files)
    removed = sum(f.removed for f in files)
    cats = {}
    for f in files:
        cats[categorize(f)] = cats.get(categorize(f), 0) + 1
    tests = [f for f in files if f.is_test]
    sr = policy.raw["size_rules"]
    by_lines = size_category(added + removed, sr["lines_changed"])
    by_files = size_category(len(files), sr["files_changed"])
    size = max(by_lines, by_files, key=["small", "medium", "large"].index)

    deps = dependency_changes(pr)
    dep_rules = policy.raw["dependency_rules"]
    for d in deps:
        d["risk"] = dep_rules.get(d["bump"], "medium")

    return {
        "files_total": len(pr.files),
        "files_analyzed": len(files),
        "noise_files": [f.path for f in pr.noise_files],
        "lines_added": added,
        "lines_removed": removed,
        "lines_changed": added + removed,
        "size": size,
        "size_by_lines": by_lines,
        "size_by_files": by_files,
        "categories": cats,
        "test_files": len(tests),
        "test_ratio": round(len(tests) / len(files), 2) if files else 0.0,
        "tests_only": bool(files) and all(f.is_test for f in files),
        "docs_only": bool(files) and all(categorize(f) == "docs" for f in files),
        "deleted_files": [f.path for f in files if f.status == "deleted"],
        "dependency_changes": deps,
    }


# =========================================================================== chunking


@dataclass
class Chunk:
    id: str
    files: list[FileDiff]
    state: dict[str, Any]
    tokens: int
    truncated: bool = False


def build_chunks(pr: PullRequest) -> list[Chunk]:
    """Pack content files into chunks that fit the token budget.

    Files are packed in path order so siblings (a module and its neighbours)
    tend to land together. Small PRs are a single chunk.
    """
    body = pr.body[:MAX_BODY_CHARS] + ("\n... [body truncated]" if len(pr.body) > MAX_BODY_CHARS else "")
    header = {"title": pr.title, "body": body}
    header_tokens = estimate_tokens(json.dumps(header))

    chunks: list[Chunk] = []
    current: list[tuple[FileDiff, dict[str, Any], bool]] = []
    current_tokens = header_tokens

    def flush() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        state = {**header, "files": [entry for _, entry, _ in current]}
        chunks.append(
            Chunk(
                id=f"chunk-{len(chunks) + 1}",
                files=[f for f, _, _ in current],
                state=state,
                tokens=current_tokens,
                truncated=any(t for _, _, t in current),
            )
        )
        current, current_tokens = [], header_tokens

    for f in sorted(pr.content_files, key=lambda x: x.path):
        full = f.text()
        truncated = len(full) > MAX_FILE_CHARS
        entry = {
            "path": f.path,
            "status": f.status,
            "lines_added": f.added,
            "lines_removed": f.removed,
            "diff": f.text(MAX_FILE_CHARS),
        }
        if f.old_path:
            entry["renamed_from"] = f.old_path
        tokens = estimate_tokens(json.dumps(entry))
        if current and (len(current) >= MAX_FILES_PER_CHUNK or current_tokens + tokens > MAX_CHUNK_TOKENS):
            flush()
        current.append((f, entry, truncated))
        current_tokens += tokens
    flush()
    return chunks


# =========================================================================== model calls


def rule_questions(rules: list[Rule]) -> dict[str, Noul]:
    return {f"rule:{r.id}": r.question() for r in rules}


async def _ask(client: AsyncTypeSafeClient, state: dict[str, Any], questions: dict, sem: asyncio.Semaphore, model: str | None):
    async with sem:
        return await client.system_one(state=state, questions=questions, model=model)


async def run_model(chunks: list[Chunk], policy: Policy, model: str | None, localize: bool):
    """Two passes: every question on every chunk, then localise fired rules.

    Pass 1 sends ALL rule Nouls plus the base questions for each chunk in one
    request (independent questions run in parallel server-side). Pass 2 re-asks
    only the rules that fired, one request per file of each multi-file chunk
    that fired, so the report can point at the file rather than the chunk.
    Smaller state per request also matches the guidance that irrelevant detail
    degrades accuracy.
    """
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    fire = float(policy.raw.get("fire_threshold", 0.6))
    async with AsyncTypeSafeClient() as client:
        questions = {**rule_questions(policy.rules), **BASE_QUESTIONS}
        responses = await asyncio.gather(*(_ask(client, c.state, questions, sem, model) for c in chunks))

        localization: dict[str, dict[str, float]] = {}  # rule id -> {path: p}
        if not localize:
            return responses, localization, 0
        jobs: list[tuple[str, list[Rule], dict[str, Any]]] = []
        for chunk, resp in zip(chunks, responses):
            if len(chunk.files) < 2:
                continue
            fired = [r for r in policy.rules if resp.nouls[f"rule:{r.id}"].noul >= fire and policy.rule_points(r) > 0]
            if not fired:
                continue
            for f, entry in zip(chunk.files, chunk.state["files"]):
                state = {"title": chunk.state["title"], "body": chunk.state["body"], "files": [entry]}
                jobs.append((f.path, fired, state))
        results = await asyncio.gather(*(_ask(client, st, rule_questions(rs), sem, model) for _, rs, st in jobs))
        for (path, rules, _), resp in zip(jobs, results):
            for r in rules:
                localization.setdefault(r.id, {})[path] = resp.nouls[f"rule:{r.id}"].noul
        return responses, localization, len(results)


# =========================================================================== composite scoring


def assess(pr: PullRequest, policy: Policy, chunks: list[Chunk], responses: list, localization: dict[str, dict[str, float]] | None = None, extra_requests: int = 0) -> dict[str, Any]:
    localization = localization or {}
    facts = compute_facts(pr, policy)
    fire = float(policy.raw.get("fire_threshold", 0.6))
    lo, hi = policy.raw.get("uncertain_band", [0.35, 0.65])
    low_conf = float(policy.raw.get("low_confidence", 0.5))
    contributions: list[dict[str, Any]] = []
    review_flags: list[str] = []

    # --- rules: aggregate per-chunk probabilities in code -------------------
    rule_rows = []
    for rule in policy.rules:
        per_chunk = {c.id: resp.nouls[f"rule:{rule.id}"].noul for c, resp in zip(chunks, responses)}
        agg = max if rule.aggregate == "max" else min
        prob = agg(per_chunk.values())
        fired = prob >= fire
        points = policy.rule_points(rule) if fired else 0.0
        # Files that triggered the rule. Single-file chunks are exact; for
        # multi-file chunks use the localisation pass, falling back to the
        # chunk's files if no single file reaches the threshold on its own.
        where: list[str] = []
        per_file = localization.get(rule.id, {})
        for c in chunks:
            if per_chunk[c.id] < fire:
                continue
            if len(c.files) == 1:
                where.append(c.files[0].path)
                continue
            hits = [f.path for f in c.files if per_file.get(f.path, 0.0) >= fire]
            where.extend(hits or [f.path for f in c.files])
        uncertain = lo <= prob <= hi
        row = {
            "id": rule.id,
            "risk": rule.risk,
            "weight": rule.weight,
            "probability": round(prob, 3),
            "fired": fired,
            "points": points,
            "uncertain": uncertain,
            "aggregate": rule.aggregate,
            "per_chunk": {k: round(v, 3) for k, v in per_chunk.items()},
            "files": where,
            "per_file": {k: round(v, 3) for k, v in per_file.items()},
            "description": rule.description,
        }
        rule_rows.append(row)
        if fired and points != 0:
            verb = "fired" if points > 0 else "mitigates"
            contributions.append(
                {
                    "source": "rule",
                    "id": rule.id,
                    "points": points,
                    "reason": f"rule `{rule.id}` ({rule.risk}) {verb} at p={prob:.2f}"
                    + (f" in {', '.join(where[:3])}" if where and points > 0 else ""),
                    "uncertain": uncertain,
                }
            )
        if uncertain and rule.risk in ("high", "critical"):
            review_flags.append(f"rule `{rule.id}` ({rule.risk}) is near coin-flip at p={prob:.2f}")

    # --- dependencies: largest bump counts (code) ----------------------------
    deps = facts["dependency_changes"]
    if deps:
        worst = max(deps, key=lambda d: LEVELS.index(d["risk"]))
        pts = float(policy.risk_points.get(worst["risk"], 0))
        label = f"{worst['name']} {worst['old'] or '(new)'} -> {worst['new'] or '(removed)'}"
        contributions.append(
            {
                "source": "dependency",
                "id": worst["name"],
                "points": pts,
                "reason": f"dependency {label} is a {worst['bump']} change ({worst['risk']} risk)"
                + (f", {len(deps)} dependency changes total" if len(deps) > 1 else ""),
                "uncertain": False,
            }
        )

    # --- size (code) ---------------------------------------------------------
    size_pts = float(policy.raw["size_rules"]["points"].get(facts["size"], 0))
    contributions.append(
        {
            "source": "size",
            "id": facts["size"],
            "points": size_pts,
            "reason": f"{facts['size']} change: {facts['files_analyzed']} files, {facts['lines_changed']} lines",
            "uncertain": False,
        }
    )

    # --- base scores: worst chunk wins ---------------------------------------
    base_cfg = policy.raw.get("base", {})
    base_out: dict[str, Any] = {}
    for qid, levels, weight_key in (
        ("blast_radius", BLAST_RADIUS, "blast_radius_weight"),
        ("behavior_change", BEHAVIOR_CHANGE, "behavior_change_weight"),
    ):
        answers = [resp.scores[qid] for resp in responses]
        worst_i = max(range(len(answers)), key=lambda i: answers[i].score)
        a = answers[worst_i]
        norm = a.score / (len(levels) - 1)
        weight = float(base_cfg.get(weight_key, 1.0))
        pts = round(norm * weight, 2)
        nearest = levels[min(range(len(levels)), key=lambda i: abs(i - a.score))]
        base_out[qid] = {
            "score": round(a.score, 2),
            "max": len(levels) - 1,
            "normalized": round(norm, 3),
            "confidence": round(a.confidence, 2),
            "nearest_level": nearest,
            "probabilities": {str(k): round(v, 3) for k, v in a.probabilities.items()},
            "chunk": chunks[worst_i].id,
            "points": pts,
        }
        contributions.append(
            {
                "source": "base",
                "id": qid,
                "points": pts,
                "reason": f"{qid.replace('_', ' ')}: {nearest.split(':')[0].lower()} ({a.score:.1f}/{len(levels) - 1}, confidence {a.confidence:.2f})",
                "uncertain": a.confidence < low_conf,
            }
        )

    tests_p = max(resp.nouls["tests_cover_change"].noul for resp in responses)
    credit = float(base_cfg.get("tests_cover_change_credit", 0))
    tests_fired = tests_p >= fire and facts["test_files"] > 0 and not facts["tests_only"]
    base_out["tests_cover_change"] = {"probability": round(tests_p, 3), "fired": tests_fired, "points": -credit if tests_fired else 0.0}
    if tests_fired and credit:
        contributions.append(
            {"source": "base", "id": "tests_cover_change", "points": -credit, "reason": f"tests cover the change (p={tests_p:.2f})", "uncertain": False}
        )

    # --- total -> level ------------------------------------------------------
    score = max(0.0, round(sum(c["points"] for c in contributions), 2))
    level = policy.level_for(score)

    positives = sorted((c for c in contributions if c["points"] > 0), key=lambda c: -c["points"])
    top = positives[:3]
    if any(c["uncertain"] for c in top):
        review_flags.append("a top contributor has low confidence")
    truncated = any(c.truncated for c in chunks)
    if truncated:
        review_flags.append("some file diffs were truncated to fit the token budget")
    # A level decided by a hair is worth a second look too.
    for lvl in ("medium", "high", "critical"):
        t = policy.thresholds.get(lvl)
        if t is not None and 0 < abs(score - t) < 0.5:
            review_flags.append(f"score {score} is within 0.5 of the {lvl} threshold ({t})")

    return {
        "pr": {"number": pr.number, "title": pr.title, "url": pr.url, "labels": pr.labels},
        "level": level,
        "score": score,
        "thresholds": policy.thresholds,
        "fire_threshold": fire,
        "needs_human_review": bool(review_flags),
        "review_flags": review_flags,
        "top_reasons": [c["reason"] for c in top],
        "mitigations": [c["reason"] for c in contributions if c["points"] < 0],
        "facts": facts,
        "rules": rule_rows,
        "base": base_out,
        "contributions": contributions,
        "chunks": [{"id": c.id, "files": [f.path for f in c.files], "est_tokens": c.tokens, "truncated": c.truncated} for c in chunks],
        "truncated": truncated,
        "usage": {
            "requests": len(responses) + extra_requests,
            "input_tokens": sum(r.usage.input_tokens for r in responses),
            "output_tokens": sum(r.usage.output_tokens for r in responses),
        },
        "policy": str(policy.path),
    }


# =========================================================================== report


def print_report(result: dict[str, Any]) -> None:
    pr, facts = result["pr"], result["facts"]
    title = pr["title"] + (f"  (#{pr['number']})" if pr["number"] else "")
    print(f"PR risk: {result['level'].upper()}   score {result['score']}   "
          f"(medium >= {result['thresholds']['medium']}, high >= {result['thresholds']['high']}, critical >= {result['thresholds']['critical']})")
    print(f"Title:   {title}")
    print(f"Files:   {facts['files_analyzed']} analyzed, {len(facts['noise_files'])} noise dropped · "
          f"+{facts['lines_added']}/-{facts['lines_removed']} lines · size {facts['size']} · "
          f"tests {facts['test_files']} ({facts['test_ratio']:.0%})")
    if result["needs_human_review"]:
        print("Review:  NEEDS HUMAN REVIEW")
        for flag in result["review_flags"]:
            print(f"         - {flag}")

    print("\nTop reasons")
    for i, r in enumerate(result["top_reasons"], 1):
        print(f"  {i}. {r}")
    for m in result["mitigations"]:
        print(f"  - mitigation: {m}")

    print(f"\nRules (fire at p >= {result['fire_threshold']}; '?' marks a near coin-flip)")
    print(f"  {'id':<24} {'risk':<9} {'p':>5}  {'fired':<6} {'points':>6}  where")
    for row in result["rules"]:
        mark = "YES" if row["fired"] else "no"
        if row["uncertain"]:
            mark += "?"
        where = ", ".join(row["files"][:3]) + (" ..." if len(row["files"]) > 3 else "")
        print(f"  {row['id']:<24} {row['risk']:<9} {row['probability']:>5.2f}  {mark:<6} {row['points']:>6.1f}  {where}")

    print("\nCode-computed facts")
    if facts["dependency_changes"]:
        for d in facts["dependency_changes"]:
            print(f"  dependency  {d['name']}: {d['old'] or '(new)'} -> {d['new'] or '(removed)'}  [{d['bump']} -> {d['risk']}]  ({d['file']})")
    else:
        print("  dependency  none")
    print(f"  size        {facts['size']} (lines: {facts['size_by_lines']}, files: {facts['size_by_files']})")
    print(f"  categories  {facts['categories']}")
    print(f"  deleted     {facts['deleted_files'] or 'none'}")
    print(f"  tests-only  {facts['tests_only']}   docs-only {facts['docs_only']}")
    if facts["noise_files"]:
        print(f"  noise       {', '.join(facts['noise_files'])}")

    print("\nBase judgments (worst chunk)")
    for qid in ("blast_radius", "behavior_change"):
        b = result["base"][qid]
        print(f"  {qid:<20} {b['score']:.2f}/{b['max']}  conf {b['confidence']:.2f}  -> {b['nearest_level']}")
    t = result["base"]["tests_cover_change"]
    print(f"  {'tests_cover_change':<20} p={t['probability']:.2f}  {'credited' if t['fired'] else 'no credit'}")

    print("\nScore breakdown")
    for c in result["contributions"]:
        print(f"  {c['points']:>+6.2f}  {c['source']:<10} {c['reason']}")
    print(f"  {'=':>6}  {result['score']:.2f} -> {result['level'].upper()}")

    ch = result["chunks"]
    print(f"\n{result['usage']['requests']} request(s) over {len(ch)} chunk(s), ~{sum(c['est_tokens'] for c in ch)} est. state tokens, "
          f"{result['usage']['input_tokens']} input tokens billed"
          + ("  [TRUNCATED diffs]" if result["truncated"] else ""))


# =========================================================================== main


def main() -> None:
    ap = argparse.ArgumentParser(description="Assess pull-request risk with Jev and a natural-language policy.")
    ap.add_argument("ref", nargs="?", help="PR number or URL (uses `gh`)")
    ap.add_argument("--repo", help="owner/name when ref is a number")
    ap.add_argument("--body-file", help="fixture: markdown body, first line `# <title>`")
    ap.add_argument("--diff-file", help="fixture: unified diff")
    ap.add_argument("--policy", default=str(DEFAULT_POLICY), help="policy YAML (default: examples/pr-risk/policy.yaml)")
    ap.add_argument("--model", default=None, help="Jev model id (default: policy `model` or jev-latest)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-localize", action="store_true", help="skip the per-file pass that attributes fired rules to files")
    ap.add_argument("--dump-state", action="store_true", help="print the per-chunk state sent to Jev (debugging)")
    args = ap.parse_args()

    policy = load_policy(Path(args.policy).resolve())
    model = args.model or policy.raw.get("model")
    pr = load_pr(args.ref, body_file=args.body_file, diff_file=args.diff_file, repo=args.repo)

    chunks = build_chunks(pr)
    if not chunks:
        print("No content files to assess (only noise files changed).", file=sys.stderr)
        sys.exit(1)
    if args.dump_state:
        for c in chunks:
            print(f"--- {c.id} (~{c.tokens} tokens) ---", file=sys.stderr)
            print(json.dumps(c.state, indent=2), file=sys.stderr)

    responses, localization, extra = asyncio.run(run_model(chunks, policy, model, localize=not args.no_localize))
    result = assess(pr, policy, chunks, responses, localization, extra)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
