"""The ``/api/v1`` operations snapshot (ADR scripting-and-mcp §3.2).

``backend/openapi/operations.v1.json`` records every published operation: its id,
method, path, and whether its success response is typed. ``tests/test_api_contract.py``
compares the live OpenAPI document with it:

- an operation that disappears, or whose method or path changes, is a breaking change;
- a typed operation may not become untyped;
- a new operation must be typed.

So the number of untyped operations can only go down. After a deliberate change,
rewrite the snapshot with ``python -m app.core.api_contract --write`` and review its
diff with the change.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SNAPSHOT = Path(__file__).resolve().parents[2] / "openapi" / "operations.v1.json"
_METHODS = ("get", "put", "post", "delete", "patch")


def _typed(operation: dict[str, Any]) -> bool:
    """A success response with a JSON schema, a declared non-JSON body, or no body."""
    for status, response in (operation.get("responses") or {}).items():
        if not status.startswith("2"):
            continue
        content = response.get("content")
        if status == "204" or not content:
            return True
        for media_type, body in content.items():
            if media_type != "application/json" or (body.get("schema") or {}):
                return True
    return False


def operations(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path, item in schema["paths"].items():
        for method in _METHODS:
            operation = item.get(method)
            if operation is not None:
                found[operation["operationId"]] = {
                    "method": method.upper(),
                    "path": path,
                    "typed": _typed(operation),
                }
    return dict(sorted(found.items()))


def problems(current: dict[str, dict[str, Any]], recorded: dict[str, dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for op_id, was in recorded.items():
        now = current.get(op_id)
        if now is None:
            found.append(f"{op_id}: removed or renamed ({was['method']} {was['path']})")
        elif (now["method"], now["path"]) != (was["method"], was["path"]):
            found.append(f"{op_id}: moved from {was['method']} {was['path']} to {now['method']} {now['path']}")
        elif was["typed"] and not now["typed"]:
            found.append(f"{op_id}: was typed, is now untyped")
    for op_id, now in current.items():
        if op_id not in recorded and not now["typed"]:
            found.append(f"{op_id}: new operation without a typed response ({now['method']} {now['path']})")
    return found


def live() -> dict[str, dict[str, Any]]:
    from app.main import app

    return operations(app.openapi())


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.core.api_contract")
    parser.add_argument("--write", action="store_true", help="rewrite the snapshot from the live API")
    args = parser.parse_args()
    current = live()
    if args.write:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    untyped = sum(1 for op in current.values() if not op["typed"])
    print(f"{len(current)} operations, {untyped} untyped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
