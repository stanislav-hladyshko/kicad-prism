"""The ``capture_profile`` ops tool (ADR scripting-and-mcp §11, D26)."""

from __future__ import annotations

from typing import Any

from app.services.mcp.registry import READ_ONLY_ANNOTATIONS, Tool, register


def _args(arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    body = {"target": arguments.get("target") or "api", "seconds": arguments.get("seconds") or 5}
    if arguments.get("worker_id"):
        body["workerId"] = arguments["worker_id"]
    return {}, {}, body


register(
    Tool(
        name="capture_profile",
        title="Capture a sampling profile",
        description=(
            "Sample the stacks of the API process (target 'api') or a prism worker's supervisor "
            "(target 'worker') for up to 10 seconds and return collapsed stacks "
            "(thread;module:function;...), most frequent first: what the process spends its time on. "
            "One profile runs at a time and at most six start per hour, so take one only while the "
            "symptom is happening. Admin only; needs ops:read."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["api", "worker"], "description": "Which process to sample."},
                "seconds": {"type": "number", "minimum": 0.1, "maximum": 10, "description": "How long to sample (default 5)."},
                "worker_id": {"type": "string", "description": "A specific prism worker; any live one when omitted."},
            },
            "additionalProperties": False,
        },
        route=("post", "/api/v1/ops/profiles"),
        args=_args,
        annotations={**READ_ONLY_ANNOTATIONS, "idempotentHint": False},
        action="ops_read",
    )
)
