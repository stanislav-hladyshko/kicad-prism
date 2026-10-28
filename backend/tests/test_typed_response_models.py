"""Typed responses describe the JSON the handlers already returned (ADR MCP §3.2).

Each case builds the payload a route returns — from the comments store where the
real shapings matter, a literal otherwise — and runs it through the route's
declared response model with FastAPI's own serializer. The body must be
unchanged: a coercion (``int`` to ``str``, datetime to string), an alias, or a
key the model drops would show up as a diff. Payloads also carry keys the models
do not name, so ``extra="allow"`` is pinned too.
"""

from __future__ import annotations

import tempfile
import unittest

from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, Response
from fastapi.routing import APIRoute, serialize_response

from app.api import auth, comment_live, comments, design_compare, projects, settings
from app.services import comment_capabilities, comments_url_service
from app.services.comment_permissions import ActorIdentity
from support.postgres import comments_store_on, requires_scratch_postgres, scratch_schema


def _route(router, operation: str) -> APIRoute:
    for route in router.routes:
        if isinstance(route, APIRoute) and route.name == operation:
            return route
    raise AssertionError(f"{operation} is not a route on this router")


async def _body(router, operation: str, payload):
    """Serialize ``payload`` exactly as the app would, and return the JSON body."""

    route = _route(router, operation)
    if route.response_model is None:
        raise AssertionError(f"{operation} lost its response model")
    return await serialize_response(
        field=route.response_field,
        response_content=payload,
        by_alias=True,
        exclude_unset=route.response_model_exclude_unset,
        exclude_defaults=route.response_model_exclude_defaults,
        exclude_none=route.response_model_exclude_none,
    )


class _Case(unittest.IsolatedAsyncioTestCase):
    async def assertBody(self, router, operation: str, payload) -> None:  # noqa: N802 - unittest style
        body = await _body(router, operation, payload)
        self.assertEqual(body, jsonable_encoder(payload), f"{operation} changed the response body")


class AuthResponseTests(_Case):
    async def test_session_mutations_keep_their_body(self) -> None:
        await self.assertBody(auth.router, "change_own_password", {"success": True})
        await self.assertBody(auth.router, "logout", {"success": True})
        await self.assertBody(auth.router, "revoke_other_sessions", {"revoked": 3})
        await self.assertBody(
            auth.router, "revoke_user_sessions", {"email": "user@example.test", "revoked": 3}
        )


class SettingsResponseTests(_Case):
    async def test_ssh_key_and_git_access_keep_their_body(self) -> None:
        await self.assertBody(
            settings.router,
            "generate_ssh_key",
            {"success": True, "public_key": "ssh-ed25519 AAAA test", "fingerprint": "SHA256:abc"},
        )
        await self.assertBody(
            settings.router,
            "generate_ssh_key",
            {"success": True, "public_key": None, "fingerprint": None},
        )
        await self.assertBody(
            settings.router,
            "get_git_access",
            {
                "key": {
                    "exists": True,
                    "public_key": "ssh-ed25519 AAAA test",
                    "fingerprint": "SHA256:abc",
                    "key_type": "ed25519",
                    "comment": "prism-workspace",
                    "created_at": "2026-10-26T00:00:00Z",
                },
                "trusted_hosts": ["github.com"],
                "repositories": [
                    {
                        "id": "p1",
                        "name": "board",
                        "url": "git@github.com:org/board.git",
                        "last_synced_at": "2026-10-25T00:00:00Z",
                        "host": "github.com",
                        "host_trusted": True,
                        "guidance": "Add the deploy key to the repository.",
                        "forge": "github",
                        "deploy_key_url": "https://github.com/org/board/settings/keys",
                    },
                    {
                        "id": None,
                        "name": None,
                        "url": "not a url",
                        "last_synced_at": None,
                        "host": None,
                        "host_trusted": None,
                        "guidance": "This repository's URL predates URL validation.",
                    },
                ],
                "tools": {"ssh": True, "ssh_keygen": True, "ssh_keyscan": False},
            },
        )

    async def test_git_access_answers_keep_their_body(self) -> None:
        await self.assertBody(
            settings.router,
            "check_git_access",
            {
                "reachable": True,
                "authorized": False,
                "reason": "auth_failed",
                "message": "Permission denied (publickey).",
                "default_branch": None,
                "forge": "github",
                "deploy_key_url": "https://github.com/org/board/settings/keys",
                "instructions": "Add the workspace key as a deploy key.",
            },
        )
        await self.assertBody(
            settings.router,
            "scan_git_host_key",
            {"host": "github.com", "fingerprints": ["SHA256:abc"], "already_trusted": False},
        )
        await self.assertBody(
            settings.router,
            "trust_git_host_key",
            {"host": "github.com", "fingerprints": ["SHA256:abc"], "trusted": True},
        )
        await self.assertBody(
            settings.router, "forget_git_host_key", {"host": "github.com", "removed": True}
        )

    async def test_access_mutations_keep_their_body(self) -> None:
        await self.assertBody(
            settings.router,
            "delete_access_user",
            {"deleted": "user@example.test", "sessions_revoked": 2},
        )
        await self.assertBody(
            settings.router,
            "set_user_password",
            {"email": "user@example.test", "must_change": True, "sessions_revoked": 0},
        )
        await self.assertBody(
            settings.router,
            "delete_user_password",
            {"email": "user@example.test", "deleted": True},
        )


