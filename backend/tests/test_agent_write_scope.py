"""Bearer tokens need the write scope as well as the role to change anything (D-P2-32).

A desktop agent signed in with ``api:read`` carries its user's role. Catalog writes
already refused it; designer, admin and release routes, Git push and the
agent-to-browser handoff did not.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import install_error_handlers  # noqa: E402
from app.api import git_http  # noqa: E402
from app.core import security  # noqa: E402
from app.core.security import AuthenticatedUser  # noqa: E402

try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError):  # starlette's TestClient needs httpx
    TestClient = None  # type: ignore[assignment]


def agent(role: str, *scopes: str) -> AuthenticatedUser:
    return AuthenticatedUser(email=f"{role}@example.com", name=role, role=role, auth_type="agent",
                             client_id="kicad-agent", scopes=list(scopes))


def run(dependency, user: AuthenticatedUser) -> AuthenticatedUser:
    return asyncio.run(dependency(user))


class MutationDependencyTest(unittest.TestCase):
    CASES = (
        (security.require_designer, "designer", "design:write"),
        (security.require_admin, "admin", "admin"),
        (security.require_project_release_actor, "designer", "release:approve"),
    )

    def test_a_read_only_token_is_refused_whatever_its_role(self) -> None:
        for dependency, role, scope in self.CASES:
            with self.subTest(dependency=dependency.__name__), self.assertRaises(HTTPException) as caught:
                run(dependency, agent(role, "api:read"))
            self.assertEqual(caught.exception.status_code, 403)
            self.assertEqual(caught.exception.detail, f"{scope} scope required")

    def test_a_write_token_and_a_browser_session_pass(self) -> None:
        for dependency, role, _scope in self.CASES:
            with self.subTest(dependency=dependency.__name__):
                run(dependency, agent(role, "api:read", "api:write"))
                run(dependency, AuthenticatedUser(email="s@example.com", name="s", role=role))

    def test_the_role_still_applies_to_a_write_token(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            run(security.require_designer, agent("viewer", "api:write"))
        self.assertEqual(caught.exception.status_code, 403)


@unittest.skipIf(TestClient is None, "starlette.testclient needs httpx")
class DesignerRouteTest(unittest.TestCase):
    """Through a real designer route and its dependencies, as the review reproduced it."""

    def post(self, user: AuthenticatedUser):
        from app.api import project_viewer_3d

        app = FastAPI()

        install_error_handlers(app)
        app.include_router(project_viewer_3d.router, prefix="/api/projects")
        app.dependency_overrides[security.get_current_user] = lambda: user
        with patch.object(project_viewer_3d, "get_project_for_role_or_404"), \
                patch.object(project_viewer_3d, "_generate_bundle", return_value={"job_id": "job_x"}) as generate:
            response = TestClient(app).post("/api/projects/prj_abc/webgpu-3d/generate", json={})
        return response, generate

    def test_a_read_only_agent_cannot_queue_a_build(self) -> None:
        response, generate = self.post(agent("designer", "api:read"))
        self.assertEqual(response.status_code, 403)
        generate.assert_not_called()

    def test_a_write_agent_reaches_the_service(self) -> None:
        _response, generate = self.post(agent("designer", "api:read", "api:write"))
        generate.assert_called_once()


class GitScopeTest(unittest.TestCase):
    def authorise(self, user: AuthenticatedUser, path: str) -> None:
        with patch("app.api._helpers.workspace.get_project_for_role", return_value={"id": "prj_abc", "name": "Board"}):
            git_http._authorise(user, "prj_abc", path)

    def test_a_read_only_agent_clones_but_cannot_push(self) -> None:
        self.authorise(agent("designer", "api:read"), "/git-upload-pack")
        with self.assertRaises(HTTPException) as caught:
            self.authorise(agent("designer", "api:read"), "/git-receive-pack")
        self.assertEqual(caught.exception.status_code, 403)

    def test_a_write_agent_pushes(self) -> None:
        self.authorise(agent("designer", "api:read", "api:write"), "/git-receive-pack")


class BrowserHandoffTest(unittest.TestCase):
    def handoff(self, scope: str):
        from app.services import provider_auth_service

        payload = {"email": "d@example.com", "name": "d", "role": "designer", "scope": scope}
        with patch("app.services.agent_auth_service.validate_agent_token", return_value=payload), \
                patch.object(provider_auth_service, "_bootstrap_url_for", return_value="https://p/oauth/bootstrap?t"):
            return provider_auth_service.build_bootstrap_nonce_url_for_agent("https://p", "v1.x", "https://p/")

    def test_a_read_only_agent_cannot_open_a_signed_in_session(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self.handoff("api:read")
        self.assertEqual(caught.exception.status_code, 403)

    def test_a_write_agent_can(self) -> None:
        self.assertEqual(self.handoff("api:read api:write"), ("https://p/oauth/bootstrap?t", "d@example.com"))


if __name__ == "__main__":
    unittest.main()
