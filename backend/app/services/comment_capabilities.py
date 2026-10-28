"""The ``permissions`` block a reader's copy of a thread carries.

``comment_permissions`` decides one action; this module applies those
decisions to a whole thread projection (root and replies) for one actor, the
same way for listings and for the thread a write returns. It also maps an
actor onto the identities a write records (``Editor``) and publishes under
(``PromotionActor``).
"""

from __future__ import annotations

from typing import Dict, Optional

from app.core.roles import normalize_role
from app.services import comment_permissions, comment_social
from app.services.comment_permissions import ActorIdentity, AuthoredObject, CommentAction
from app.services.comments_revisions import Editor
from app.services.trackers.projections import can_retry_projection
from app.services.trackers.promotion import PromotionActor


def editor_for(actor: ActorIdentity) -> Editor:
    return Editor(
        user_id=actor.actor_id,
        kind=actor.actor_kind,
        display=actor.display_name,
        auth_type=actor.auth_type,
        client_id=actor.client_id or None,
        token_id=actor.token_id or None,
    )


def promotion_actor_for(actor: ActorIdentity) -> PromotionActor:
    """The actor whose role decides whether a linked thread's change reaches the forge."""
    return PromotionActor(user_id=actor.actor_id, role=actor.role, kind=actor.actor_kind)


def authored(value: Dict) -> AuthoredObject:
    """Ownership columns of a projected root or reply (camelCase keys)."""
    return AuthoredObject(
        author_user_id=value.get("authorUserId"),
        author_kind=str(value.get("authorKind") or comment_permissions.ACTOR_KIND_LEGACY),
    )


def authored_row(row: Dict) -> AuthoredObject:
    """Ownership columns of a ``comments`` / ``comment_replies`` row."""
    return AuthoredObject(
        author_user_id=row.get("author_user_id"),
        author_kind=str(row.get("author_kind") or comment_permissions.ACTOR_KIND_LEGACY),
    )


def reply_capabilities(reply: Dict, actor: ActorIdentity) -> Dict:
    capabilities = comment_permissions.capabilities(actor, authored(reply))
    capabilities["canPublish"] = False
    return capabilities


def with_capabilities(comment: Dict, actor: Optional[ActorIdentity]) -> Dict:
    """Personalize reactions and attach ``permissions`` to the root and each reply.

    Without an actor (a caller whose identity cannot be resolved) only the
    reactions are personalized, for nobody.
    """
    if actor is None:
        return comment_social.personalize(comment, None)
    comment_social.personalize(comment, actor.actor_id)
    tracker = comment.get("tracker") or {}
    promote_role = normalize_role(tracker.get("promoteMinRole")) or "designer"
    linked = bool(tracker.get("linkState"))
    capabilities = comment_permissions.capabilities(
        actor, authored(comment), linked=linked, promote_min_role=promote_role,
    )
    capabilities["canPublish"] = (
        bool(tracker.get("promoteMinRole"))
        and not linked
        and (comment.get("anchor") or {}).get("state") == "pinned"
        and comment_permissions.allowed(
            CommentAction.PROMOTE, actor, promote_min_role=promote_role,
        )
    )
    capabilities["canRetry"] = can_retry_projection(tracker) and comment_permissions.allowed(
        CommentAction.RETRY, actor, linked=linked, promote_min_role=promote_role,
    )
    comment["permissions"] = capabilities
    can_share = linked and comment_permissions.allowed(
        CommentAction.SHARE, actor, promote_min_role=promote_role,
    )
    for reply in comment.get("replies", []):
        reply_caps = reply_capabilities(reply, actor)
        reply_caps["canShare"] = can_share and (reply.get("sync") or {}).get("state") == "unsynced_local"
        reply["permissions"] = reply_caps
    return comment
