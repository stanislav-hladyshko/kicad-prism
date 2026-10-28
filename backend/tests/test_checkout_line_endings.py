"""Checkouts made with core.autocrlf=true are converted to the committed bytes, and new clones keep LF."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git import Git, Repo

from app.services import checkout_line_endings as module
from app.services.checkout_line_endings import CLONE_ARGS, GITPYTHON_CLONE_KWARGS, convert_existing
from support.repo import commit_all, git, git_env, init_repo, set_identity


def _crlf_environment(folder: Path) -> dict[str, str]:
    """The test git environment on a machine whose git config sets core.autocrlf=true (as Git for Windows does)."""

    config = folder / "autocrlf.gitconfig"
    config.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
    return git_env(GIT_CONFIG_GLOBAL=str(config))


def _source_repository(folder: Path, files: dict[str, bytes]) -> Path:
    source = folder / "src"
    init_repo(source, files)
    return source


class ConvertExistingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env = _crlf_environment(self.folder)
        self.enterContext(mock.patch.dict(os.environ, self.env))
        self.source = _source_repository(self.folder, {"a.kicad_sch": b"(kicad_sch)\n"})

    def _crlf_checkout(self) -> Path:
        clone = self.folder / "clone"
        Repo.clone_from(str(self.source), str(clone), env=self.env)
        set_identity(clone)
        return clone

    def test_converts_a_clean_crlf_checkout(self) -> None:
        clone = self._crlf_checkout()
        self.assertEqual((clone / "a.kicad_sch").read_bytes(), b"(kicad_sch)\r\n")
        self.assertTrue(convert_existing(clone))
        self.assertEqual((clone / "a.kicad_sch").read_bytes(), b"(kicad_sch)\n")
        self.assertEqual(git(clone, "status", "--porcelain"), "")
        self.assertFalse(convert_existing(clone))

    def test_leaves_a_tree_with_local_edits_alone(self) -> None:
        clone = self._crlf_checkout()
        (clone / "a.kicad_sch").write_bytes(b"(kicad_sch (edited))\r\n")
        self.assertFalse(convert_existing(clone))
        self.assertEqual((clone / "a.kicad_sch").read_bytes(), b"(kicad_sch (edited))\r\n")

    def test_leaves_a_git_lfs_checkout_alone(self) -> None:
        clone = self._crlf_checkout()
        (clone / ".gitattributes").write_bytes(b"*.step filter=lfs diff=lfs merge=lfs -text\n")
        commit_all(clone, "lfs")
        self.assertFalse(convert_existing(clone))
        self.assertEqual((clone / "a.kicad_sch").read_bytes(), b"(kicad_sch)\r\n")

    def test_a_conversion_that_fails_part_way_is_retried(self) -> None:
        clone = self._crlf_checkout()
        real = module._git

        def failing_reset(repo: Path, *args: str, **kwargs: bool) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("reset", "--hard"):
                return subprocess.CompletedProcess(args, 1, "", "boom")
            return real(repo, *args, **kwargs)

        with mock.patch.object(module, "_git", side_effect=failing_reset):
            self.assertFalse(convert_existing(clone))
        # The setting is written last, so nothing marks the checkout as done.
        self.assertNotEqual(real(clone, "config", "--local", "--get", "core.autocrlf").stdout.strip(), "false")
        self.assertTrue(convert_existing(clone))
        self.assertEqual((clone / "a.kicad_sch").read_bytes(), b"(kicad_sch)\n")
        self.assertEqual(git(clone, "status", "--porcelain"), "")


class CloneKeepsLfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env = _crlf_environment(self.folder)
        self.source = _source_repository(self.folder, {"a.txt": b"x\n"})

    def test_git_clone_args(self) -> None:
        clone = self.folder / "plain"
        with Git().custom_environment(**self.env):
            Git().clone(*CLONE_ARGS, str(self.source), str(clone))
        self.assertEqual((clone / "a.txt").read_bytes(), b"x\n")

    def test_gitpython_clone_is_not_refused_and_keeps_lf(self) -> None:
        clone = self.folder / "gitpython"
        Repo.clone_from(str(self.source), str(clone), env=self.env, **GITPYTHON_CLONE_KWARGS)
        self.assertEqual((clone / "a.txt").read_bytes(), b"x\n")

    def test_gitpython_refuses_the_option_without_the_flag(self) -> None:
        """Why the flag is there: this is the failure that broke every import."""

        with self.assertRaises(Exception) as caught:
            Repo.clone_from(
                str(self.source), str(self.folder / "refused"), env=self.env,
                multi_options=GITPYTHON_CLONE_KWARGS["multi_options"],
            )
        self.assertIn("not allowed", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
