"""An open archive snapshot must not stop prism-kicad from replacing the file (Windows)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from app.services.design_bundle.archive import _open_shared


class SharedOpenTests(unittest.TestCase):
    def test_reads_the_file(self) -> None:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "a.prism"
        path.write_bytes(b"0123456789")
        with _open_shared(path) as handle:
            self.assertEqual(handle.read(4), b"0123")
            self.assertEqual(handle.read(3), b"456")

    @unittest.skipUnless(sys.platform == "win32", "Windows blocks deleting a file opened without sharing")
    def test_the_file_can_be_deleted_while_open(self) -> None:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "a.prism"
        path.write_bytes(b"x")
        with _open_shared(path):
            path.unlink()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
