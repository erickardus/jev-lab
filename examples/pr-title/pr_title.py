"""Catalogue a pull request: pick its Conventional Commits type and rewrite the title.

A PR arrives called "fixes" or "address review comments". A human reading the
list of open PRs cannot tell a dependency bump from a schema migration. This
gives every PR a prefix that says what kind of change it is:

    address review comments   ->  refactor(pr-risk): address review comments
    bump stripe to 12.1       ->  build(deps): bump stripe to 12.1
    add SSO login             ->  feat(auth)!: add SSO login

Jev answers one Choice over the diff -- which of these ten kinds of change is
this -- plus a Noul for "does this break existing callers" and one for "is the
author's own wording informative". Everything else is code: the scope comes
from the changed paths, the `!` from the breaking Noul plus a literal marker in
the body, the final string from string concatenation, and the decision of
whether to touch the title at all from two thresholds.

The classifier is deliberately unlike `pr-risk`, which fans one question per
rule across chunks because it must not miss a single dangerous line. A type is
a property of the change as a whole, so this sends one request holding every
file at a summarised, truncated size, and says so when the diff did not fit.

Runs as a GitHub Action (`.github/workflows/pr-title.yml`) and from a terminal:
  uv run examples/pr-title/pr_title.py 7 --repo owner/name
  uv run examples/pr-title/pr_title.py 7 --apply          # rewrite the title
  uv run examples/pr-title/pr_title.py 7 --apply --labels # ...and set the type label
  uv run examples/pr-title/pr_title.py --body-file f.md --diff-file f.diff
  uv run examples/pr-title/pr_title.py --fixtures         # classify the bundled samples

Exit codes: 0 fine, 1 the title should change and --check was given, 2 load error.

Env:
  TYPESAFE_API_KEY   required
  PR_TITLE_TYPES     path to a taxonomy file (default: examples/pr-title/types.yaml)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, TypeSafeClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jevlab.cost import cost_usd  # noqa: E402
from jevlab.pr import FileDiff, PullRequest, PullRequestError, dependency_changes, estimate_tokens, load_pr  # noqa: E402

load_dotenv()

HERE = Path(__file__).resolve().parent
DEFAULT_TYPES = HERE / "types.yaml"

# One request, so the budget is spent on breadth rather than depth: every file
# is represented, each one truncated, and the total capped well under Jev's 32k.
MAX_STATE_TOKENS = 18_000
MAX_FILE_CHARS = 2_400
MAX_FILES = 40
MAX_BODY_CHARS = 1_200
TIMEOUT_S = 20.0

# `type(scope)!: description`
TITLE_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<rest>.*)$", re.S)


# ----------------------------------------------------------------------------- taxonomy

@dataclass
class Taxonomy:
    raw: dict[str, Any]
    path: Path

    @property
    def types(self) -> dict[str, dict]:
        return self.raw["types"]

    @property
    def thresholds(self) -> dict[str, float]:
        return self.raw.get("thresholds", {})

    def t(self, name: str, default: float) -> float:
        return float(self.thresholds.get(name, default))

    def label_for(self, type_name: str) -> str | None:
        return (self.types.get(type_name) or {}).get("label")

    def accepts_existing(self, chosen: str, existing: str | None) -> bool:
        """Is a type already on the title an acceptable way of saying `chosen`?"""
        if not existing or existing not in self.types:
            return False
        return existing == chosen or existing in ((self.types.get(chosen) or {}).get("accept_existing") or [])

    def choice(self) -> Choice:
        # Jev matches criteria literally, so each option carries both what it is
        # and what it is not; the `not` clauses are what keep feat/fix/refactor apart.
        criteria = {}
        for name, spec in self.types.items():
            desc = " ".join(str(spec.get("description", "")).split())
            neg = " ".join(str(spec.get("not", "")).split())
            criteria[name] = f"{desc} {neg}".strip()
        return Choice(
            instructions={
                "question": "What kind of change do the code changes in `files` make?",
                "judge_from": (
                    "The added (+) and removed (-) lines in each `files[].diff`, and the paths. "
                    "`title` and `body` state the author's intent and are context only; when they "
                    "disagree with the diff, the diff is what happened."
                ),
            },
            criteria=criteria,
        )

    def breaking(self) -> Noul:
        spec = self.raw.get("breaking") or {}
        # YAML reads `true:` / `false:` as booleans; Noul wants string keys.
        crit = {str(k).lower(): " ".join(str(v).split()) for k, v in (spec.get("criteria") or {}).items()}
        return Noul(
            instructions=" ".join(str(spec.get("description", "The change breaks existing callers")).split()),
            criteria=crit or None,
        )

    def title_describes(self) -> Noul:
        crit = {str(k).lower(): " ".join(str(v).split()) for k, v in ((self.raw.get("title_describes") or {}).get("criteria") or {}).items()}
        return Noul(
            instructions="`title` tells a reader what this change does, judged against the changes in `files`",
            criteria=crit or None,
        )

    @property
    def body_markers(self) -> list[str]:
        return (self.raw.get("breaking") or {}).get("body_markers") or []


def load_taxonomy(path: Path) -> Taxonomy:
    doc = yaml.safe_load(path.read_text()) or {}
    if not doc.get("types"):
        raise ValueError(f"{path}: no `types` defined")
    return Taxonomy(raw=doc, path=path)


# ----------------------------------------------------------------------------- state

def build_state(pr: PullRequest, tax: Taxonomy) -> tuple[dict, bool]:
    """Every content file, truncated, under one budget. Returns (state, truncated)."""
    files = sorted(pr.content_files, key=lambda f: -(f.added + f.removed))
    truncated = len(files) > MAX_FILES
    entries: list[dict] = []
    used = 0
    for f in files[:MAX_FILES]:
        text = f.text(MAX_FILE_CHARS)
        cost = estimate_tokens(text)
        if used + cost > MAX_STATE_TOKENS:
            # Still name the file, so a big PR's shape survives even when its
            # body does not; a silently dropped file would skew the type.
            entries.append({"path": f.path, "status": f.status, "added": f.added, "removed": f.removed, "diff": "[omitted: budget]"})
            truncated = True
            continue
        entries.append({"path": f.path, "status": f.status, "added": f.added, "removed": f.removed, "diff": text})
        used += cost
        if len(text) < len(f.text()):
            truncated = True
    state = {
        "title": pr.title,
        "body": (pr.body or "")[:MAX_BODY_CHARS],
        "files": entries,
    }
    if noise := pr.noise_files:
        state["also_changed_but_not_shown"] = [f.path for f in noise][:20]
    return state, truncated


# ----------------------------------------------------------------------------- scope (code)

def scope_for(pr: PullRequest, tax: Taxonomy) -> str | None:
    """The `(scope)` in `type(scope):`, from the paths. Never asked of the model."""
    content = pr.content_files or pr.files
    # Tests mirror the tree they test, so src/billing + tests/billing would have
    # no common directory. Ignore tests unless tests are all there is.
    paths = [f.path for f in content if not f.is_test] or [f.path for f in content]
    if not paths:
        return None

    # Dependency-only changes are conventionally scoped `deps`.
    if all(f.is_manifest or f.is_noise for f in pr.files) and dependency_changes(pr):
        return "deps"

    scope_map: dict[str, Any] = tax.raw.get("scope_map") or {}
    mapped: set[str] = set()
    for p in paths:
        hit = next((k for k in scope_map if p.startswith(k)), None)
        if hit is None:
            mapped.add("")
            continue
        value = scope_map[hit]
        if value is None:  # use the next path segment after the prefix
            rest = p[len(hit):].split("/")
            mapped.add(rest[0] if len(rest) > 1 and rest[0] else "")
        else:
            mapped.add(str(value))
    mapped.discard("")
    if len(mapped) == 1:
        return mapped.pop()
    if mapped:
        return None  # several scopes: no honest single answer

    # Fall back to the deepest directory every file shares.
    parts = [p.split("/")[:-1] for p in paths]
    common: list[str] = []
    for segs in zip(*parts):
        if len(set(segs)) == 1:
            common.append(segs[0])
        else:
            break
    return common[-1] if common else None


def compose(type_name: str, scope: str | None, breaking: bool, description: str) -> str:
    # `ci(ci):` and `docs(docs):` say the same thing twice.
    if scope == type_name:
        scope = None
    # "fix nullpointer" under type `fix` becomes "fix: nullpointer". Only an exact
    # match of the type word is dropped: "bump axios from 1.6.7" keeps its verb,
    # because "bump" carries meaning that "fix" in "fix: fix ..." does not.
    words = description.split()
    if words and words[0].lower().strip(":") == type_name and len(words) > 1:
        description = " ".join(words[1:])
    return f"{type_name}{f'({scope})' if scope else ''}{'!' if breaking else ''}: {description}".strip()


def strip_prefix(title: str) -> tuple[str, dict | None]:
    """Split an existing `type(scope)!: rest` title. Returns (description, parsed or None)."""
    m = TITLE_RE.match(title.strip())
    if not m:
        return title.strip(), None
    d = m.groupdict()
    return (d["rest"] or "").strip(), d


# ----------------------------------------------------------------------------- judge

@dataclass
class Verdict:
    type: str          # what goes in the title (may be the author's word)
    chosen_type: str   # what Jev picked
    type_probs: dict[str, float]
    confidence: float
    runner_up: str
    margin: float
    breaking: float
    breaking_reason: str
    title_describes: float
    scope: str | None
    current_title: str
    proposed_title: str
    changed: bool
    apply_ok: bool
    kept_existing: bool = False
    reasons: list[str] = field(default_factory=list)
    truncated: bool = False
    input_tokens: int = 0
    dependencies: list[dict] = field(default_factory=list)


def judge(pr: PullRequest, tax: Taxonomy, client: TypeSafeClient, dump: bool = False) -> Verdict:
    state, truncated = build_state(pr, tax)
    if dump:
        print(json.dumps(state, indent=2)[:20000], file=sys.stderr)
    resp = client.system_one(
        state=state,
        questions={"type": tax.choice(), "breaking": tax.breaking(), "title_describes": tax.title_describes()},
        timeout=TIMEOUT_S,
    )
    ch = resp.choices["type"]
    probs = dict(sorted(ch.probabilities.items(), key=lambda kv: -kv[1]))
    ordered = list(probs.items())
    top_p = ordered[0][1] if ordered else 0.0
    runner_up, second_p = (ordered[1] if len(ordered) > 1 else ("", 0.0))
    margin = top_p - second_p

    # A body that says BREAKING CHANGE is a statement of fact by the author;
    # believe it in code rather than spending a question on it.
    marker = next((m for m in tax.body_markers if m.lower() in (pr.body or "").lower()), None)
    p_breaking = resp.nouls["breaking"].noul
    is_breaking = bool(marker) or p_breaking >= tax.t("breaking", 0.70)
    breaking_reason = f"body says {marker}" if marker else f"p={p_breaking:.2f}"

    description, parsed = strip_prefix(pr.title)
    existing_type = (parsed or {}).get("type")
    scope = scope_for(pr, tax)

    reasons: list[str] = []
    # The author already labelled it, and the taxonomy says that label is a fair
    # way to say what Jev chose. Rewriting it would be noise, so keep their word.
    final_type = ch.choice
    kept = False
    if existing_type and existing_type != ch.choice and tax.accepts_existing(ch.choice, existing_type):
        final_type, kept = existing_type, True
        reasons.append(f"kept the author's `{existing_type}`, accepted as a way of saying `{ch.choice}`")

    # A scope the author wrote by hand is better local knowledge than a path prefix.
    scope = (parsed or {}).get("scope") or scope
    proposed = compose(final_type, scope, is_breaking, description)

    apply_ok = True
    if top_p < tax.t("apply", 0.55):
        apply_ok = False
        reasons.append(f"top type {ch.choice} is only p={top_p:.2f} (apply floor {tax.t('apply', 0.55):.2f})")
    if margin < tax.t("margin", 0.15) and runner_up:
        apply_ok = False
        reasons.append(f"{ch.choice} beats {runner_up} by only {margin:.2f} (margin {tax.t('margin', 0.15):.2f})")
    if truncated:
        reasons.append("the diff did not fit in one request and was truncated")
    if not description:
        apply_ok = False
        reasons.append("the title has no text after the prefix")

    return Verdict(
        type=final_type, chosen_type=ch.choice, type_probs=probs, kept_existing=kept, confidence=ch.confidence, runner_up=runner_up, margin=margin,
        breaking=p_breaking, breaking_reason=breaking_reason,
        title_describes=resp.nouls["title_describes"].noul,
        scope=scope, current_title=pr.title, proposed_title=proposed,
        changed=proposed.strip() != pr.title.strip(), apply_ok=apply_ok, reasons=reasons,
        truncated=truncated, input_tokens=resp.usage.input_tokens or 0,
        dependencies=dependency_changes(pr),
    )


# ----------------------------------------------------------------------------- apply

def gh(*args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"} if not os.environ.get("GH_TOKEN") else dict(os.environ)
    proc = subprocess.run(["gh", *args], check=False, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise PullRequestError(f"gh {' '.join(args[:3])} failed: {(proc.stderr or proc.stdout).strip().splitlines()[-1:] or ''}")
    return proc.stdout


def apply(pr: PullRequest, v: Verdict, tax: Taxonomy, repo: str | None, labels: bool) -> list[str]:
    done: list[str] = []
    repo_args = ["--repo", repo] if repo else []
    ref = str(pr.number)
    if v.changed and v.apply_ok:
        gh("pr", "edit", ref, *repo_args, "--title", v.proposed_title)
        done.append(f"title -> {v.proposed_title}")
    if labels and v.apply_ok:
        want = tax.label_for(v.type)
        if want:
            stale = [l for l in pr.labels if l.startswith("type: ") and l != want]
            args = ["pr", "edit", ref, *repo_args, "--add-label", want]
            for label in stale:
                args += ["--remove-label", label]
            try:
                gh(*args)
                done.append(f"label -> {want}" + (f" (removed {', '.join(stale)})" if stale else ""))
            except PullRequestError as e:
                # `gh` refuses a label the repo has not defined. That is a repo
                # setup detail, not a reason to fail a run that retitled fine.
                done.append(f"label {want!r} not applied ({e})")
    return done


# ----------------------------------------------------------------------------- report

def report(v: Verdict, tax: Taxonomy) -> str:
    L = []
    verb = "would set" if v.apply_ok else "suggests"
    L.append(f"{v.current_title}")
    if v.changed:
        L.append(f"  {verb}: {v.proposed_title}")
    else:
        L.append("  title already correct; nothing to do")
    L.append("")
    bar = ", ".join(f"{k} {p:.2f}" for k, p in list(v.type_probs.items())[:4])
    kept = f"  [kept the author's `{v.type}`]" if v.kept_existing else ""
    L.append(f"type       {v.chosen_type}  (confidence {v.confidence:.2f}; {bar}){kept}")
    L.append(f"scope      {v.scope or '-'}   [from the changed paths, in code]")
    L.append(f"breaking   {'yes' if '!' in v.proposed_title.split(':')[0] else 'no'}  ({v.breaking_reason})")
    vague = v.title_describes < tax.t("title_describes", 0.45)
    L.append(f"title text {'vague, worth rewording by hand' if vague else 'describes the change'}  (p describes {v.title_describes:.2f})")
    if v.dependencies:
        d = ", ".join(f"{x['name']} {x['old']}->{x['new']} ({x['bump']})" for x in v.dependencies[:4])
        L.append(f"deps       {d}")
    if v.reasons:
        L.append("")
        L.append("Not applying automatically:" if not v.apply_ok else "Notes:")
        for r in v.reasons:
            L.append(f"  - {r}")
    L.append("")
    L.append(f"{v.input_tokens:,} input tokens · ${cost_usd(v.input_tokens):.5f}")
    return "\n".join(L)


# ----------------------------------------------------------------------------- cli

FIXTURES = [
    ("../pr-risk/fixtures/auth-refactor.md", "../pr-risk/fixtures/auth-refactor.diff"),
    ("../pr-risk/fixtures/copy-change.md", "../pr-risk/fixtures/copy-change.diff"),
    ("../pr-risk/fixtures/major-bump-payments.md", "../pr-risk/fixtures/major-bump-payments.diff"),
    ("../pr-risk/fixtures/patch-bump.md", "../pr-risk/fixtures/patch-bump.diff"),
    ("fixtures/null-crash.md", "fixtures/null-crash.diff"),
    ("fixtures/export-csv.md", "fixtures/export-csv.diff"),
    ("fixtures/ci-cache.md", "fixtures/ci-cache.diff"),
]


def run_fixtures(tax: Taxonomy) -> int:
    rows, total = [], 0
    with TypeSafeClient() as client:
        for body, diff in FIXTURES:
            b, d = (HERE / body).resolve(), (HERE / diff).resolve()
            if not (b.exists() and d.exists()):
                rows.append(("?", "-", f"missing fixture {body}", ""))
                continue
            pr = load_pr(None, body_file=b, diff_file=d)
            v = judge(pr, tax, client)
            total += v.input_tokens
            mark = "🔒" if v.kept_existing else "✅" if v.apply_ok else "⚠️ "
            rows.append((mark, f"{v.type_probs[v.chosen_type]:.2f}", v.current_title, v.proposed_title))
    w = max(len(r[2]) for r in rows)
    print(f"|    | p    | {'current title'.ljust(w)} | proposed |")
    print(f"|----|------|-{'-' * w}-|----------|")
    for mark, p, cur, prop in rows:
        print(f"| {mark} | {p} | {cur.ljust(w)} | {prop} |")
    print(f"\n{len(rows)} PRs · {total:,} input tokens · ${cost_usd(total):.4f} · ${cost_usd(total)/max(len(rows),1):.5f} each")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ref", nargs="?", help="PR number or URL")
    ap.add_argument("--repo", help="owner/name when ref is a number")
    ap.add_argument("--body-file")
    ap.add_argument("--diff-file")
    ap.add_argument("--types", default=os.environ.get("PR_TITLE_TYPES", str(DEFAULT_TYPES)))
    ap.add_argument("--apply", action="store_true", help="rewrite the PR title on GitHub")
    ap.add_argument("--labels", action="store_true", help="with --apply, also set the type label")
    ap.add_argument("--check", action="store_true", help="exit 1 if the title should change (for a required check)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fixtures", action="store_true", help="classify the bundled sample PRs")
    ap.add_argument("--dump-state", action="store_true", help="print what Jev sees, to stderr")
    args = ap.parse_args()

    try:
        tax = load_taxonomy(Path(args.types).resolve())
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.fixtures:
        return run_fixtures(tax)

    try:
        pr = load_pr(args.ref, body_file=args.body_file, diff_file=args.diff_file, repo=args.repo)
    except (PullRequestError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not pr.files:
        print("error: the PR has no diff to classify", file=sys.stderr)
        return 2

    with TypeSafeClient() as client:
        v = judge(pr, tax, client, dump=args.dump_state)

    applied: list[str] = []
    if args.apply:
        if pr.number is None:
            print("error: --apply needs a real PR, not fixture files", file=sys.stderr)
            return 2
        try:
            applied = apply(pr, v, tax, args.repo, args.labels)
        except PullRequestError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    if args.json:
        out = {k: getattr(v, k) for k in v.__dataclass_fields__}
        out["applied"] = applied
        print(json.dumps(out, indent=2))
    else:
        print(report(v, tax))
        for a in applied:
            print(f"applied: {a}")

    return 1 if (args.check and v.changed and v.apply_ok) else 0


if __name__ == "__main__":
    sys.exit(main())