class DesignCompareResponseTests(_Case):
    async def test_small_answers_keep_their_body(self) -> None:
        await self.assertBody(
            design_compare.router,
            "append_design_compare_debug_log",
            {"status": "logged", "path": "/tmp/comparison.jsonl"},
        )
        await self.assertBody(
            design_compare.router, "start_design_compare", {"job_id": "job-1"}
        )
        await self.assertBody(
            design_compare.router, "delete_design_compare", {"status": "cancel_requested"}
        )
        await self.assertBody(
            design_compare.router, "delete_design_compare", {"status": "retained"}
        )

    async def test_the_v2_manifest_fits_the_result_model(self) -> None:
        # The route returns this one as a JSONResponse, so the body is untouched;
        # the model still has to accept it in case the pass-through changes.
        await self.assertBody(
            design_compare.router,
            "design_compare_result",
            {
                "schema": "prism.design_compare_bundle_v2",
                "resultSchema": "prism.design_compare_result_v2",
                "base": "a" * 40,
                "head": "b" * 40,
                "revisions": {"base": {"commit": "a" * 40}, "head": {"commit": "b" * 40}},
                "readiness": {"stage": "complete", "domains": {"schematic": "ready"}},
                "counts": {"schematic": {"added": 1, "removed": 0}},
                "modes": {"schematic": "semantic"},
                "sidecars": {
                    "core": {
                        "digest": "c" * 64,
                        "sizeBytes": 1234,
                        "mediaType": "application/json",
                        "url": f"/api/projects/p1/design-compare/job-1/sidecars/{'c' * 64}",
                    }
                },
            },
        )


class FileRouteTests(unittest.TestCase):
    def test_file_routes_declare_their_media_types(self) -> None:
        attachment = _route(comments.router, "get_comment_attachment")
        self.assertIs(attachment.response_class, FileResponse)
        attachment_types = set(attachment.responses[200]["content"])
        self.assertIn("image/png", attachment_types)
        self.assertIn("application/pdf", attachment_types)

        sidecar = _route(design_compare.router, "design_compare_sidecar")
        self.assertIs(sidecar.response_class, FileResponse)
        self.assertEqual(
            set(sidecar.responses[200]["content"]), {"application/json", "image/svg+xml"}
        )

        report = _route(comments.router, "get_comparison_discussion_report")
        self.assertIs(report.response_class, Response)
        self.assertEqual(set(report.responses[200]["content"]), {"application/zip"})


class ProjectsResponseTests(_Case):
    async def test_source_urls_keep_their_body(self) -> None:
        urls = comments_url_service.build_comments_source_urls("p1", "http://localhost:8000")
        await self.assertBody(
            projects.router,
            "get_project_comments_source_urls",
            {
                "project_id": "p1",
                "project_name": "Board",
                "base_url": urls["base_url"],
                "list_url": urls["absolute"]["list_url"],
                "patch_url_template": urls["absolute"]["patch_url_template"],
                "reply_url_template": urls["absolute"]["reply_url_template"],
                "delete_url_template": urls["absolute"]["delete_url_template"],
                "relative": urls["relative"],
            },
        )


class CommentChangesResponseTests(_Case):
    async def test_a_replay_page_keeps_its_body(self) -> None:
        await self.assertBody(
            comment_live.router,
            "get_comment_changes",
            {
                "cursor": 12,
                "hasMore": False,
                "changes": [
                    {
                        "cursor": 11,
                        "commentId": "c1",
                        "scope": "comparison",
                        "baseCommit": "a" * 40,
                        "compareCommit": "b" * 40,
                        "changeKind": "upsert",
                        "createdAt": "2026-10-26T00:00:00Z",
                    },
                    {
                        "cursor": 12,
                        "commentId": "c2",
                        "scope": "canvas",
                        "baseCommit": None,
                        "compareCommit": None,
                        "changeKind": "delete",
                        "createdAt": "2026-10-26T00:00:01Z",
                    },
                ],
            },
        )


