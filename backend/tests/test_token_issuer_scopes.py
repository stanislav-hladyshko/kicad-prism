"""Every bearer-token issuer still yields a caller that may read.

Reads behind ``require_viewer`` now need ``api:read`` from bearer callers, with
``api:write`` implying it and ``*`` holding everything. Each test takes the scope
set an issuer actually mints, resolves it through ``security._resolve_bearer_user``
(the code path a request takes), and asks the caller policy.
"""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.service_clients import CreateServiceClientRequest  # noqa: E402
from app.core import policy, security  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.services import (  # noqa: E402
    agent_auth_service,
    auth_service,
    mcp_auth_service,
    provider_auth_service,
    service_client_service,
)

SECRET = "test-secret-at-least-32-characters-long-x"


def _reject(*_args, **_kwargs):
    raise HTTPException(status_code=401, detail="not this kind")


class AgentTokenTests(unittest.TestCase):
    """The KiCad agent asks for a subset of {api:read, api:write}; empty means both."""

    def _caller(self, requested: str):
        scope = agent_auth_service.normalize_agent_scope(requested)
        payload = {"email": "d@example.com", "name": "D", "role": "designer", "scope": scope, "client_id": "kicad-agent"}
        with patch.object(agent_auth_service, "validate_agent_token", return_value=payload), patch.object(
            agent_auth_service, "touch_agent_token"
        ):
            return security._resolve_bearer_user("v1.agent.token")

    def test_every_grantable_agent_scope_set_reads(self) -> None:
        for requested in ("", "api:read", "api:write", "api:read api:write"):
            with self.subTest(requested=requested):
                caller = self._caller(requested)
                self.assertEqual(caller.auth_type, "agent")
                self.assertTrue(policy.allows(caller, "read"))

    def test_default_agent_token_also_writes(self) -> None:
        self.assertTrue(policy.allows(self._caller(""), "design"))


class _Row(dict):
    pass


class _FakeCatalogDb:
    def __init__(self, row: dict) -> None:
        self.row = row

    @contextmanager
    def connection(self):
        row = self.row

        class _Conn:
            def execute(self, *_args, **_kwargs):
                class _Result:
                    rowcount = 1

                    def fetchone(self_inner):
                        return _Row(row)

                return _Result()

            def commit(self):
                return None

        yield _Conn()


class ServiceClientTokenTests(unittest.TestCase):
    """Admins choose a client's scopes (default ["api:read"]); a token may request a subset."""

    def _caller(self, client_scopes: list[str], requested: str = ""):
        secret = "prism_secret_value"
        row = {
            "client_id": "prism_abc",
            "name": "PLM",
            "secret_hash": service_client_service._hash_secret(secret),
            "role": "viewer",
            "scopes": json.dumps(client_scopes),
            "enabled": 1,
        }
        with patch.object(settings, "SESSION_SECRET", SECRET), patch.object(
            service_client_service, "_db", return_value=_FakeCatalogDb(row)
        ), patch.object(agent_auth_service, "validate_agent_token", side_effect=_reject), patch.object(
            provider_auth_service, "validate_access_token", side_effect=_reject
        ):
            issued = service_client_service.issue_client_credentials_token(
                client_id="prism_abc", client_secret=secret, requested_scope=requested
            )
            return security._resolve_bearer_user(issued["access_token"])

    def test_default_client_scopes_read(self) -> None:
        caller = self._caller(CreateServiceClientRequest().scopes)
        self.assertEqual(caller.auth_type, "service_client")
        self.assertTrue(policy.allows(caller, "read"))

    def test_write_only_client_and_write_only_token_still_read(self) -> None:
        self.assertTrue(policy.allows(self._caller(["api:write"]), "read"))
        self.assertTrue(policy.allows(self._caller(["api:read", "api:write"], "api:write"), "read"))

    def test_client_without_an_api_scope_cannot_read(self) -> None:
        # Documented consequence: such a client read projects before this change.
        self.assertFalse(policy.allows(self._caller([]), "read"))


class ExternalJwtTests(unittest.TestCase):
    """Scopes come from the configured claim of an externally issued JWT."""

    def _caller(self, claims: dict):
        payload = {"sub": "plm", "role": "viewer", **claims}
        with patch.object(settings, "OAUTH_EXTERNAL_JWT_ISSUER_URL", "https://idp.example.com"), patch.object(
            settings, "OAUTH_EXTERNAL_JWT_AUDIENCE", "prism"
        ), patch.object(settings, "OAUTH_EXTERNAL_JWT_ROLE_CLAIM", "role"), patch.object(
            settings, "OAUTH_EXTERNAL_JWT_SCOPES_CLAIM", "scope"
        ), patch.object(auth_service, "_verify_jwt", return_value=payload):
            return security._resolve_bearer_user("eyJexternal.jwt.token")

    def test_space_separated_and_list_scope_claims_read(self) -> None:
        for scope in ("api:read", "api:write", ["api:read"], ["api:write"], "*"):
            with self.subTest(scope=scope):
                caller = self._caller({"scope": scope})
                self.assertEqual(caller.auth_type, "external_service")
                self.assertTrue(policy.allows(caller, "read"))

    def test_token_without_an_api_scope_cannot_read(self) -> None:
        # Documented consequence: the identity provider must issue api:read or api:write.
        self.assertFalse(policy.allows(self._caller({}), "read"))


class ProviderTokenTests(unittest.TestCase):
    """KiCad provider tokens are always remote_symbols.read and stay out of reads."""

    def test_provider_token_reads_only_remote_symbols(self) -> None:
        scope = provider_auth_service.normalize_provider_scope("")
        payload = {"email": "d@example.com", "name": "D", "role": "admin", "scope": scope}
        with patch.object(agent_auth_service, "validate_agent_token", side_effect=_reject), patch.object(
            mcp_auth_service, "validate_access_token", side_effect=_reject
        ), patch.object(provider_auth_service, "validate_access_token", return_value=payload):
            caller = security._resolve_bearer_user("v1.provider.token")
        self.assertEqual(caller.auth_type, "kicad_provider")
        self.assertFalse(policy.allows(caller, "read"))
        self.assertTrue(policy.allows(caller, "remote_symbols"))


if __name__ == "__main__":
    unittest.main()
