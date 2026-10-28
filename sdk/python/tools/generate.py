"""Generate ``prism/_generated.py`` from the committed OpenAPI document.

Pure standard library: the SDK must install anywhere. The output is sorted and
formatted so regenerating an unchanged API produces an identical file; the
drift test compares both.

::

    python sdk/python/tools/generate.py
    python sdk/python/tools/generate.py --input /tmp/openapi.json --output /tmp/_generated.py
"""

from __future__ import annotations

import argparse
import json
import keyword
import re
from pathlib import Path
from typing import Any

SDK = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = SDK / "openapi.json"
DEFAULT_OUTPUT = SDK / "prism" / "_generated.py"

_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
_PATH_PARAM = re.compile(r"{([^}]+)}")


def _identifier(name: str) -> str:
    cleaned = re.sub(r"\W", "_", name)
    if not cleaned:
        cleaned = "_"
    if cleaned[0].isdigit():
        cleaned = "_" + cleaned
    if keyword.iskeyword(cleaned):
        cleaned += "_"
    return cleaned


def _ref_name(ref: str) -> str:
    return _identifier(str(ref).rsplit("/", 1)[-1])


def _type_for(schema: Any, annotations: dict[str, str]) -> str:
    """A Python annotation for an OpenAPI schema, best effort."""

    if not isinstance(schema, dict) or not schema:
        return "Any"
    if "$ref" in schema:
        return annotations.get(_ref_name(schema["$ref"]), "Any")
    for union in ("anyOf", "oneOf"):
        if isinstance(schema.get(union), list):
            parts: list[str] = []
            for option in schema[union]:
                part = _type_for(option, annotations)
                if part == "Any":
                    return "Any"
                if part not in parts:
                    parts.append(part)
            return " | ".join(parts) if parts else "Any"
    if "allOf" in schema:
        return "Any"

    declared = schema.get("type")
    if isinstance(declared, list):
        parts = [_type_for({**schema, "type": item}, annotations) for item in declared]
        unique = list(dict.fromkeys(parts))
        return "Any" if "Any" in unique else " | ".join(unique)

    if declared == "string":
        return "str"
    if declared == "integer":
        return "int"
    if declared == "number":
        return "float"
    if declared == "boolean":
        return "bool"
    if declared == "null":
        return "None"
    if declared == "array":
        item = _type_for(schema.get("items") or {}, annotations)
        return f"list[{item}]"
    if declared == "object":
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            return f"dict[str, {_type_for(extra, annotations)}]"
        return "dict[str, Any]"
    return "Any"


def _docstring(text: str, indent: str) -> list[str]:
    cleaned = str(text).replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return [f'{indent}"""{cleaned}"""']


def _typed_dict_name(schemas: dict[str, Any]) -> dict[str, str]:
    """Map each object schema that can be a TypedDict to a safe class name."""

    names: dict[str, str] = {}
    used: set[str] = set()
    for schema_name, schema in sorted(schemas.items()):
        if not isinstance(schema, dict):
            continue
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            continue
        if any(not str(key).isidentifier() or keyword.iskeyword(str(key)) for key in properties):
            continue
        name = _identifier(schema_name)
        if name in used:
            continue
        used.add(name)
        names[schema_name] = name
    return names


def _annotations(schemas: dict[str, Any], names: dict[str, str]) -> dict[str, str]:
    """Every component schema's annotation, keyed by its safe name."""

    annotations: dict[str, str] = {}
    for schema_name, schema in sorted(schemas.items()):
        if not isinstance(schema, dict):
            continue
        key = _identifier(schema_name)
        if schema_name in names:
            annotations[key] = names[schema_name]
        elif schema.get("type") == "object":
            annotations[key] = "dict[str, Any]"
        elif schema.get("type") == "string":
            annotations[key] = "str"
        elif schema.get("type") == "integer":
            annotations[key] = "int"
        elif schema.get("type") == "number":
            annotations[key] = "float"
        elif schema.get("type") == "boolean":
            annotations[key] = "bool"
    return annotations


def _typed_dicts(
    schemas: dict[str, Any], names: dict[str, str], annotations: dict[str, str]
) -> list[str]:
    lines: list[str] = []
    for schema_name, class_name in sorted(names.items(), key=lambda item: item[1]):
        schema = schemas[schema_name]
        properties = schema["properties"]
        required = set(schema.get("required") or [])
        total = "total=True" if required.issuperset(properties) else "total=False"
        lines.append("")
        lines.append("")
        lines.append(f"class {class_name}(TypedDict, {total}):")
        description = schema.get("description")
        if description:
            lines.extend(_docstring(description, "    "))
        for field_name in sorted(properties):
            annotation = _type_for(properties[field_name], annotations)
            lines.append(f"    {_identifier(field_name)}: {annotation}")
        if not properties:
            lines.append("    pass")
    return lines


