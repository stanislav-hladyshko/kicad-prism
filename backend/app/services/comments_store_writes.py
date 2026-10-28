"""Thread writes: authorize, mutate, record the revision and stream, in one transaction.

Every root and reply mutation the API and the tracker routes make goes
through these methods. Given ``actor`` (a resolved ``ActorIdentity``), a
method checks ``comment_permissions`` against the live row inside its own
transaction, records the change under that actor, and returns the thread
with the actor's ``permissions`` attached (``comment_capabilities``). Without
``actor`` the caller is trusted (fixtures, imports, tests) and passes the
recorded identity itself.

Refusals keep the order the routes always had: identity, then existence
(``None``), then permission, then anchor validation, then revision conflict.
Anchor resolution (Git) is a callback run after the permission check, so a
refused caller never reaches it.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from app.services import comment_attachments, comment_live_events, comment_permissions, comment_social, comment_write, comments_revisions
from app.services.comment_capabilities import (
    authored_row,
    editor_for,
    promotion_actor_for,
    reply_capabilities,
    with_capabilities,
)
from app.services.comment_permissions import ActorIdentity, CommentAction, CommentPermissionError
from app.services.comments_revisions import Editor, RevisionConflict
from app.services.comments_store_codec import (
    ANCHOR_STATE_PINNED,
    ANCHOR_STATE_UNPINNED,
    AUTHOR_KIND_LEGACY,
    AUTHOR_KIND_USER,
    DEFAULT_COMMENT_SEVERITY,
    _COMMENT_COLUMNS,
    _SYSTEM_EDITOR,
    _anchor_already_pinned,
    _mentions_from_content,
    _normalize_comment_class,
    _normalize_mentions,
    _normalize_severity,
    _optional_str,
    _parse_area_bounds,
    _row_to_reply_dict,
    _utc_now_iso,
)
from app.services.comments_store_retry import _retry_on_deadlock
from app.services.trackers.promotion import (
    PromotionActor,
    _live_thread,
    after_root_severity_change,
    evaluate_dispatch,
    load_policy_row,
    manual_promote_root,
    maybe_auto_promote_root,
)
from app.services.trackers.publication_policy import DispatchPause, PublicationDenied
from app.services.trackers.reply_mutations import after_reply_added, after_reply_deleted, after_reply_edited
from app.services.trackers.reply_mutations import share_reply as enqueue_share_reply
from app.services.trackers.state_mutations import enqueue_set_state
from app.services.trackers.thread_mutations import after_root_content_edited, after_root_deleted

if TYPE_CHECKING:
    from app.services.comment_anchor_service import ResolvedAnchor

AnchorResolver = Callable[[str], "ResolvedAnchor"]
"""Resolve an anchor for a root's ``context`` (``PCB``/``SCH``); raises ``AnchorValidationError``."""


def _decorated(comment: Optional[Dict], actor: Optional[ActorIdentity]) -> Optional[Dict]:
    return with_capabilities(comment, actor) if comment is not None and actor is not None else comment