ACTOR = ActorIdentity(
    actor_id="u_a",
    actor_kind="user",
    display_name="Author",
    role="designer",
    auth_type="session",
)


@requires_scratch_postgres("the comment response shapes")
class CommentResponseTests(_Case):
    def setUp(self) -> None:
        self.scratch = scratch_schema(self, "typed_comment_models", create=False)
        self.store = comments_store_on(self.scratch)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.project_id = "typed_models"
        self.path = self.tempdir.name

    def _root(self, **kwargs) -> dict:
        return self.store.create_comment(
            self.project_id,
            self.path,
            "PCB",
            {"x": 1.5, "y": 2.5, "layer": "F.Cu", "page": "board.kicad_pcb"},
            "Look at this",
            actor=ACTOR,
            content_format="md",
            **kwargs,
        )

    async def test_a_listing_and_a_thread_keep_their_body(self) -> None:
        created = self._root()
        await self.assertBody(comments.router, "get_comments", self.store.get_comments_file(self.project_id, self.path))
        thread, cursor = self.store.get_thread(self.project_id, self.path, created["id"])
        await self.assertBody(
            comments.router, "get_comment_thread", {"comment": thread, "cursor": cursor}
        )
        await self.assertBody(
            comments.router, "get_comment_thread", {"comment": None, "cursor": 0}
        )

    async def test_a_created_thread_matches_the_write_models(self) -> None:
        created = self._root()
        with_permissions = comment_capabilities.with_capabilities(created, ACTOR)
        for operation in ("create_comment", "create_comparison_comment", "update_comment", "pin_comment", "reattach_comment"):
            await self.assertBody(comments.router, operation, with_permissions)

    async def test_a_reply_round_trips_through_the_reply_models(self) -> None:
        created = self._root()
        thread, reply = self.store.add_reply(
            self.project_id,
            self.path,
            created["id"],
            "Answered",
            actor=ACTOR,
            content_format="md",
        )
        await self.assertBody(
            comments.router, "add_reply", {"comment": thread, "reply": reply}
        )
        for operation in ("update_reply", "delete_reply"):
            await self.assertBody(comments.router, operation, thread)

    async def test_a_reaction_returns_the_thread(self) -> None:
        created = self._root()
        reacted = self.store.set_reaction(
            self.project_id,
            self.path,
            created["id"],
            None,
            reaction="thumbs_up",
            present=True,
            actor=ACTOR,
        )
        for operation in ("add_comment_reaction", "remove_comment_reaction"):
            await self.assertBody(comments.router, operation, reacted)

    async def test_history_and_attachment_shapes(self) -> None:
        created = self._root()
        _, reply = self.store.add_reply(
            self.project_id, self.path, created["id"], "Answered", actor=ACTOR
        )
        await self.assertBody(
            comments.router,
            "get_comment_history",
            self.store.get_history(self.project_id, "root", created["id"]),
        )
        await self.assertBody(
            comments.router,
            "get_reply_history",
            self.store.get_history(self.project_id, "reply", reply["id"]),
        )

    async def test_upload_and_tombstone_shapes(self) -> None:
        # The real pending row, as ``comments_store.create_attachment`` returns it.
        attachment = {
            "id": "f" * 32,
            "filename": "snip.png",
            "mediaType": "image/png",
            "size": 64,
            "state": "pending",
            "width": 8,
            "height": 6,
            "url": f"/api/projects/{self.project_id}/comment-attachments/{'f' * 32}",
        }
        await self.assertBody(comments.router, "upload_comment_attachment", attachment)
        await self.assertBody(
            comments.router,
            "upload_comment_attachment",
            {
                "id": "e" * 32,
                "filename": "notes.txt",
                "mediaType": "text/plain",
                "size": 12,
                "state": "pending",
                "url": f"/api/projects/{self.project_id}/comment-attachments/{'e' * 32}",
            },
        )
        await self.assertBody(
            comments.router, "delete_comment", {"deleted": self._root()["id"]}
        )

    async def test_push_reports_where_it_wrote(self) -> None:
        await self.assertBody(
            comments.router,
            "push_comments",
            {
                "success": True,
                "message": "Comments exported",
                "comments_path": f"{self.path}/comments.json",
            },
        )


if __name__ == "__main__":
    unittest.main()
