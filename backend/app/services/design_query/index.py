"""Read a revision's semantic index for design queries (ADR §3.8).

The index is the bundle member ``projections/semantic-index.json``
(``prism.semantic_index.a2``; native/contracts/prism/field-maps/semantic-index.md).
This module turns it into the three answers the MCP read set needs: search,
one component, one net. It parses JSON only; the caller's worker thread runs
``load`` so a multi-megabyte member never blocks the event loop.

Fields used (all names are the member's own, a2):

* ``components[].reference``, ``value``, ``footprint``, ``libraryRef``,
  ``componentUid``, ``fields`` (a ``Record<string>`` whose keys include
  ``Manufacturer Part Number``, ``DNP`` and ``In BOM``), and
  ``schematicRefs[].sheetInstancePath`` / ``page`` (the component's sheet).
* ``terminals[].reference``, ``pin``, ``pinName``, ``componentUid``,
  ``netUid``, ``netName`` — the pad-resolved name — and ``schematicNetName``.
* ``nets[].name``, ``netUid``, ``netClass`` and ``aliases`` (names the net
  absorbed); ``indexes.netByName`` also maps absorbed aliases to their net.
* ``assembly.default.components`` (keyed by ``componentUid``, or by reference
  when the reference is ambiguous) carries the DNP/exclusion flags that are
  true for the default assembly.

A parsed index is immutable for a bundle revision, so it is cached by the
artifact's ``(revision key, generator)`` in a two-entry LRU. Readers treat the
returned mapping as read-only.
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from app.services import semantic_index_service

#: Two revisions cover a query and its follow-up, or both sides of a
#: comparison. A large board's parsed member is tens to hundreds of megabytes
#: of Python objects, so the bound stays small.
CACHE_SIZE = 2

_CACHE: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()

_NATURAL_PART = re.compile(r"(\d+)")
_ASSEMBLY_FLAGS = ("dnp", "excludeFromBom", "excludeFromBoard", "excludeFromSim", "excludeFromPosFiles")


def load(project: Any, commit: str) -> tuple[dict[str, Any], str | None]:
    """The parsed index at a resolved commit and the bundle's revision key.

    ``commit`` is already resolved (``design_query.revision.resolve``); the
    bundle read is the same one every semantic-index consumer makes. The
    revision key is the response's ``designKey``. A cache hit reuses the parsed
    mapping, so callers must not mutate it.
    """

    artifact = semantic_index_service.artifact_file(project, commit)
    path, revision_key = artifact
    key = (str(revision_key), str(getattr(artifact, "generator", "") or ""))
    cached = _cached(key)
    if cached is not None:
        return cached, str(revision_key) or None
    with Path(path).open("r", encoding="utf-8") as handle:
        index = json.load(handle)
    _store(key, index)
    return index, str(revision_key) or None


def clear_cache() -> None:
    """Drop every parsed index (tests and operators; the cache is process-local)."""

    with _CACHE_LOCK:
        _CACHE.clear()


def cache_stats() -> dict[str, int]:
    """The LRU's entry count and its bound (tests and operators)."""

    with _CACHE_LOCK:
        return {"entries": len(_CACHE), "maxEntries": CACHE_SIZE}


def _cached(key: tuple[str, str]) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        index = _CACHE.get(key)
        if index is None:
            return None
        _CACHE.move_to_end(key)
        return index


