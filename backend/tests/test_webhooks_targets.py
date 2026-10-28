"""The webhook URL policy: https and public addresses unless explicitly relaxed."""

from __future__ import annotations

import ipaddress
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import BadRequest  # noqa: E402
from app.services.webhooks import targets  # noqa: E402

PUBLIC = ipaddress.ip_address("93.184.216.34")
PRIVATE_ADDRESSES = (
    "10.0.0.5",
    "172.16.0.1",
    "192.168.1.10",
    "127.0.0.1",
    "169.254.169.254",
    "224.0.0.1",
    "240.0.0.1",
    "::1",
    "fd00::1",
    "fe80::1",
)


def resolves_to(*addresses: str):
    """A resolver returning the given addresses regardless of host."""

    return lambda _host, _port: [ipaddress.ip_address(value) for value in addresses]


class WebhookTargetTests(unittest.TestCase):
    def assert_refused(self, url: str, **kwargs) -> None:
        with self.assertRaises(BadRequest) as caught:
            targets.require_target(url, **kwargs)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.code, targets.REFUSED)

    def test_a_public_https_target_is_accepted_unchanged(self) -> None:
        with patch.object(targets, "resolve_addresses", resolves_to(str(PUBLIC))):
            self.assertEqual(
                targets.require_target("https://hooks.example.test/path?q=1"),
                "https://hooks.example.test/path?q=1",
            )

    def test_http_is_refused_by_default(self) -> None:
        with patch.object(targets, "resolve_addresses", resolves_to(str(PUBLIC))):
            self.assert_refused("http://hooks.example.test/path", allow_private=False)

    def test_every_non_public_range_is_refused(self) -> None:
        for value in PRIVATE_ADDRESSES:
            with self.subTest(address=value):
                with patch.object(targets, "resolve_addresses", resolves_to(value)):
                    self.assert_refused("https://hooks.example.test", allow_private=False)

    def test_one_private_answer_refuses_a_mixed_host(self) -> None:
        with patch.object(
            targets, "resolve_addresses", resolves_to(str(PUBLIC), "10.0.0.5")
        ):
            self.assert_refused("https://hooks.example.test", allow_private=False)

    def test_embedded_credentials_are_refused(self) -> None:
        with patch.object(targets, "resolve_addresses", resolves_to(str(PUBLIC))):
            self.assert_refused(
                "https://user:pass@hooks.example.test", allow_private=False
            )

    def test_a_url_without_a_host_is_refused(self) -> None:
        self.assert_refused("https:///no-host", allow_private=False)

    def test_an_unresolvable_host_is_refused(self) -> None:
        with patch.object(
            targets,
            "resolve_addresses",
            side_effect=socket.gaierror("no such host"),
        ):
            self.assert_refused("https://missing.example.test", allow_private=False)

    def test_allow_private_accepts_http_and_private_addresses(self) -> None:
        with patch.object(targets, "resolve_addresses", resolves_to("127.0.0.1")):
            self.assertEqual(
                targets.require_target("http://127.0.0.1:9000/hook", allow_private=True),
                "http://127.0.0.1:9000/hook",
            )

    def test_allow_private_still_refuses_a_missing_host_or_bad_scheme(self) -> None:
        with patch.object(targets, "resolve_addresses", resolves_to("127.0.0.1")):
            self.assert_refused("ftp://127.0.0.1/hook", allow_private=True)
            self.assert_refused("https://", allow_private=True)

    def test_the_setting_is_the_default(self) -> None:
        from app.core.config import settings

        with patch.object(settings, "PRISM_WEBHOOK_ALLOW_PRIVATE", True):
            with patch.object(targets, "resolve_addresses", resolves_to("127.0.0.1")):
                self.assertEqual(
                    targets.require_target("http://127.0.0.1/hook"),
                    "http://127.0.0.1/hook",
                )
        with patch.object(settings, "PRISM_WEBHOOK_ALLOW_PRIVATE", False):
            with patch.object(targets, "resolve_addresses", resolves_to("127.0.0.1")):
                self.assert_refused("http://127.0.0.1/hook")


if __name__ == "__main__":
    unittest.main()
