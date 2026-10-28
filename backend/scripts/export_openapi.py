"""Write the ``/api/v1`` OpenAPI document the Python SDK is generated from.

The document is committed at ``sdk/python/openapi.json`` and read by
``sdk/python/tools/generate.py``. It is written sorted, so regenerating it
produces the same bytes when the API has not changed; the drift test in
``backend/tests/test_sdk_drift.py`` compares both files.

Run from the repository root, or from ``backend/``::

    python backend/scripts/export_openapi.py
    python scripts/export_openapi.py --output /tmp/openapi.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_BACKEND = Path(__file__).resolve().parents[1]
_SDK = _BACKEND.parent / "sdk" / "python"
DEFAULT_OUTPUT = _SDK / "openapi.json"


def live_openapi() -> dict[str, Any]:
    """The live ``/api/v1`` document, imported the way ``app.core.api_contract`` does."""

    if str(_BACKEND) not in sys.path:
        sys.path.insert(0, str(_BACKEND))
    from app.main import app

    return app.openapi()


def write_openapi(schema: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python backend/scripts/export_openapi.py")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="where to write the document (default: sdk/python/openapi.json)",
    )
    args = parser.parse_args(argv)
    schema = live_openapi()
    write_openapi(schema, args.output)
    print(f"wrote {len(schema.get('paths', {}))} paths to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
