"""The committed Python SDK must match the live API and its generator.

Run from the repository root to refresh both files:

    python backend/scripts/export_openapi.py && python sdk/python/tools/generate.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT / "sdk" / "python"
REFRESH = (
    "run `python backend/scripts/export_openapi.py && "
    "python sdk/python/tools/generate.py` from the repository root"
)


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


class SdkDriftTests(unittest.TestCase):
    def test_the_committed_openapi_document_matches_the_live_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exported = Path(directory) / "openapi.json"
            process = _run(
                [str(ROOT / "backend" / "scripts" / "export_openapi.py"), "--output", str(exported)]
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(
                exported.read_text(encoding="utf-8"),
                (SDK / "openapi.json").read_text(encoding="utf-8"),
                f"sdk/python/openapi.json is stale; {REFRESH}.",
            )

    def test_the_committed_generated_client_matches_the_generator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / "_generated.py"
            process = _run(
                [
                    str(SDK / "tools" / "generate.py"),
                    "--input",
                    str(SDK / "openapi.json"),
                    "--output",
                    str(generated),
                ]
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(
                generated.read_text(encoding="utf-8"),
                (SDK / "prism" / "_generated.py").read_text(encoding="utf-8"),
                f"sdk/python/prism/_generated.py is stale; {REFRESH}.",
            )

    def test_the_sdk_tests_pass_under_the_backend_python(self) -> None:
        process = _run(["-m", "unittest", "discover", "-s", "sdk/python/tests"])
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)


if __name__ == "__main__":
    unittest.main()
