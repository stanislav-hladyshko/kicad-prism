"""Connector runtime: the one place a connector row becomes something that talks to its forge.

Load a connector, decrypt its credential envelope, derive its single status
(``ready / linking-only / needs-test / paused / revoked``), and build the issue
and comment adapters its provider kit supplies. Outbound executors, poll,
sweep, recovery and inbound hint application all go through the installed
``ConnectorRuntime``; ``composition`` wires it and tests install one with a fake
forge and their own database connection. Credentials never leave this module
except inside the adapters; nothing here logs tokens or PEMs.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, ContextManager, Iterator, Mapping, Optional

from app.core.config import Settings, settings as default_settings
from app.services import comment_live_events
from app.services.trackers.contracts import CommentRead, Destination, IssueRead
from app.services.trackers.errors import ProviderError
from app.services.trackers.github_updates import destination_for, list_destination_updates
from app.services.trackers.inbound import InboundFetcher
from app.services.trackers.providers import comment_adapter_for, host_kit, is_issue_provider, issue_adapter_for
from app.services.trackers.secrets import decrypt_secret

# Part of every stored envelope's authenticated data, for every provider.
# Renaming it would make existing credentials undecryptable.
CREDENTIAL_FIELD = "github_app"

# --- Status -------------------------------------------------------------------

READY = "ready"
LINKING_ONLY = "linking-only"
NEEDS_TEST = "needs-test"
PAUSED = "paused"
REVOKED = "revoked"
STATUSES = (READY, LINKING_ONLY, NEEDS_TEST, PAUSED, REVOKED)

# Credentials are gone (admin revoke) or the forge rejected them (``auth_lost``).
LOST_CREDENTIAL_REASONS = frozenset({"revoked", "auth_lost"})
# Pause reasons that only a successful connection test clears.
WRITE_BLOCKING_REASONS = frozenset({"revoked", "permissions", "test_failed", "auth_lost"})


def connector_status(row: Mapping[str, Any]) -> str:
    """The connector's one status; the API returns it and the UI only labels it.

    - ``revoked``: credentials were revoked or the forge rejected them, or an
      issue host that cannot exist without credentials (GitHub's App) has none.
    - ``linking-only``: a host that only links accounts, or one with no
      publishing credentials yet.
    - ``ready``: credentials tested, bot identity known, not paused.
    - ``paused``: paused by an admin, a permission probe or unknown visibility.
    - ``needs-test``: anything else that needs a passing connection test.
    """

    provider = str(row.get("provider") or "")
    reason = str(row.get("paused_reason") or "")
    if reason in LOST_CREDENTIAL_REASONS:
        return REVOKED
    if not is_issue_provider(provider):
        return LINKING_ONLY
    if not (row.get("credentialConfigured") or row.get("credential_envelope")):
        return LINKING_ONLY if host_kit(provider).links_without_credentials else REVOKED
    paused = bool(row.get("paused"))
    if not paused and row.get("bot_forge_user_id") and reason not in WRITE_BLOCKING_REASONS:
        return READY
    if paused and reason != "test_failed":
        return PAUSED
    return NEEDS_TEST


def can_write(row: Mapping[str, Any]) -> bool:
    return connector_status(row) == READY


# --- Credentials ----------------------------------------------------------------


def credential_context(connector_id: str) -> dict[str, str]:
    """Authenticated data binding an envelope to its connector."""

    return {"record": connector_id, "field": CREDENTIAL_FIELD}


def decrypt_material(connector_id: str, envelope: str, *, settings: Settings | None = None) -> dict[str, Any]:
    return json.loads(
        decrypt_secret(envelope, credential_context(connector_id), settings=settings or default_settings).decode()
    )


AdapterPair = tuple[Any, Any]
AdapterFactory = Callable[[Mapping[str, Any]], AdapterPair]


def _kit_adapters(settings: Settings) -> AdapterFactory:
    def build(connector: Mapping[str, Any]) -> AdapterPair:
        envelope = connector.get("credential_envelope")
        if not envelope:
            raise ProviderError("auth_lost", "Connector has no installation credentials.")
        material = decrypt_material(str(connector["id"]), envelope, settings=settings)
        if not material:
            raise ProviderError("auth_lost", "Connector has no installation credentials.")
        issue = issue_adapter_for(connector, material)
        return issue, comment_adapter_for(connector, issue)

    return build


# --- Destinations and bundles ---------------------------------------------------


@dataclass(frozen=True)
class DestinationContext:
    connector_id: str
    remote_container_id: str
    container_path: str
    destination_generation: int
    paused: bool
    provider: str
    writes_enabled: bool


@dataclass(frozen=True)
class TrackerProviderBundle:
    """Concrete provider adapters for one connector."""

    connector_id: str
    provider: str
    issue: Any
    comment: Any
    bot_user_id: str
    bot_login: str

    def destination(self, ctx: DestinationContext) -> Destination:
        return destination_for(
            connector_id=ctx.connector_id,
            container_path=ctx.container_path,
            remote_container_id=ctx.remote_container_id,
            generation=ctx.destination_generation,
        )

    def list_updates(self, dest: Destination, since_cursor):
        return list_destination_updates(self.issue, dest, since_cursor)


class ContextInboundFetcher(InboundFetcher):
    """Inbound fetcher bound to one destination."""

    def __init__(self, bundle: TrackerProviderBundle, ctx: DestinationContext) -> None:
        self._bundle = bundle
        self._ctx = ctx
        self.provider = bundle.provider

    def fetch_issue(self, connector_id: str, container_id: str, external_id: str) -> IssueRead:
        del connector_id, container_id
        return self._bundle.issue.get_issue(self._bundle.destination(self._ctx), external_id, etag=None)

    def fetch_comment(self, connector_id: str, container_id: str, external_comment_id: str) -> CommentRead:
        del connector_id, container_id
        return self._bundle.comment.get_comment(
            self._bundle.destination(self._ctx),
            external_comment_id,
            etag=None,
        )


def _load_connector(conn: Any, connector_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM tracker_connectors WHERE id = %s",
        (connector_id,),
    ).fetchone()
    return dict(row) if row else None


def resolve_destination_context(
    conn: Any,
    *,
    connector_id: str,
    remote_container_id: str,
    workspace_schema: str | None = None,
) -> Optional[DestinationContext]:
    connector = _load_connector(conn, connector_id)
    if connector is None:
        return None
    if workspace_schema is None:
        from app.services.trackers.executor_support import workspace_schema as resolve_workspace_schema

        workspace_schema = resolve_workspace_schema(conn)
    qual = f'"{workspace_schema}".project_trackers' if workspace_schema.replace("_", "").isalnum() else "project_trackers"
    row = conn.execute(
        f"""
        SELECT container_path, destination_generation
        FROM {qual}
        WHERE connector_id = %s AND remote_container_id = %s
        ORDER BY destination_generation DESC, created_at DESC NULLS LAST, id ASC
        LIMIT 1
        """,
        (connector_id, remote_container_id),
    ).fetchone()
    return DestinationContext(
        connector_id=connector_id,
        remote_container_id=remote_container_id,
        container_path=str((row or {}).get("container_path") or ""),
        destination_generation=int((row or {}).get("destination_generation") or 1),
        paused=bool(connector.get("paused")),
        provider=str(connector.get("provider") or "github"),
        writes_enabled=can_write(connector),
    )


# --- Runtime --------------------------------------------------------------------


@contextmanager
def _database_connection() -> Iterator[Any]:
    from app.services.postgres_database import database

    with database.connection() as conn:
        conn.execute("SET search_path TO comments, workspace, public")
        yield conn


class ConnectorRuntime:
    """Connections, adapters and bundles for every tracker worker path.

    ``connect`` opens a database connection whose search path covers the
    comments and workspace schemas. ``adapters`` turns a connector row into its
    ``(issue, comment)`` adapters; the default decrypts the envelope and asks the
    provider kit. Tests pass both to run every executor against a fake forge.
    """

    def __init__(
        self,
        *,
        connect: Callable[[], ContextManager[Any]] | None = None,
        adapters: AdapterFactory | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or default_settings
        self._connect = connect or _database_connection
        self._adapters = adapters or _kit_adapters(self.settings)

    @contextmanager
    def connection(self) -> Iterator[Any]:
        with comment_live_events.queued_events() as queued:
            with self._connect() as conn:
                yield conn
        comment_live_events.record_queued_events(queued)

    def issue_adapter(self, connector: Mapping[str, Any]) -> Any:
        """The connector's issue adapter; ``auth_lost`` when it has no credentials."""

        return self._adapters(connector)[0]

    def comment_adapter(self, connector: Mapping[str, Any]) -> Any:
        return self._adapters(connector)[1]

    def bundle_for_connector(self, conn: Any, connector_id: str) -> Optional[TrackerProviderBundle]:
        """Adapters for background reads (poll, sweep, inbound, recovery).

        ``None`` when the connector is unknown, cannot publish, or its
        credentials are missing or unreadable: those paths skip, never fail.
        """

        connector = _load_connector(conn, connector_id)
        if connector is None or not is_issue_provider(str(connector.get("provider") or "")):
            return None
        try:
            issue, comment = self._adapters(connector)
        except Exception:
            return None
        return TrackerProviderBundle(
            connector_id=str(connector["id"]),
            provider=str(connector.get("provider") or ""),
            issue=issue,
            comment=comment,
            bot_user_id=str(connector.get("bot_forge_user_id") or ""),
            bot_login=str(connector.get("bot_login") or ""),
        )

    def inbound_fetcher(self, conn: Any, ctx: DestinationContext) -> Optional[InboundFetcher]:
        bundle = self.bundle_for_connector(conn, ctx.connector_id)
        if bundle is None:
            return None
        return ContextInboundFetcher(bundle, ctx)


_installed: ConnectorRuntime | None = None


def connector_runtime() -> ConnectorRuntime:
    """The installed runtime (a default one on first use)."""

    global _installed
    if _installed is None:
        _installed = ConnectorRuntime()
    return _installed


def install_connector_runtime(runtime: ConnectorRuntime | None) -> None:
    """Replace the runtime every tracker path uses; ``None`` restores the default."""

    global _installed
    _installed = runtime


__all__ = [
    "CREDENTIAL_FIELD",
    "ConnectorRuntime",
    "ContextInboundFetcher",
    "DestinationContext",
    "LOST_CREDENTIAL_REASONS",
    "STATUSES",
    "TrackerProviderBundle",
    "WRITE_BLOCKING_REASONS",
    "can_write",
    "connector_runtime",
    "connector_status",
    "credential_context",
    "decrypt_material",
    "install_connector_runtime",
    "resolve_destination_context",
]
