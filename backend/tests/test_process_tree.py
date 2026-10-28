"""Starting a child in its own group and killing its tree, on any OS."""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.services import kicad_tools
from app.services.process_tree import group_popen_kwargs, kill_tree


class KillTreeTests(unittest.TestCase):
    def test_kills_a_sleeping_child(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], **group_popen_kwargs()
        )
        started = time.monotonic()
        kill_tree(process)
        self.assertIsNotNone(process.poll())
        self.assertLess(time.monotonic() - started, 10)

    def test_run_process_kills_on_timeout(self) -> None:
        with self.assertRaises(kicad_tools.ToolTimeout):
            kicad_tools._run_process(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=None, env={}, timeout=0.5, tee=False, cancel=None,
            )


class KicadPythonTests(unittest.TestCase):
    def _settings(self, value: str) -> SimpleNamespace:
        return SimpleNamespace(PRISM_KICAD_PYTHON=value, KICAD_CLI_PATH="")

    def test_configured_value_wins(self) -> None:
        self.assertEqual(kicad_tools.locate_kicad_python(self._settings(" /x/py ")), "/x/py")

    def test_windows_finds_python_beside_kicad_cli(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            (bin_dir / "python.exe").write_text("")
            cli = bin_dir / "kicad-cli.exe"
            with mock.patch.object(kicad_tools.platform, "system", return_value="Windows"), mock.patch.object(
                kicad_tools, "locate_kicad_cli", return_value=str(cli)
            ):
                self.assertEqual(kicad_tools.locate_kicad_python(self._settings("")), str(bin_dir / "python.exe"))

    def test_none_when_kicad_is_absent(self) -> None:
        with mock.patch.object(kicad_tools.platform, "system", return_value="Windows"), mock.patch.object(
            kicad_tools, "locate_kicad_cli", return_value=None
        ):
            self.assertIsNone(kicad_tools.locate_kicad_python(self._settings("")))


if __name__ == "__main__":
    unittest.main()
