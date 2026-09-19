"""Fetch a pull request and split its diff into per-file hunks.

Both PR examples (acceptance-criteria coverage, risk assessment) need the same
three things: the PR title/body, the unified diff, and that diff broken into
small, noise-free pieces that fit Jev's context budget. Keep that here.

Jev has a 32k-token budget for state + the longest question, and its accuracy
drops when the state carries irrelevant detail, so we drop lockfiles, generated
files, and binaries in code before anything reaches the model.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Files that are almost never evidence for anything and only eat context.
NOISE_PATTERNS = [
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock|Cargo\.lock|Gemfile\.lock|composer\.lock|go\.sum)$",
    r"\.(min\.js|min\.css|map|snap|lock)$",
    r"(^|/)(dist|build|node_modules|vendor|__snapshots__|\.next)/",
    r"\.(png|jpe?g|gif|svg|ico|webp|woff2?|ttf|eot|pdf|zip|gz|jar|so|dylib)$",
    r"\.generated\.|_pb2\.py$|\.pb\.go$",
]
_NOISE = re.compile("|".join(f"(?:{p})" for p in NOISE_PATTERNS))

# Dependency manifests: kept as evidence, but flagged so tools can treat them specially.
MANIFEST_PATTERNS = re.compile(
    r"(^|/)(package\.json|pyproject\.toml|requirements[^/]*\.txt|setup\.py|setup\.cfg|"
    r"Pipfile|Cargo\.toml|go\.mod|Gemfile|composer\.json|pom\.xml|build\.gradle(\.kts)?|"
    r"\.csproj|Podfile|pubspec\.yaml|mix\.exs)$"
)


@dataclass
class Hunk:
    file: str
    header: str  # the @@ ... @@ line
    body: str  # the hunk lines, including +/-/space prefixes
    added: int = 0
    removed: int = 0

    @property
    def id(self) -> str:
        return f"{self.file}{self.header.split('@@')[1].strip().split(' ')[0]}"

    def text(self) -> str:
        return f"{self.header}\n{self.body}"


@dataclass
class FileDiff:
    path: str
    old_path: str | None
    status: str  # added | deleted | modified | renamed
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def added(self) -> int:
        return sum(h.added for h in self.hunks)

    @property
    def removed(self) -> int:
        return sum(h.removed for h in self.hunks)

    @property
    def is_noise(self) -> bool:
        return bool(_NOISE.search(self.path)) or self.is_binary

    @property
    def is_manifest(self) -> bool:
        return bool(MANIFEST_PATTERNS.search(self.path))

    @property
    def is_test(self) -> bool:
        p = self.path.lower()
        return bool(
            re.search(r"(^|/)(tests?|__tests__|spec|specs)/", p)
            or re.search(r"(_test|\.test|_spec|\.spec|test_[^/]*)\.[a-z]+$", p)
        )

    def text(self, max_chars: int | None = None) -> str:
        out = "\n".join(h.text() for h in self.hunks)
        if max_chars and len(out) > max_chars:
            out = out[:max_chars] + f"\n... [truncated {len(out) - max_chars} chars]"
        return out


@dataclass
class PullRequest:
    number: int | None
    title: str
    body: str
    files: list[FileDiff]
    url: str | None = None
    base: str | None = None
    labels: list[str] = field(default_factory=list)

    @property
    def content_files(self) -> list[FileDiff]:
        """Files worth showing to the model."""
        return [f for f in self.files if not f.is_noise]

    @property
    def noise_files(self) -> list[FileDiff]:
        return [f for f in self.files if f.is_noise]


# --------------------------------------------------------------------------- diff parsing

_DIFF_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@.*$")


def parse_unified_diff(diff: str) -> list[FileDiff]:
    """Split a `git diff` / `gh pr diff` output into files and hunks."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    body_lines: list[str] = []

    def flush_hunk() -> None:
        nonlocal hunk, body_lines
        if hunk is not None and current is not None:
            hunk.body = "\n".join(body_lines)
            current.hunks.append(hunk)
        hunk, body_lines = None, []

    for line in diff.splitlines():
        m = _DIFF_HEADER.match(line)
        if m:
            flush_hunk()
            old, new = m.group(1), m.group(2)
            current = FileDiff(path=new, old_path=old if old != new else None, status="modified")
            files.append(current)
            continue
        if current is None:
            continue
        if line.startswith("new file mode"):
            current.status = "added"
        elif line.startswith("deleted file mode"):
            current.status = "deleted"
        elif line.startswith("rename from") or line.startswith("similarity index"):
            current.status = "renamed"
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            current.is_binary = True
        elif _HUNK_HEADER.match(line):
            flush_hunk()
            hunk = Hunk(file=current.path, header=line, body="")
        elif hunk is not None:
            if line.startswith("\\ No newline"):
                continue
            body_lines.append(line)
            if line.startswith("+"):
                hunk.added += 1
            elif line.startswith("-"):
                hunk.removed += 1
    flush_hunk()
    return files