def _store(key: tuple[str, str], index: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = index
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_SIZE:
            _CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def natural_key(text: str) -> tuple[tuple[int, Any], ...]:
    """A case-insensitive order where ``R2`` precedes ``R10``.

    Digits compare as numbers; text compares as text. Used for references and
    net names, never for JSON output.
    """

    parts = _NATURAL_PART.split(text.casefold())
    return tuple((1, int(part)) if part.isdigit() else (0, part) for part in parts if part)


def _ordered(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best match first, then natural reference or name order.

    Exact matches rank first, then reference and net-name prefixes, then value
    and net-name substrings, then other field values. Without the tiers a
    query like "R" listed every part with an "r" in its description ahead of
    the resistors.
    """

    keyed: list[tuple[tuple[int, tuple[tuple[int, Any], ...], str], dict[str, Any]]] = []
    for hit in hits:
        label = str(hit.get("reference") or hit.get("name") or "")
        rank = int(hit.pop("_rank", 3))
        keyed.append(((rank, natural_key(label), label), hit))
    keyed.sort(key=lambda item: item[0])
    return [hit for _, hit in keyed]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def find(
    index: dict[str, Any],
    q: str,
    kinds: set[str],
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Components and nets matching ``q``, exact names first.

    Components match on reference (exact or prefix), value, or any field value
    (MPN included), case-insensitively. Nets match on name substring. An empty
    query returns nothing, so a caller cannot accidentally dump the design.
    """

    needle = q.strip().casefold()
    if not needle or limit <= 0:
        return []
    wanted = {kind.casefold() for kind in kinds} or {"component", "net"}
    hits: list[dict[str, Any]] = []
    if "component" in wanted:
        hits.extend(_component_hits(index, needle))
    if "net" in wanted:
        hits.extend(_net_hits(index, needle))
    return _ordered(hits)[:limit]


def _component_hits(index: dict[str, Any], needle: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for component in _components(index):
        reference = str(component.get("reference") or "")
        folded = reference.casefold()
        matched: str | None = None
        field: str | None = None
        rank = 3
        if folded == needle:
            matched, rank = "reference", 0
        elif folded.startswith(needle):
            matched, rank = "reference", 1
        elif needle in str(component.get("value") or "").casefold():
            matched, rank = "value", 2
        else:
            for name, value in (component.get("fields") or {}).items():
                if needle in str(value or "").casefold():
                    matched, field = "field", str(name)
                    break
        if matched is None:
            continue
        hits.append(
            {
                "kind": "component",
                "reference": reference,
                "name": None,
                "value": str(component.get("value") or ""),
                "footprint": str(component.get("footprint") or ""),
                "matched": matched,
                "field": field,
                "_rank": rank,
            }
        )
    return hits


def _net_hits(index: dict[str, Any], needle: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for net in _nets(index):
        name = str(net.get("name") or "")
        folded = name.casefold()
        if needle not in folded:
            continue
        hits.append(
            {
                "kind": "net",
                "reference": None,
                "name": name,
                "value": None,
                "footprint": None,
                "matched": "name",
                "field": None,
                "_rank": 0 if folded == needle else 1 if folded.startswith(needle) else 2,
            }
        )
    return hits


# ---------------------------------------------------------------------------
# One component
# ---------------------------------------------------------------------------


def component(index: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """One component by reference with its fields and pins, or ``None``."""

    found = _component(index, ref)
    if found is None:
        return None
    fields = {str(name): str(value or "") for name, value in (found.get("fields") or {}).items()}
    flags = _assembly_flags(index, found)
    dnp = flags.get("dnp")
    if dnp is None:
        dnp = _truthy(fields.get("DNP"))
    in_bom: bool | None = None
    if "excludeFromBom" in flags:
        in_bom = not flags["excludeFromBom"]
    elif "In BOM" in fields:
        in_bom = _truthy(fields["In BOM"])
    sheet_ref = next(iter(found.get("schematicRefs") or []), {}) or {}
    return {
        "reference": str(found.get("reference") or ""),
        "componentUid": found.get("componentUid"),
        "value": str(found.get("value") or ""),
        "footprint": str(found.get("footprint") or ""),
        "libraryRef": found.get("libraryRef"),
        "sheet": sheet_ref.get("sheetInstancePath"),
        "page": sheet_ref.get("page"),
        "dnp": bool(dnp),
        "inBom": in_bom,
        "assembly": {name: flags[name] for name in _ASSEMBLY_FLAGS if name in flags},
        "fields": fields,
        "pins": _component_pins(index, found),
    }


def _component(index: dict[str, Any], ref: str) -> dict[str, Any] | None:
    components = _components(index)
    by_reference = (index.get("indexes") or {}).get("componentByReference") or {}
    position = by_reference.get(ref)
    if isinstance(position, int) and 0 <= position < len(components):
        return components[position]
    folded = ref.casefold()
    for candidate in components:
        if str(candidate.get("reference") or "").casefold() == folded:
            return candidate
    return None


def _component_pins(index: dict[str, Any], component: dict[str, Any]) -> list[dict[str, Any]]:
    uid = component.get("componentUid")
    reference = str(component.get("reference") or "")
    folded = reference.casefold()
    pins: list[dict[str, Any]] = []
    for terminal in _terminals(index):
        same_component = bool(uid) and terminal.get("componentUid") == uid
        if not same_component and folded:
            same_component = str(terminal.get("reference") or "").casefold() == folded
        if not same_component:
            continue
        pins.append(
            {
                "pin": str(terminal.get("pin") or ""),
                "pinName": str(terminal.get("pinName") or ""),
                "net": terminal.get("netName") or terminal.get("schematicNetName"),
                "netUid": terminal.get("netUid"),
            }
        )
    pins.sort(key=lambda pin: (natural_key(pin["pin"]), pin["pinName"]))
    return pins


def _assembly_flags(index: dict[str, Any], component: dict[str, Any]) -> dict[str, bool]:
    """The default assembly's true flags for ``component`` (empty when none)."""

    default = ((index.get("assembly") or {}).get("default") or {})
    entries = default.get("components") or {}
    entry = None
    uid = component.get("componentUid")
    if uid:
        entry = entries.get(uid)
    if entry is None:
        entry = entries.get(component.get("reference"))
    if not isinstance(entry, dict):
        return {}
    return {name: bool(entry[name]) for name in _ASSEMBLY_FLAGS if name in entry}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"yes", "true", "1"}


# ---------------------------------------------------------------------------
# One net
# ---------------------------------------------------------------------------


def net(index: dict[str, Any], name: str) -> dict[str, Any] | None:
    """One net by name (or an absorbed alias) with its pins, or ``None``."""

    found = _net(index, name)
    if found is None:
        return None
    net_uid = found.get("netUid")
    net_name = str(found.get("name") or "")
    names = {net_name, *(str(alias) for alias in (found.get("aliases") or []))}
    pins: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for terminal in _terminals(index):
        same_net = bool(net_uid) and terminal.get("netUid") == net_uid
        if not same_net:
            same_net = (
                terminal.get("netName") in names or terminal.get("schematicNetName") in names
            )
        if not same_net:
            continue
        reference = str(terminal.get("reference") or "")
        pin = str(terminal.get("pin") or "")
        if (reference, pin) in seen:
            continue
        seen.add((reference, pin))
        pins.append(
            {
                "reference": reference,
                "pin": pin,
                "pinName": str(terminal.get("pinName") or ""),
            }
        )
    pins.sort(key=lambda pin: (natural_key(pin["reference"]), natural_key(pin["pin"])))
    references = sorted({pin["reference"] for pin in pins}, key=natural_key)
    return {
        "name": net_name,
        "netUid": net_uid,
        "netClass": str(found.get("netClass") or ""),
        "aliases": [str(alias) for alias in (found.get("aliases") or [])],
        "components": references,
        "pins": pins,
    }


def _net(index: dict[str, Any], name: str) -> dict[str, Any] | None:
    nets = _nets(index)
    by_name = (index.get("indexes") or {}).get("netByName") or {}
    position = by_name.get(name)
    if isinstance(position, int) and 0 <= position < len(nets):
        return nets[position]
    for candidate in nets:
        if candidate.get("name") == name or name in (candidate.get("aliases") or []):
            return candidate
    folded = name.casefold()
    for candidate in nets:
        if str(candidate.get("name") or "").casefold() == folded:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Member access
# ---------------------------------------------------------------------------


def _components(index: dict[str, Any]) -> list[dict[str, Any]]:
    return index.get("components") or []


def _nets(index: dict[str, Any]) -> list[dict[str, Any]]:
    return index.get("nets") or []


def _terminals(index: dict[str, Any]) -> list[dict[str, Any]]:
    return index.get("terminals") or []