class CommentsStoreWritesMixin:
    """Root and reply writes of ``CommentsStoreService``."""

    @contextmanager
    def _write_connection(self):
        """``_connect`` for a write: appends ``comment.changed`` after the commit.

        The event log is never written inside the mutation's transaction; the
        changes ``comment_live_events`` queued are appended once it committed.
        A raise rolls the transaction back, so nothing queued is recorded.
        """

        with comment_live_events.queued_events() as queued:
            with self._connect() as conn:
                yield conn
        comment_live_events.record_queued_events(queued)

    def _root_row(self, conn, project_id: str, comment_id: str, *, canvas_only: bool = False):
        scope = " AND scope = 'canvas'" if canvas_only else ""
        return conn.execute(
            f"SELECT {_COMMENT_COLUMNS} FROM comments WHERE project_id = %s AND id = %s AND deleted_at IS NULL{scope}",
            (project_id, comment_id),
        ).fetchone()

    def _promote_min_role(self, conn, project_id: str) -> str:
        row = load_policy_row(conn, project_id, workspace_schema=self.workspace_schema)
        if row and row.get("promote_min_role"):
            return str(row["promote_min_role"])
        return "designer"

    @_retry_on_deadlock
    def create_comment(
        self,
        project_id: str,
        project_path: str,
        context: str,
        location: Dict,
        content: str,
        author: Optional[str] = None,
        content_format: str = comment_attachments.CONTENT_FORMAT_PLAIN,
        element_id: Optional[str] = None,
        element_ref: Optional[str] = None,
        element_type: Optional[str] = None,
        comment_class: Optional[str] = None,
        severity: Optional[str] = None,
        mentions: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
        scope: str = "canvas",
        base_commit: Optional[str] = None,
        compare_commit: Optional[str] = None,
        comparison_domain: Optional[str] = None,
        file_path: Optional[str] = None,
        semantic_item_id: Optional[str] = None,
        anchor_kind: Optional[str] = None,
        author_user_id: Optional[str] = None,
        author_kind: Optional[str] = None,
        anchor_commit: Optional[str] = None,
        anchor_revision_key: Optional[str] = None,
        anchor_design_key: Optional[str] = None,
        anchor_source: Optional[str] = None,
        selected_side: Optional[str] = None,
        project_relative_path: Optional[str] = None,
        promotion_actor: Optional[PromotionActor] = None,
        *,
        actor: Optional[ActorIdentity] = None,
    ) -> Dict:
        """Insert a root at revision 1 and record its creation.

        ``author`` is the display text. Ownership comes only from
        ``author_user_id``; without one the row is ``legacy`` and has no
        owner, which is how pre-identity callers keep working. The anchor
        is ``pinned`` only when the caller supplies a validated commit;
        nothing here infers one. ``actor`` supplies all three and the
        promotion actor.
        """
        self.initialize()
        if actor is not None:
            author, author_user_id, author_kind = actor.display_name, actor.actor_id, actor.actor_kind
            promotion_actor = promotion_actor_for(actor)
        author_client_id = _optional_str(actor.client_id) if actor is not None else None
        timestamp = _utc_now_iso()
        author_user_id = _optional_str(author_user_id)
        author_kind_norm = _optional_str(author_kind) or (AUTHOR_KIND_USER if author_user_id else AUTHOR_KIND_LEGACY)
        anchor_commit = _optional_str(anchor_commit)
        has_comparison_pair = bool(_optional_str(base_commit) and _optional_str(compare_commit))
        area = _parse_area_bounds(location.get("bounds"))
        class_norm = _normalize_comment_class(comment_class)
        severity_norm = _normalize_severity(severity)
        mentions_norm = _normalize_mentions(mentions) or _mentions_from_content(content)
        comment_id = comment_write.new_comment_id()
        columns = {
            "id": comment_id, "project_id": project_id, "author": author, "timestamp": timestamp,
            "status": "OPEN", "context": context.upper(),
            "location_x": float(location.get("x", 0.0)), "location_y": float(location.get("y", 0.0)),
            "location_layer": str(location.get("layer", "")), "location_page": str(location.get("page", "")),
            "content": content,
            "area_x": area[0] if area else None, "area_y": area[1] if area else None,
            "area_w": area[2] if area else None, "area_h": area[3] if area else None,
            "element_id": _optional_str(element_id), "element_ref": _optional_str(element_ref),
            "element_type": _optional_str(element_type),
            "comment_class": class_norm, "severity": severity_norm, "mentions": mentions_norm,
            "metadata": metadata if isinstance(metadata, dict) else {},
            "scope": scope, "base_commit": _optional_str(base_commit), "compare_commit": _optional_str(compare_commit),
            "comparison_domain": _optional_str(comparison_domain), "file_path": _optional_str(file_path),
            "semantic_item_id": _optional_str(semantic_item_id), "anchor_kind": _optional_str(anchor_kind),
            "author_user_id": author_user_id, "author_kind": author_kind_norm, "updated_at": timestamp,
            "author_client_id": author_client_id,
            "anchor_commit": anchor_commit, "anchor_revision_key": _optional_str(anchor_revision_key),
            "anchor_design_key": _optional_str(anchor_design_key),
            "anchor_source": _optional_str(anchor_source),
            "anchor_state": ANCHOR_STATE_PINNED if (anchor_commit or has_comparison_pair) else ANCHOR_STATE_UNPINNED,
            "selected_side": _optional_str(selected_side),
            "project_relative_path": _optional_str(project_relative_path),
            "content_format": content_format,
        }

        with self._write_connection() as conn:
            with conn.transaction():
                if actor is not None:
                    comment_permissions.authorize(CommentAction.CREATE, actor)
                self._bootstrap_project_if_needed(conn, project_id, project_path)
                comment_write.insert_root(
                    conn, columns=columns, content_format=content_format,
                    editor=Editor(
                        user_id=author_user_id, kind=author_kind_norm, display=author,
                        auth_type=actor.auth_type if actor is not None else None,
                        client_id=author_client_id,
                        token_id=_optional_str(actor.token_id) if actor is not None else None,
                    ),
                )
                comment_attachments.link_references(
                    conn, project_id=project_id, content=content,
                    linker_user_id=author_user_id, comment_id=comment_id,
                )

                created = self._get_comment_with_replies(conn, project_id, comment_id)
                if not created:
                    raise RuntimeError("Failed to fetch created comment.")

                if promotion_actor is not None:
                    outcome = maybe_auto_promote_root(
                        conn, project_id=project_id, comment=created, actor=promotion_actor,
                        workspace_schema=self.workspace_schema,
                    )
                    created = self._get_comment_with_replies(conn, project_id, comment_id)
                    if not created:
                        raise RuntimeError("Failed to fetch created comment.")
                    if outcome.action == "denied" and outcome.code:
                        created.setdefault("tracker", {}).setdefault("notPromotableReason", outcome.code)

                return _decorated(created, actor)

    def _authorize_linked_status(self, conn, project_id: str, comment_id: str, actor: PromotionActor) -> None:
        """A role below the project's promote role may not move a linked thread.

        A paused connector only means no ``set_state`` op is queued; the local
        status still changes.
        """
        thread = _live_thread(conn, comment_id)
        if thread is None or str(thread.get("external_id") or "") in ("", "pending"):
            return
        try:
            evaluate_dispatch(conn, project_id, actor.role, workspace_schema=self.workspace_schema)
        except DispatchPause:
            pass

    def _enqueue_status(self, conn, project_id: str, comment_id: str, actor: PromotionActor) -> Optional[Dict]:
        updated = self._get_comment_with_replies(conn, project_id, comment_id)
        if updated is not None:
            enqueue_set_state(
                conn, project_id=project_id, comment_id=comment_id, actor=actor,
                local_revision=int(updated.get("revision") or 1),
                workspace_schema=self.workspace_schema,
            )
            updated = self._get_comment_with_replies(conn, project_id, comment_id)
        return updated

    def _after_root_edit(
        self, conn, project_id: str, comment_id: str, actor: PromotionActor,
        *, previous_severity: str, severity_changed: bool, content_changed: bool,
    ) -> None:
        updated = self._get_comment_with_replies(conn, project_id, comment_id)
        if updated is None:
            return
        if severity_changed:
            after_root_severity_change(
                conn, project_id=project_id, comment=updated, previous_severity=previous_severity,
                actor=actor, workspace_schema=self.workspace_schema,
            )
            updated = self._get_comment_with_replies(conn, project_id, comment_id)
        if content_changed and updated is not None:
            after_root_content_edited(
                conn, project_id=project_id, comment=updated, actor=actor,
                workspace_schema=self.workspace_schema,
            )

    @_retry_on_deadlock
    def update_comment_status(
        self,
        project_id: str,
        project_path: str,
        comment_id: str,
        status: str,
        editor: Optional[Editor] = None,
        expected_revision: Optional[int] = None,
        promotion_actor: Optional[PromotionActor] = None,
    ) -> Optional[Dict]:
        """Resolve or reopen; raises RevisionConflict when the thread moved on."""
        self.initialize()

        with self._write_connection() as conn:
            with conn.transaction():
                self._bootstrap_project_if_needed(conn, project_id, project_path)
                if not self._live_root_exists(conn, project_id, comment_id):
                    return None
                if promotion_actor is not None:
                    self._authorize_linked_status(conn, project_id, comment_id, promotion_actor)
                comments_revisions.set_root_status(
                    conn, project_id=project_id, comment_id=comment_id, status=status,
                    editor=editor or _SYSTEM_EDITOR, expected_revision=expected_revision,
                )
                if promotion_actor is not None:
                    return self._enqueue_status(conn, project_id, comment_id, promotion_actor)
                return self._get_comment_with_replies(conn, project_id, comment_id)

    def edit_comment(
        self,
        project_id: str,
        project_path: str,
        comment_id: str,
        editor: Editor,
        expected_revision: Optional[int],
        content: Optional[str] = None,
        severity: Optional[str] = None,
        comment_class: Optional[str] = None,
        mentions: Optional[List[str]] = None,
        promotion_actor: Optional[PromotionActor] = None,
        content_format: Optional[str] = None,
    ) -> Optional[Dict]:
        """Edit a root's prose/severity/class/mentions as one revision.

        Anchor and location are immutable and deliberately not accepted here.
        """
        if all(value is None for value in (content, severity, comment_class, mentions)):
            raise ValueError("edit_root needs at least one field")
        return self.patch_comment(
            project_id, project_path, comment_id, editor, expected_revision=expected_revision,
            content=content, severity=severity, comment_class=comment_class, mentions=mentions,
            promotion_actor=promotion_actor, content_format=content_format,
        )

    @_retry_on_deadlock
    def patch_comment(
        self,
        project_id: str,
        project_path: str,
        comment_id: str,
        editor: Optional[Editor] = None,
        *,
        expected_revision: Optional[int] = None,
        content: Optional[str] = None,
        severity: Optional[str] = None,
        comment_class: Optional[str] = None,
        mentions: Optional[List[str]] = None,
        status: Optional[str] = None,
        promotion_actor: Optional[PromotionActor] = None,
        content_format: Optional[str] = None,
        actor: Optional[ActorIdentity] = None,
    ) -> Optional[Dict]:
        """Apply an HTTP patch atomically, even when it edits prose and status.

        With ``promotion_actor`` a linked thread queues the matching forge
        update (issue body, severity label, open/closed) in the same
        transaction; without it the patch stays Prism-local. ``actor`` must
        own the root (or be an admin) to edit it and hold status authority to
        resolve or reopen.
        """
        self.initialize()
        if actor is not None:
            editor, promotion_actor = editor_for(actor), promotion_actor_for(actor)
        has_edit = any(value is not None for value in (content, severity, comment_class, mentions))
        with self._write_connection() as conn:
            with conn.transaction():
                self._bootstrap_project_if_needed(conn, project_id, project_path)
                row = self._root_row(conn, project_id, comment_id)
                if row is None:
                    return None
                if actor is not None:
                    if has_edit:
                        comment_permissions.authorize(CommentAction.EDIT, actor, target=authored_row(row))
                    if status is not None:
                        comment_permissions.authorize(CommentAction.RESOLVE, actor)
                if status is not None and promotion_actor is not None:
                    self._authorize_linked_status(conn, project_id, comment_id, promotion_actor)
                previous_severity = str(row.get("severity") or DEFAULT_COMMENT_SEVERITY)
                if has_edit:
                    expected_revision = comments_revisions.edit_root(
                        conn, project_id=project_id, comment_id=comment_id, editor=editor,
                        expected_revision=expected_revision, content=content,
                        severity=_normalize_severity(severity) if severity is not None else None,
                        comment_class=_normalize_comment_class(comment_class) if comment_class is not None else None,
                        mentions=_normalize_mentions(mentions) if mentions is not None else None,
                        content_format=content_format,
                    )
                    if content is not None:
                        comment_attachments.link_references(
                            conn, project_id=project_id, content=content,
                            linker_user_id=editor.user_id, comment_id=comment_id,
                        )
                    if promotion_actor is not None:
                        self._after_root_edit(
                            conn, project_id, comment_id, promotion_actor,
                            previous_severity=previous_severity,
                            severity_changed=severity is not None, content_changed=content is not None,
                        )
                if status is not None:
                    comments_revisions.set_root_status(
                        conn, project_id=project_id, comment_id=comment_id, status=status,
                        editor=editor, expected_revision=expected_revision,
                    )
                    if promotion_actor is not None:
                        return _decorated(self._enqueue_status(conn, project_id, comment_id, promotion_actor), actor)
                return _decorated(self._get_comment_with_replies(conn, project_id, comment_id), actor)

    @_retry_on_deadlock
    def pin_comment_anchor(
        self,
        project_id: str,
        project_path: str,
        comment_id: str,
        editor: Optional[Editor] = None,
        *,
        commit: str,
        source_revision_key: Optional[str] = None,
        design_key: Optional[str] = None,
        file_path: Optional[str] = None,
        project_relative_path: Optional[str] = None,
        expected_revision: Optional[int] = None,
        actor: Optional[ActorIdentity] = None,
        resolve: Optional[AnchorResolver] = None,
    ) -> Optional[Dict]:
        """Admin pin of an unpinned root. Already-pinned rows stay immutable.

        ``resolve`` turns ``commit`` into the anchor for the root's context.
        """
        if actor is not None:
            if not actor.is_admin:
                raise CommentPermissionError("legacy_admin_only", "Only an admin can pin a comment")
            editor = editor_for(actor)
        self.initialize()
        now = _utc_now_iso()
        with self._write_connection() as conn:
            with conn.transaction():
                self._bootstrap_project_if_needed(conn, project_id, project_path)
                row = self._root_row(conn, project_id, comment_id)
                if row is None:
                    return None
                if resolve is not None:
                    anchor = resolve(row["context"])
                    commit = anchor.commit or commit
                    source_revision_key = anchor.source_revision_key
                    design_key = anchor.design_key
                    file_path, project_relative_path = anchor.file_path, anchor.project_relative_path
                if _anchor_already_pinned(row):
                    raise comments_revisions.AnchorImmutable()
                params: List[object] = [
                    commit, source_revision_key, _optional_str(design_key), "manual", ANCHOR_STATE_PINNED,
                    _optional_str(file_path) or row.get("file_path"),
                    _optional_str(project_relative_path) or row.get("project_relative_path"),
                    now, project_id, comment_id, ANCHOR_STATE_UNPINNED,
                ]
                guard = " AND anchor_state = %s"
                if expected_revision is not None:
                    guard += " AND revision = %s"
                    params.append(expected_revision)
                updated = conn.execute(
                    f"""
                    UPDATE comments
                    SET anchor_commit = %s, anchor_revision_key = %s, anchor_design_key = %s,
                        anchor_source = %s, anchor_state = %s, file_path = %s, project_relative_path = %s,
                        revision = revision + 1, updated_at = %s
                    WHERE project_id = %s AND id = %s AND deleted_at IS NULL{guard}
                    RETURNING revision
                    """,
                    tuple(params),
                ).fetchone()
                if updated is None:
                    latest = self._root_row(conn, project_id, comment_id)
                    if latest is None:
                        return None
                    if _anchor_already_pinned(latest):
                        raise comments_revisions.AnchorImmutable()
                    raise RevisionConflict("root", comment_id, int(latest["revision"] or 1))
                comments_revisions.record_revision(
                    conn, project_id=project_id, target_kind=comments_revisions.ROOT, target_id=comment_id,
                    revision=int(updated["revision"]), change_kind=comments_revisions.CHANGE_EDIT, editor=editor,
                    content=row["content"],
                )
                return _decorated(self._get_comment_with_replies(conn, project_id, comment_id), actor)

    def fill_anchor_design_keys(self, project_id: str, keys: Dict[str, str]) -> int:
        """Backfill the design key of already-pinned rows that predate the column.

        A design key is derived identity, not an edit: the UPDATE fills only a
        row that has a commit and no key yet, and never bumps ``revision``,
        ``updated_at`` or records a revision. Keys that are not a 64-character
        lowercase hex digest are ignored. Returns the number of rows filled.
        """
        self.initialize()
        valid = {
            comment_id: key
            for comment_id, key in keys.items()
            if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)
        }
        if not valid:
            return 0
        filled = 0
        with self._write_connection() as conn:
            with conn.transaction():
                for comment_id, key in valid.items():
                    cursor = conn.execute(
                        """
                        UPDATE comments SET anchor_design_key = %s
                        WHERE project_id = %s AND id = %s
                          AND anchor_design_key IS NULL AND anchor_commit IS NOT NULL
                        """,
                        (key, project_id, comment_id),
                    )
                    filled += cursor.rowcount
        return filled

    @_retry_on_deadlock
    def reattach_comment(
        self,
        project_id: str,
        project_path: str,
        comment_id: str,
        *,
        commit: str,
        location: Dict,
        element_id: Optional[str],
        relative_point: Optional[List[float]],
        file_path: Optional[str] = None,
        editor: Optional[Editor] = None,
        expected_revision: int,
        actor: Optional[ActorIdentity] = None,
        resolve: Optional[AnchorResolver] = None,
    ) -> Optional[Dict]:
        """Append a binding effective from ``commit``; never rewrite origin.

        Only canvas roots have bindings; ``actor`` must be able to edit the root.
        """
        self.initialize()
        if actor is not None:
            editor = editor_for(actor)
        with self._write_connection() as conn:
            with conn.transaction():
                self._bootstrap_project_if_needed(conn, project_id, project_path)
                if actor is not None or resolve is not None:
                    current = self._root_row(conn, project_id, comment_id, canvas_only=True)
                    if current is None:
                        return None
                    if actor is not None:
                        comment_permissions.authorize(CommentAction.EDIT, actor, target=authored_row(current))
                    if resolve is not None:
                        anchor = resolve(current["context"])
                        commit, file_path = anchor.commit or commit, anchor.file_path
                row = conn.execute(
                    """UPDATE comments SET revision = revision + 1, updated_at = %s
                       WHERE project_id = %s AND id = %s AND scope = 'canvas'
                         AND deleted_at IS NULL AND revision = %s
                       RETURNING revision""",
                    (_utc_now_iso(), project_id, comment_id, expected_revision),
                ).fetchone()
                if row is None:
                    current = self._root_row(conn, project_id, comment_id, canvas_only=True)
                    if current is None:
                        return None
                    raise RevisionConflict("root", comment_id, int(current["revision"]))

                conn.execute(
                    """INSERT INTO comment_anchor_bindings (
                           project_id, comment_id, effective_commit, element_id, file_path,
                           location_x, location_y, location_layer, location_page, area_bounds, relative_point,
                           editor_user_id, editor_kind
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)""",
                    (
                        project_id, comment_id, commit, _optional_str(element_id), _optional_str(file_path),
                        float(location["x"]), float(location["y"]),
                        str(location.get("layer", "")), str(location.get("page", "")),
                        _json_or_none(location.get("bounds")), _json_or_none(relative_point),
                        editor.user_id, editor.kind,
                    ),
                )
                comments_revisions.record_revision(
                    conn, project_id=project_id, target_kind=comments_revisions.ROOT,
                    target_id=comment_id, revision=int(row["revision"]),
                    change_kind=comments_revisions.CHANGE_REATTACH, editor=editor, emit_change=False,
                )
                comment_live_events.record_thread_change(
                    conn, project_id=project_id, comment_id=comment_id, change_kind="anchor",
                )
                return _decorated(self._get_comment_with_replies(conn, project_id, comment_id), actor)

    @_retry_on_deadlock
    def add_reply(
        self,
        project_id: str,
        project_path: str,
        comment_id: str,
        content: str,
        author: Optional[str] = None,
        author_user_id: Optional[str] = None,
        author_kind: Optional[str] = None,
        origin: str = comments_revisions.ORIGIN_PRISM,
        promotion_actor: Optional[PromotionActor] = None,
        content_format: str = comment_attachments.CONTENT_FORMAT_PLAIN,
        *,
        actor: Optional[ActorIdentity] = None,
    ) -> Optional[Tuple[Dict, Dict]]:
        """Append a reply under a live root: ``(thread, reply)`` or ``None``.

        A reply the actor may not publish on a linked thread is still saved,
        marked ``unsynced_local``.
        """
        self.initialize()
        if actor is not None:
            author, author_user_id, author_kind = actor.display_name, actor.actor_id, actor.actor_kind
            promotion_actor = promotion_actor_for(actor)
        author_user_id = _optional_str(author_user_id)
        author_kind_norm = _optional_str(author_kind) or (AUTHOR_KIND_USER if author_user_id else AUTHOR_KIND_LEGACY)
        author_client_id = _optional_str(actor.client_id) if actor is not None else None

        with self._write_connection() as conn:
            with conn.transaction():
                if actor is not None:
                    comment_permissions.authorize(CommentAction.REPLY, actor)
                self._bootstrap_project_if_needed(conn, project_id, project_path)

                if not self._lock_live_root(conn, project_id, comment_id):
                    return None

                reply_id = comment_write.insert_reply(
                    conn, project_id=project_id, comment_id=comment_id, content=content,
                    editor=Editor(
                        user_id=author_user_id, kind=author_kind_norm, display=author, origin=origin,
                        auth_type=actor.auth_type if actor is not None else None,
                        client_id=author_client_id,
                        token_id=_optional_str(actor.token_id) if actor is not None else None,
                    ),
                    content_format=content_format, require_live_root=True,
                )
                if reply_id is None:
                    return None
                comment_attachments.link_references(
                    conn, project_id=project_id, content=content,
                    linker_user_id=author_user_id, comment_id=comment_id, reply_id=reply_id,
                )

                unsynced_reply_ids: set[str] = set()
                if promotion_actor is not None:
                    parent = self._get_comment_with_replies(conn, project_id, comment_id)
                    created_reply = self._live_reply(conn, project_id, comment_id, reply_id)
                    if parent and created_reply:
                        outcome = after_reply_added(
                            conn, project_id=project_id, comment=parent,
                            reply=_row_to_reply_dict(created_reply), actor=promotion_actor,
                            workspace_schema=self.workspace_schema,
                        )
                        if outcome.action == "unsynced":
                            unsynced_reply_ids.add(reply_id)
                            # The reply's create revision streamed this thread.
                            comment_write.set_reply_sync_state(
                                conn, project_id=project_id, reply_id=reply_id,
                                state="unsynced_local", streamed_by_caller=True,
                            )

                updated_comment = self._get_comment_with_replies(
                    conn, project_id, comment_id, unsynced_reply_ids=unsynced_reply_ids or None,
                )
                if not updated_comment:
                    return None

                created = self._live_reply(conn, project_id, comment_id, reply_id)
                reply_payload = _row_to_reply_dict(created)
                comment_attachments.decorate(conn, project_id, [{"replies": [reply_payload]}])
                if reply_id in unsynced_reply_ids:
                    reply_payload["sync"] = {"state": "unsynced_local", "reason": "publication_required"}
                if actor is not None:
                    updated_comment = with_capabilities(updated_comment, actor)
                    reply_payload["permissions"] = reply_capabilities(reply_payload, actor)
                return (updated_comment, reply_payload)

    def _lock_live_root(self, conn, project_id: str, comment_id: str) -> bool:
        """Lock a live root so a concurrent tombstone cannot race a reply insert."""
        return bool(conn.execute(
            """
            SELECT 1 FROM comments
            WHERE project_id = %s AND id = %s AND deleted_at IS NULL
            FOR UPDATE
            """,
            (project_id, comment_id),
        ).fetchone())

    @_retry_on_deadlock
    def edit_reply(
        self,
        project_id: str,
        project_path: str,
        comment_id: str,
        reply_id: str,
        content: str,
        editor: Optional[Editor] = None,
        expected_revision: Optional[int] = None,
        promotion_actor: Optional[PromotionActor] = None,
        content_format: Optional[str] = None,
        *,
        actor: Optional[ActorIdentity] = None,
    ) -> Optional[Dict]:
        self.initialize()
        if actor is not None:
            editor, promotion_actor = editor_for(actor), promotion_actor_for(actor)
        with self._write_connection() as conn:
            with conn.transaction():
                self._bootstrap_project_if_needed(conn, project_id, project_path)
                live = self._live_reply(conn, project_id, comment_id, reply_id)
                if not live:
                    return None
                if actor is not None:
                    comment_permissions.authorize(CommentAction.EDIT, actor, target=authored_row(live))
                comments_revisions.edit_reply(
                    conn, project_id=project_id, reply_id=reply_id, content=content,
                    editor=editor, expected_revision=expected_revision, content_format=content_format,
                )
                comment_attachments.link_references(
                    conn, project_id=project_id, content=content,
                    linker_user_id=editor.user_id, comment_id=comment_id, reply_id=reply_id,
                )
                updated = self._get_comment_with_replies(conn, project_id, comment_id)
                if updated is not None and promotion_actor is not None:
                    edited = self._live_reply(conn, project_id, comment_id, reply_id)
                    if edited:
                        after_reply_edited(
                            conn, project_id=project_id, comment=updated,
                            reply=_row_to_reply_dict(edited), actor=promotion_actor,
                            workspace_schema=self.workspace_schema,
                        )
                        updated = self._get_comment_with_replies(conn, project_id, comment_id)
                return _decorated(updated, actor)

    @_retry_on_deadlock
    def delete_reply(
        self,
        project_id: str,
        project_path: str,
        comment_id: str,
        reply_id: str,
        editor: Optional[Editor] = None,
        expected_revision: Optional[int] = None,
        promotion_actor: Optional[PromotionActor] = None,
        *,
        actor: Optional[ActorIdentity] = None,
    ) -> Optional[Dict]:
        """Tombstone one reply; history and the row itself are kept."""
        self.initialize()
        if actor is not None:
            editor, promotion_actor = editor_for(actor), promotion_actor_for(actor)
        with self._write_connection() as conn:
            with conn.transaction():
                self._bootstrap_project_if_needed(conn, project_id, project_path)
                live_reply = self._live_reply(conn, project_id, comment_id, reply_id)
                if not live_reply:
                    return None
                if actor is not None:
                    comment_permissions.authorize(CommentAction.DELETE, actor, target=authored_row(live_reply))
                reply_snapshot = _row_to_reply_dict(live_reply)
                comments_revisions.tombstone_reply(
                    conn, project_id=project_id, reply_id=reply_id, editor=editor, expected_revision=expected_revision,
                )
                updated = self._get_comment_with_replies(conn, project_id, comment_id)
                if updated is not None and promotion_actor is not None:
                    after_reply_deleted(
                        conn, project_id=project_id, comment=updated, reply=reply_snapshot,
                        actor=promotion_actor, workspace_schema=self.workspace_schema,
                    )
                    updated = self._get_comment_with_replies(conn, project_id, comment_id)
                return _decorated(updated, actor)

    @_retry_on_deadlock
    def delete_comment(
        self,
        project_id: str,
        project_path: str,
        comment_id: str,
        editor: Optional[Editor] = None,
        expected_revision: Optional[int] = None,
        promotion_actor: Optional[PromotionActor] = None,
        *,
        actor: Optional[ActorIdentity] = None,
    ) -> bool:
        """Tombstone a root and its live replies.

        The rows stay for history and for the tracker's unlink lineage;
        every read path filters ``deleted_at``. ``delete_project_comments``
        remains the one hard delete, used when the project itself goes.
        """
        self.initialize()
        if actor is not None:
            editor, promotion_actor = editor_for(actor), promotion_actor_for(actor)

        with self._write_connection() as conn:
            with conn.transaction():
                self._bootstrap_project_if_needed(conn, project_id, project_path)
                row = self._root_row(conn, project_id, comment_id)
                if row is None:
                    return False
                if actor is not None:
                    comment_permissions.authorize(CommentAction.DELETE, actor, target=authored_row(row))
                comments_revisions.tombstone_root(
                    conn, project_id=project_id, comment_id=comment_id,
                    editor=editor or _SYSTEM_EDITOR, expected_revision=expected_revision,
                )
                if promotion_actor is not None:
                    after_root_deleted(
                        conn, project_id=project_id, comment_id=comment_id, actor=promotion_actor,
                        workspace_schema=self.workspace_schema,
                    )
                return True

    @_retry_on_deadlock
    def set_reaction(
        self, project_id: str, project_path: str, comment_id: str, reply_id: Optional[str], *,
        reaction: str, present: bool, user_id: Optional[str] = None, user_display: Optional[str] = None,
        actor: Optional[ActorIdentity] = None,
    ) -> Optional[Dict]:
        """Toggle one reader's reaction on a root or reply; None when it is gone."""
        self.initialize()
        if actor is not None:
            user_id, user_display = actor.actor_id, actor.display_name
        with self._write_connection() as conn:
            with conn.transaction():
                if actor is not None:
                    comment_permissions.authorize(CommentAction.REPLY, actor)
                self._bootstrap_project_if_needed(conn, project_id, project_path)
                if not self._live_root_exists(conn, project_id, comment_id):
                    return None
                if reply_id is not None and self._live_reply(conn, project_id, comment_id, reply_id) is None:
                    return None
                changed = comment_social.set_reaction(
                    conn, project_id=project_id, comment_id=comment_id, target_id=reply_id or comment_id,
                    user_id=user_id, user_display=user_display, reaction=reaction, present=present,
                )
                if changed:
                    comment_live_events.record_thread_change(
                        conn, project_id=project_id, comment_id=comment_id, change_kind="upsert",
                    )
                return _decorated(self._get_comment_with_replies(conn, project_id, comment_id), actor)

    @_retry_on_deadlock
    def promote_comment(self, project_id: str, project_path: str, comment_id: str, actor: ActorIdentity) -> Optional[Dict]:
        """Queue the root's issue publication; returns the thread with the reader's capabilities."""
        self.initialize()
        with self._write_connection() as conn:
            with conn.transaction():
                comment_permissions.authorize(
                    CommentAction.PROMOTE, actor, promote_min_role=self._promote_min_role(conn, project_id),
                )
                self._bootstrap_project_if_needed(conn, project_id, project_path)
                current = self._get_comment_with_replies(conn, project_id, comment_id)
                if current is None:
                    return None
                result = manual_promote_root(
                    conn, project_id=project_id, comment=current, actor=promotion_actor_for(actor),
                    workspace_schema=self.workspace_schema,
                )
                if result.action == "denied":
                    raise PublicationDenied(result.code or "publication_required", result.reason or "Publication denied")
                # The tracked_threads/sync_ops trigger emits the durable
                # projection event in this transaction for a newly queued op.
                return _decorated(self._get_comment_with_replies(conn, project_id, comment_id), actor)

    @_retry_on_deadlock
    def share_reply(
        self, project_id: str, project_path: str, comment_id: str, reply_id: str, actor: ActorIdentity,
    ) -> Optional[Dict]:
        """Queue a Prism-local reply on a linked thread; returns the thread with the reader's capabilities."""
        self.initialize()
        with self._write_connection() as conn:
            with conn.transaction():
                if self._live_reply(conn, project_id, comment_id, reply_id) is None:
                    return None
                comment_permissions.authorize(
                    CommentAction.SHARE, actor, promote_min_role=self._promote_min_role(conn, project_id),
                )
                self._bootstrap_project_if_needed(conn, project_id, project_path)
                current = self._get_comment_with_replies(conn, project_id, comment_id)
                reply = self._live_reply(conn, project_id, comment_id, reply_id)
                if current is None or reply is None:
                    return None
                shared = enqueue_share_reply(
                    conn, project_id=project_id, comment=current,
                    reply=_row_to_reply_dict(reply), actor=promotion_actor_for(actor),
                    workspace_schema=self.workspace_schema,
                )
                # A newly queued op's sync_ops trigger already streamed this thread.
                comment_write.set_reply_sync_state(
                    conn, project_id=project_id, reply_id=reply_id, state=None,
                    streamed_by_caller=shared.action == "enqueued",
                )
                return _decorated(self._get_comment_with_replies(conn, project_id, comment_id), actor)


def _json_or_none(value) -> Optional[str]:
    return json.dumps(value) if value is not None else None