# --------------------------------------------------------------------------- loading


def _gh(*args: str) -> str:
    # A stale GITHUB_TOKEN in the shell overrides gh's stored login; prefer the login.
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, env=env).stdout


def load_pr(
    ref: str | None = None,
    *,
    body_file: str | Path | None = None,
    diff_file: str | Path | None = None,
    repo: str | None = None,
) -> PullRequest:
    """Load a PR from GitHub via `gh`, or from local fixture files.

    `ref` is a PR number or URL. When `body_file` and `diff_file` are given the
    PR is built from those instead (title = first line of the body file if it
    starts with '# ').
    """
    if body_file or diff_file:
        if not (body_file and diff_file):
            raise ValueError("--body-file and --diff-file must be given together")
        raw = Path(body_file).read_text()
        title, body = "", raw
        if raw.startswith("# "):
            title, _, body = raw.partition("\n")
            title = title[2:].strip()
        return PullRequest(
            number=None,
            title=title or Path(body_file).stem,
            body=body.strip(),
            files=parse_unified_diff(Path(diff_file).read_text()),
        )

    if not ref:
        raise ValueError("a PR number/URL or fixture files are required")
    repo_args = ["--repo", repo] if repo else []
    meta = json.loads(
        _gh("pr", "view", ref, *repo_args, "--json", "number,title,body,url,baseRefName,labels")
    )
    diff = _gh("pr", "diff", ref, *repo_args)
    return PullRequest(
        number=meta["number"],
        title=meta["title"],
        body=meta.get("body") or "",
        files=parse_unified_diff(diff),
        url=meta.get("url"),
        base=meta.get("baseRefName"),
        labels=[l["name"] for l in meta.get("labels", [])],
    )


# --------------------------------------------------------------------------- misc


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for budgeting against Jev's limits."""
    return len(text) // 4 + 1


def dependency_changes(pr: PullRequest) -> list[dict]:
    """Best-effort extraction of `name: old -> new` version bumps from manifests.

    Version comparison is arithmetic, so it belongs in code, not in a question
    to the model. Returns a list of {file, name, old, new, bump} where bump is
    major | minor | patch | other.
    """
    ver = r"[\^~>=<]*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?[\w.\-+]*"
    patterns = [
        # "name": "^1.2.3"  (package.json)
        re.compile(r'"([@\w./-]+)"\s*:\s*"(' + ver + r')"'),
        # name = "1.2.3" / name = "^1.2.3" (Cargo.toml, poetry)
        re.compile(r'^([\w.-]+)\s*=\s*"(' + ver + r')"'),
        # "name>=1.2.3" / name==1.2.3 (pyproject deps, requirements)
        re.compile(r'"?([\w.\[\]-]+)\s*[=><~!]+\s*(' + ver + r')"?'),
        # module v1.2.3 (go.mod)
        re.compile(r"^\s*([\w./-]+)\s+(" + ver + r")"),
    ]
    out: list[dict] = []
    for f in pr.files:
        if not f.is_manifest:
            continue
        removed: dict[str, tuple[str, tuple]] = {}
        added: dict[str, tuple[str, tuple]] = {}
        for h in f.hunks:
            for line in h.body.splitlines():
                if not line[:1] in "+-":
                    continue
                content = line[1:].strip()
                for pat in patterns:
                    m = pat.search(content)
                    if m:
                        name, full = m.group(1), m.group(2)
                        parts = tuple(int(x) if x else 0 for x in m.groups()[2:5])
                        (added if line[0] == "+" else removed)[name] = (full, parts)
                        break
        for name in added.keys() & removed.keys():
            (o, op), (n, np_) = removed[name], added[name]
            if op == np_:
                continue
            bump = "major" if op[0] != np_[0] else "minor" if op[1] != np_[1] else "patch" if op[2] != np_[2] else "other"
            out.append({"file": f.path, "name": name, "old": o, "new": n, "bump": bump})
        for name in added.keys() - removed.keys():
            out.append({"file": f.path, "name": name, "old": None, "new": added[name][0], "bump": "new"})
        for name in removed.keys() - added.keys():
            out.append({"file": f.path, "name": name, "old": removed[name][0], "new": None, "bump": "removed"})
    return out
