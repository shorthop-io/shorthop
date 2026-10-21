"""Publish this repository's PUBLIC part to the open-source repo (github.com/shorthop-io/shorthop).

    python scripts/export_public.py --check            # only report what would block an export
    python scripts/export_public.py --dest ../shorthop-public

Development happens here, in the private repository, which also holds our own deployment
(configuration/, CI, CLAUDE.md with its internal notes). The public repo is a copy of an
ALLOWLIST of paths — a file added here stays private until it is listed below, which is the
safe default — and the copy is REFUSED while anything in it matches DENY: an internal host
name, a private address, a cloud project id. A leak becomes a failed release, never a
published secret. The script copies; committing, tagging and pushing the public repo is a
separate, deliberate step (the maintainers' release checklist).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What the public repository contains. Directories are copied whole, minus SKIP.
ALLOW = [
    "app", "migrations", "tests", "web", "docs", "deploy",
    "main.py", "requirements.txt", "requirements-dev.txt", "alembic.ini", "Dockerfile",
    ".dockerignore", "README.md", "LICENSE",
    "scripts/config_reference.py", "scripts/demo_seed.py", "scripts/export_public.py",
]
SKIP_DIRS = {"__pycache__", "node_modules", "dist", ".pytest_cache", ".venv"}
SKIP_SUFFIXES = {".db", ".pyc", ".db-journal"}
# Placed at a different path in the public repo.
MOVE = {"deploy/github/release.yml": ".github/workflows/release.yml",
        "deploy/github/test.yml": ".github/workflows/test.yml"}

# Nothing matching these may leave this repository. Extend it whenever something internal is
# found; never weaken it to make an export pass — fix the file instead.
DENY = [
    (re.compile(r"developer\.se", re.I), "internal domain"),
    (re.compile(r"\bnavdeck[-.]?(lab|test|live|\.io)", re.I), "navdeck host"),
    # Our own networks (the home LAN, the cluster's pod range) — not private ranges in general.
    (re.compile(r"\b(?:192\.168\.0|10\.234\.\d+)\.\d+\b(?!/)"), "internal address"),
    (re.compile(r"\bpgm\b|\bpgs\b|\bmiscm\b"), "internal database host"),
    (re.compile(r"navdeck-(lab|test|live)-[0-9a-f]{3,}"), "cloud project id"),
    (re.compile(r"@casumo\.com|casumo", re.I), "employer"),
    (re.compile(r"mattias\.k\.holmberg@gmail\.com"), "personal email"),
    # The org is shorthop-io (github.com/shorthop is someone else's dormant account).
    (re.compile(r"(?:ghcr\.io|github\.com|githubusercontent\.com)/shorthop/"), "wrong GitHub org"),
    (re.compile(r"\bbig\b(?= cluster| \(lab\)|/lab)|\bon big\b", re.I), "internal cluster name"),
]
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".css", ".html", ".md", ".yml", ".yaml",
                 ".toml", ".ini", ".txt", ".json", ".tpl", ""}


def files() -> list[Path]:
    out = []
    for entry in ALLOW:
        p = ROOT / entry
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not (SKIP_DIRS & set(f.relative_to(ROOT).parts)) \
                        and f.suffix not in SKIP_SUFFIXES and f.name != "package-lock.json.bak":
                    out.append(f)
    return out


def violations(paths: list[Path]) -> list[str]:
    found = []
    for f in paths:
        if f.suffix not in TEXT_SUFFIXES or f.name == "package-lock.json":
            continue
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for rx, why in DENY:
                if rx.search(line) and "DENY" not in line and f.name != "export_public.py":
                    found.append(f"{f.relative_to(ROOT)}:{n}: {why}: {line.strip()[:110]}")
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, help="a checkout of the public repository")
    ap.add_argument("--check", action="store_true", help="report only")
    a = ap.parse_args()
    paths = files()
    missing = [e for e in ALLOW if not (ROOT / e).exists()]
    bad = violations(paths)
    for m in missing:
        print(f"missing: {m}")
    for v in bad:
        print(v)
    if bad or missing:
        print(f"\nexport blocked: {len(bad)} internal reference(s), {len(missing)} missing path(s)")
        return 1
    print(f"{len(paths)} files would be published")
    if a.check or not a.dest:
        return 0
    dest = a.dest.resolve()
    if not (dest / ".git").exists():
        print(f"{dest} is not a git checkout of the public repository")
        return 1
    # Replace everything but .git, so a file deleted here is deleted there too.
    for child in dest.iterdir():
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    for f in paths:
        rel = str(f.relative_to(ROOT))
        target = dest / MOVE.get(rel, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
    print(f"copied to {dest} — review `git -C {dest} diff`, then commit, tag and push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
