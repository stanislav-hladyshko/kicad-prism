"""Which Quality gate jobs a pull request needs, from the files it changes.

``.github/workflows/dev-quality-gate.yml`` runs this in its ``changes`` job and skips the jobs a change
cannot affect. Pushes, the nightly schedule and manual runs always run everything, so a rule that
is too narrow costs one late failure on the target branch, never a missed one.

A job runs when a changed file:
- is in one of its own areas (``OWN``), or
- is a file its tests read by name from another area: the file's name (without extension) appears in
  the job's test sources (``READERS``). Tests across this repository read docs, fixtures and other
  areas' sources by path; this keeps up with new ones without a list to maintain.
Changes to CI itself or to a toolchain pin run everything.

    python scripts/ci_changes.py --base <sha> [--head HEAD]   # prints key=value lines
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Any of these runs every job.
EVERYTHING = (".github/*", ".python-version", ".node-version", "scripts/ci_changes.py")

OWN: dict[str, tuple[str, ...]] = {
    "backend": ("backend/*", "requirements/*", "requirements.txt", "fixtures/*",
                "scripts/*", "assets/*", "deploy/release/*", "sdk/*", "examples/*"),
    "frontend": ("frontend/*", "backend/tests/fixtures/*", "scripts/check-embedded-wasm.mjs",
                 "scripts/build-ecad-viewer.sh"),
    "viewer": ("kicad-prism-viewer/*", "frontend/public/prism-semantic-viewer.js", "frontend/index.html",
               "requirements/*", "fixtures/*"),
    "agent": ("tools/*",),
    # Prism's KiCad parser and design-bundle workspace (native/): fmt, clippy, tests, codegen, licences.
    "native": ("native/*",),
    "deploy": ("docker-compose*.yml", "deploy/*", "scripts/*", ".env.example", "loadtest/*"),
    "deps": ("*package.json", "*package-lock.json", "requirements/*", "backend/Dockerfile",
             "scripts/verify_dependency_identity.py"),
    # A docker build of the backend image (PRs into feature branches); see ``image_live`` for dev/main.
    "image": ("backend/Dockerfile", ".dockerignore", "native/*", "requirements/*"),
    # The live KiCad job on PRs into dev/main: the image plus what its tests exercise.
    "image_live": ("backend/*", ".dockerignore", "requirements/*", "fixtures/release-studio/*"),
}

# The test sources whose by-name reads count for a job (see the module docstring).
READERS: dict[str, tuple[str, ...]] = {
    "backend": ("backend/tests",),
    "frontend": ("frontend/src",),
    "viewer": ("kicad-prism-viewer/tests", "kicad-prism-viewer/viewer/src"),
    "agent": ("tools/tests",),
}

JOBS = tuple(OWN)
# Names too generic to mean "this file": tests write their own READMEs and packages have many indexes.
GENERIC_NAMES = {"README.md", "__init__.py", "index.ts", "index.tsx", "index.js", "conftest.py"}
_SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs"}


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


@lru_cache(maxsize=None)
def _reader_text(job: str) -> str:
    chunks = []
    for folder in READERS.get(job, ()):
        base = ROOT / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.suffix in _SOURCE_SUFFIXES and "node_modules" not in path.parts and path.is_file():
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


def _read_by_name(job: str, path: str) -> bool:
    name = Path(path).name
    stem = name.split(".")[0]
    text = _reader_text(job)
    if not text or name in GENERIC_NAMES:
        return False
    if name in text:
        return True
    # A module imported by name: ``from app.services.kicad_noise_service import …`` or ``from "./x/stem"``.
    pattern = (rf"(?:\bimport|\bfrom)\s+[\w.]*\b{re.escape(stem)}\b"
               rf"|\bfrom\s+[\"'][^\"']*/{re.escape(stem)}(?:\.[jt]sx?)?[\"']")
    return re.search(pattern, text) is not None


def needed(files: list[str]) -> dict[str, bool]:
    """Job -> whether these changed files need it."""
    if any(_matches(path, EVERYTHING) for path in files):
        return dict.fromkeys(JOBS, True)
    out = {}
    for job in JOBS:
        out[job] = any(_matches(path, OWN[job]) or (job in READERS and _read_by_name(job, path)) for path in files)
    return out


def changed_files(base: str, head: str) -> list[str]:
    result = subprocess.run(["git", "diff", "--name-only", base, head], cwd=ROOT, check=True,
                            capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if line]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--all", action="store_true", help="every job (pushes, schedules, manual runs)")
    args = parser.parse_args(argv)
    if args.all or not args.base:
        result = dict.fromkeys(JOBS, True)
        files: list[str] = []
    else:
        files = changed_files(args.base, args.head)
        result = needed(files)
    for job, value in result.items():
        print(f"{job}={'true' if value else 'false'}")
    print(f"{len(files)} changed file(s)", file=sys.stderr)
    for path in files[:200]:
        print(f"  {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
