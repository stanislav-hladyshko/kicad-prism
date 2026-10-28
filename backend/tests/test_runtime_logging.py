"""Time-boxed debug logging for ops (ADR scripting-and-mcp §11, OPS-03).

The setting is stored in the workspace database and applied by every process's
poller. These tests run the real migration and SQL against a scratch schema and
inject the clock, so a window can be opened and expired without waiting.
"""

from __future__ import annotations

import logging
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import runtime_logging  # noqa: E402
from app.services.workspace_schema_migrations import WORKSPACE_LEDGER  # noqa: E402
from support.postgres import requires_scratch_postgres, scratch_schema  # noqa: E402

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=6)


@requires_scratch_postgres("runtime logging tests")
class RuntimeLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_logging._stop_poller()
        self.scratch = scratch_schema(
            self, "runtime_logging", ledgers=(WORKSPACE_LEDGER,)
        )
        patcher = patch.object(runtime_logging, "connect", self.scratch.connection)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.app_logger = logging.getLogger("app")
        self.root = logging.getLogger()
        self.app_level = self.app_logger.level
        self.root_level = self.root.level
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        runtime_logging.clear("test")
        runtime_logging.apply(now=LATER)
        self.app_logger.setLevel(self.app_level)
        self.root.setLevel(self.root_level)

    def test_the_window_is_bounded_and_defaults_to_the_app_logger(self) -> None:
        for minutes in (-1, 0, 31):
            with self.subTest(minutes=minutes):
                with self.assertRaises(ValueError):
                    runtime_logging.set_debug(minutes, by="admin", now=NOW)

        value = runtime_logging.set_debug(5, by="admin", now=NOW)

        self.assertEqual(value["until"], "2026-10-26T12:05:00Z")
        self.assertEqual(value["loggers"], ["app"])
        self.assertEqual(runtime_logging.current(now=NOW), value)

    def test_named_loggers_are_deduplicated_and_default_when_empty(self) -> None:
        value = runtime_logging.set_debug(
            30,
            loggers=["app.services.ops", " app.services.ops ", "app.prism_worker"],
            by="admin",
            now=NOW,
        )
        self.assertEqual(
            value["loggers"], ["app.services.ops", "app.prism_worker"]
        )

        value = runtime_logging.set_debug(
            1, loggers=["  "], by="admin", now=NOW
        )
        self.assertEqual(value["loggers"], ["app"])

    def test_apply_switches_levels_and_reverts_past_the_deadline(self) -> None:
        self.app_logger.setLevel(logging.NOTSET)
        value = runtime_logging.set_debug(5, by="admin", now=NOW)

        runtime_logging.apply(now=NOW)
        self.assertEqual(self.app_logger.level, logging.DEBUG)
        self.assertEqual(runtime_logging.current(now=NOW), value)

        # Applying again inside the window is idempotent.
        runtime_logging.apply(now=NOW + timedelta(minutes=1))
        self.assertEqual(self.app_logger.level, logging.DEBUG)

        runtime_logging.apply(now=LATER)
        self.assertEqual(self.app_logger.level, logging.NOTSET)
        self.assertIsNone(runtime_logging.current(now=LATER))

    def test_the_root_logger_reverts_to_info(self) -> None:
        self.root.setLevel(logging.WARNING)
        runtime_logging.set_debug(5, loggers=["root"], by="admin", now=NOW)

        runtime_logging.apply(now=NOW)
        self.assertEqual(self.root.level, logging.DEBUG)

        runtime_logging.apply(now=LATER)
        self.assertEqual(self.root.level, logging.INFO)

    def test_an_explicit_prior_level_is_restored(self) -> None:
        self.app_logger.setLevel(logging.WARNING)
        runtime_logging.set_debug(5, by="admin", now=NOW)

        runtime_logging.apply(now=NOW)
        self.assertEqual(self.app_logger.level, logging.DEBUG)

        runtime_logging.apply(now=LATER)
        self.assertEqual(self.app_logger.level, logging.WARNING)

    def test_clear_reverts_the_levels(self) -> None:
        self.app_logger.setLevel(logging.NOTSET)
        runtime_logging.set_debug(5, by="admin", now=NOW)
        runtime_logging.apply(now=NOW)
        self.assertEqual(self.app_logger.level, logging.DEBUG)

        runtime_logging.clear("admin")

        self.assertIsNone(runtime_logging.current(now=NOW))
        runtime_logging.apply(now=NOW)
        self.assertEqual(self.app_logger.level, logging.NOTSET)

    def test_a_database_outage_never_raises_from_apply_or_current(self) -> None:
        with patch.object(
            runtime_logging, "connect", side_effect=RuntimeError("database unavailable")
        ):
            runtime_logging.apply(now=NOW)
            self.assertIsNone(runtime_logging.current(now=NOW))


if __name__ == "__main__":
    unittest.main()