def _response_type(operation: dict[str, Any], annotations: dict[str, str]) -> str:
    responses = operation.get("responses") or {}
    successes = sorted(status for status in responses if status.startswith("2") and status.isdigit())
    if not successes:
        return "Any"
    for status in successes:
        response = responses[status] or {}
        content = response.get("content") or {}
        if not content:
            return "None"
        media = content.get("application/json")
        if not isinstance(media, dict):
            return "Any"
        return _type_for(media.get("schema") or {}, annotations)
    return "Any"


def _default(schema: dict[str, Any], required: bool) -> str | None:
    if required:
        return None
    if "default" in schema:
        return repr(schema["default"])
    return "None"


def _parameter_annotation(
    schema: dict[str, Any], annotations: dict[str, str], required: bool, default: str | None
) -> str:
    annotation = _type_for(schema, annotations)
    if not required and default == "None" and annotation != "Any" and "None" not in annotation.split(" | "):
        annotation += " | None"
    return annotation


def _method_signature(
    operation: dict[str, Any],
    path: str,
    annotations: dict[str, str],
    has_body: bool,
) -> tuple[str, list[str], list[str]]:
    """Return ``(signature, path arguments, query arguments)`` for one operation."""

    parameters = operation.get("parameters") or []
    by_name = {
        parameter.get("name"): parameter
        for parameter in parameters
        if isinstance(parameter, dict) and parameter.get("name")
    }
    path_names = _PATH_PARAM.findall(path)

    signature = ["self"]
    path_arguments: list[str] = []
    for name in path_names:
        parameter = by_name.get(name) or {}
        schema = parameter.get("schema") or {}
        argument = _identifier(name)
        signature.append(f"{argument}: {_type_for(schema, annotations)}")
        path_arguments.append(f'"{name}": {argument}')

    query_arguments: list[str] = []
    query_signature: list[str] = []
    for parameter in parameters:
        if not isinstance(parameter, dict) or parameter.get("in") != "query":
            continue
        name = parameter.get("name")
        if not name or not str(name).isidentifier() or keyword.iskeyword(str(name)):
            continue
        schema = parameter.get("schema") or {}
        required = bool(parameter.get("required"))
        default = _default(schema, required)
        argument = _identifier(str(name))
        annotation = _parameter_annotation(schema, annotations, required, default)
        query_arguments.append(f'"{name}": {argument}')
        query_signature.append(
            f"{argument}: {annotation}" + (f" = {default}" if default is not None else "")
        )

    if query_signature or has_body:
        signature.append("*")
    signature.extend(query_signature)
    if has_body:
        signature.append("body: dict[str, Any] | None = None")

    return ", ".join(signature), path_arguments, query_arguments


def _method(
    operation: dict[str, Any],
    method: str,
    path: str,
    annotations: dict[str, str],
) -> list[str]:
    request_body = operation.get("requestBody") or {}
    has_body = isinstance(request_body, dict) and "application/json" in (
        request_body.get("content") or {}
    )
    signature, path_arguments, query_arguments = _method_signature(
        operation, path, annotations, has_body
    )
    operation_id = _identifier(operation["operationId"])
    return_type = _response_type(operation, annotations)

    lines = ["", f"    def {operation_id}({signature}) -> {return_type}:"]
    lines.extend(_docstring(operation.get("summary") or operation_id, "        "))
    lines.append("        return self._request(")
    lines.append(f'            "{method.upper()}",')
    lines.append(f'            "{path}",')
    if path_arguments:
        lines.append(f"            path_params={{{', '.join(path_arguments)}}},")
    if query_arguments:
        lines.append(f"            query={{{', '.join(query_arguments)}}},")
    if has_body:
        lines.append("            body=body,")
    lines.append("        )")
    return lines


def generate(schema: dict[str, Any]) -> str:
    schemas = (schema.get("components") or {}).get("schemas") or {}
    names = _typed_dict_name(schemas)
    annotations = _annotations(schemas, names)

    operations: list[tuple[str, str, str, dict[str, Any]]] = []
    for path, item in (schema.get("paths") or {}).items():
        for method in _METHODS:
            operation = (item or {}).get(method)
            if isinstance(operation, dict) and operation.get("operationId"):
                operations.append((operation["operationId"], method, path, operation))
    operations.sort(key=lambda entry: entry[0])

    lines = [
        '"""Generated from openapi.json by tools/generate.py. Do not edit."""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any, TypedDict",
    ]
    lines.extend(_typed_dicts(schemas, names, annotations))
    lines.extend(["", "", "class GeneratedClient:"])
    lines.append('    """One method per operation in the /api/v1 OpenAPI document."""')
    if not operations:
        lines.append("    pass")
    for _operation_id, method, path, operation in operations:
        lines.extend(_method(operation, method, path, annotations))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python sdk/python/tools/generate.py")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    schema = json.loads(args.input.read_text(encoding="utf-8"))
    output = generate(schema)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8")
    print(f"wrote {output.count('    def ')} methods to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
