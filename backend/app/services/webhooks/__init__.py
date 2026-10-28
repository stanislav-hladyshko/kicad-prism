"""Signed outbound webhooks fed by the event log (ADR §3.10, v5.2).

``store`` owns the registrations and their delivery history, ``signing`` is the
Standard Webhooks secret and header format, ``targets`` is the URL policy
(https, public addresses unless ``PRISM_WEBHOOK_ALLOW_PRIVATE``), and
``delivery`` is the worker's pass: read the log after each cursor, post what the
filters admit, retry with backoff, and stop a registration that has failed for
24 hours. Admin routes live in ``app/api/webhook_settings.py``.
"""

from __future__ import annotations

__all__: list[str] = []
