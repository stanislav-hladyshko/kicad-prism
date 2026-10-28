"""Checkouts hold the committed bytes on every OS.

Git for Windows sets ``core.autocrlf=true`` in its system config, so a clone Prism
makes there is checked out with CRLF while git holds LF. ``prism-kicad
revision-key`` hashes the working tree with the system config switched off, so
such a tree would not match its own commit or the same commit on Linux. Every
clone Prism makes sets ``core.autocrlf=false`` in the repository's own config,
and :func:`convert_existing` fixes the checkouts made before that.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

#: ``git clone`` arguments; the setting lands in the new repository's config.
CLONE_ARGS = ("-c", "core.autocrlf=false")
#: The same for ``Repo.clone_from``. GitPython refuses ``-c`` unless told it is safe; the option is this
#: constant, never caller input.
GITPYTHON_CLONE_KWARGS = {"multi_options": [" ".join(CLONE_ARGS)], "allow_unsafe_options": True}

#: One git call in the conversion. Reading and writing a large checkout is slow, but a call that hangs is not.
_GIT_TIMEOUT_SECONDS = 300


def _git(repo: Path, *args: str, autocrlf_off: bool = False) -> subprocess.CompletedProcess[str]:
    """Run git in ``repo`` without ever prompting: no terminal, no credential manager window."""

    command = ["git", "-c", "credential.helper="]
    if autocrlf_off:
        command += ["-c", "core.autocrlf=false"]
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        return subprocess.run(
            [*command, "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env=environment,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"git {' '.join(args)} timed out")


def _uses_lfs(repo: Path) -> bool:
    """Whether any tracked ``.gitattributes`` routes files through Git LFS."""

    found = _git(repo, "grep", "-q", "-e", "filter=lfs", "--", ".gitattributes", "**/.gitattributes")
    return found.returncode == 0


def convert_existing(repo: Path) -> bool:
    """Check ``repo`` out again with LF line endings and pin ``core.autocrlf=false``; True if it did.

    Left alone: a directory that is not a git repository, one with no commit, one with local
    edits, one that uses Git LFS (checking it out again would download its objects), and one that
    already has the setting. The setting is written last, so a conversion that fails part-way is
    retried at the next start.
    """

    if not (repo / ".git").exists():
        return False
    current = _git(repo, "config", "--local", "--get", "core.autocrlf").stdout.strip()
    if current == "false":
        return False
    if _git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
        return False
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        logger.info("not converting line endings of %s: it has local edits", repo)
        return False
    if _uses_lfs(repo):
        logger.info("not converting line endings of %s: it uses Git LFS", repo)
        return False
    # The index keeps its stat data, so git would call every file unchanged: drop it and check
    # the files out again, with the setting off for these two commands.
    if _git(repo, "rm", "--cached", "-r", "-q", ".", autocrlf_off=True).returncode == 0:
        if _git(repo, "reset", "--hard", "-q", autocrlf_off=True).returncode == 0:
            return _git(repo, "config", "--local", "core.autocrlf", "false").returncode == 0
    logger.warning("line-ending conversion of %s did not finish; it is retried at the next start", repo)
    _git(repo, "reset", "-q")  # put the index back
    return False


def convert_projects(projects_root: Path) -> int:
    """Run :func:`convert_existing` on every checkout under ``<root>/<kind>/<project>``."""

    converted = 0
    if not projects_root.is_dir():
        return converted
    for repo in sorted(projects_root.glob("*/*")):
        try:
            if repo.is_dir() and convert_existing(repo):
                converted += 1
                logger.info("checked %s out again with LF line endings", repo)
        except OSError as error:
            logger.warning("line-ending conversion of %s failed: %s", repo, error)
    return converted


def convert_projects_in_background(projects_root: Path) -> threading.Thread:
    """:func:`convert_projects` on a daemon thread, so API startup never waits for it."""

    thread = threading.Thread(
        target=convert_projects, args=(projects_root,), name="checkout-line-endings", daemon=True
    )
    thread.start()
    return thread
