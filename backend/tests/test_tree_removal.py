"""Removing a folder of read-only copies."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.services.tree_removal import remove_tree


def _models_folder() -> Path:
    root = Path(tempfile.mkdtemp()) / "models"
    copy = root / "abc" / "part.step"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b"x")
    os.chmod(copy, 0o444)
    return root


class RemoveTreeTests(unittest.TestCase):
    def test_removes_read_only_files(self) -> None:
        root = _models_folder()
        remove_tree(root)
        self.assertFalse(root.exists())

    def test_job_cleanup_removes_them_with_ignore_errors(self) -> None:
        root = _models_folder()
        remove_tree(root, ignore_errors=True)
        self.assertFalse(root.exists())

    def test_ignore_errors_skips_what_cannot_be_removed(self) -> None:
        remove_tree(Path(tempfile.gettempdir()) / "prism-no-such-folder", ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
