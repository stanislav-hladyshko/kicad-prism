"""pytest configuration for the backend suite (``unittest`` tests, run by pytest).

Parallel runs (``pytest -n auto``, pytest-xdist) give each worker its own fresh
copy of the three test databases, so workers never share rows or schemas:
``PRISM_DATABASE_URL``, ``TEST_POSTGRES_URL`` and ``LEGACY_SURVIVOR_TEST_POSTGRES_URL``
each get the worker id appended to their database name (``…/ci_main_gw0``).
This runs at import, before any test module reads the variables.
"""

from __future__ import annotations

import os

# A route whose response breaks its declared model fails the test instead of
# being served unvalidated (app.core.errors._response_validation_handler).
os.environ.setdefault("PRISM_STRICT_RESPONSE_MODELS", "1")

import pytest
from urllib.parse import urlsplit, urlunsplit

DATABASE_VARIABLES = ("PRISM_DATABASE_URL", "TEST_POSTGRES_URL", "LEGACY_SURVIVOR_TEST_POSTGRES_URL")


def _worker_databases(worker: str) -> None:
    import psycopg

    for variable in DATABASE_VARIABLES:
        url = os.environ.get(variable)
        if not url or not url.startswith("postgres"):
            continue
        parts = urlsplit(url)
        name = f"{parts.path.lstrip('/')}_{worker}"
        server = urlunsplit(parts._replace(path="/postgres"))
        with psycopg.connect(server, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            conn.execute(f'CREATE DATABASE "{name}"')
        os.environ[variable] = urlunsplit(parts._replace(path=f"/{name}"))


_WORKER = os.environ.get("PYTEST_XDIST_WORKER")
if _WORKER:
    _worker_databases(_WORKER)


# ---------------------------------------------------------------------------
# CI shards (``--shard k/n``): whole test modules, balanced by their recorded duration.

DURATIONS_FILE = os.path.join(os.path.dirname(__file__), ".test_durations.json")


def pytest_addoption(parser):
    parser.addoption("--shard", default=None, metavar="K/N",
                     help="run only shard K of N: whole modules, balanced by tests/.test_durations.json")


def shard_plan(modules, durations, count):
    """Module -> shard index. Longest first onto the lightest shard; a module with no recorded
    duration counts as the median. Deterministic: the same inputs always give the same plan."""
    known = sorted(durations.values())
    default = known[len(known) // 2] if known else 1.0
    loads = [0.0] * count
    plan = {}
    for module in sorted(modules, key=lambda name: (-durations.get(name, default), name)):
        target = min(range(count), key=lambda index: (loads[index], index))
        plan[module] = target
        loads[target] += durations.get(module, default)
    return plan


def pytest_collection_modifyitems(config, items):
    spec = config.getoption("--shard")
    if not spec:
        return
    import json
    from pathlib import Path

    index, count = (int(part) for part in spec.split("/"))
    if not 1 <= index <= count:
        raise pytest.UsageError(f"--shard {spec}: K must be between 1 and N")
    try:
        with open(DURATIONS_FILE) as handle:
            durations = json.load(handle)
    except FileNotFoundError:
        durations = {}
    module_of = lambda item: Path(item.nodeid.split("::")[0]).stem  # noqa: E731
    plan = shard_plan({module_of(item) for item in items}, durations, count)
    keep = [item for item in items if plan[module_of(item)] == index - 1]
    config.hook.pytest_deselected(items=[item for item in items if plan[module_of(item)] != index - 1])
    items[:] = keep


# ---------------------------------------------------------------------------
# The worker's application database, migrated once like the backend's startup.
# ``_worker_databases`` copies the three test databases at import, so a fresh
# worker's ``PRISM_DATABASE_URL`` has no ``workspace`` schema. The comments and
# tracker tests read workspace tables through the application connection (and
# the design-bundle commit index reads them in ``comment_anchor_service``), so
# bring that database to head before any test runs.


@pytest.fixture(scope="session", autouse=True)
def _application_schema_at_head():
    # A run without databases (focused unit tests) has nothing to migrate.
    if not os.environ.get("PRISM_DATABASE_URL"):
        return
    from app.services.schema_head import bring_every_schema_to_head

    bring_every_schema_to_head()

