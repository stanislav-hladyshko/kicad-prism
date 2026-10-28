"""The catalog read routes' typed bodies are the bodies they returned before.

Each route is called through FastAPI (so the response model is applied) over a
service stub that returns the exact dictionary the catalog service builds. The
assertion compares the full JSON body, so a field dropped, added or coerced by
a model fails here rather than in a client.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import catalog_admin, catalog_part_relations  # noqa: E402
from app.core.security import (  # noqa: E402
    AuthenticatedUser,
    require_catalog_admin,
    require_catalog_browser,
    require_catalog_reader,
    require_catalog_writer,
)

USER = AuthenticatedUser(email="admin@example.com", name="Admin", role="admin")


def _validation_run() -> dict[str, Any]:
    return {
        "id": "run-1",
        "component_id": "cmp-1",
        "revision_id": "rev-1",
        "asset_id": "asset-1",
        "asset_type": "symbol",
        "checker_type": "klc",
        "status": "passed",
        "error_count": 0,
        "warning_count": 1,
        "exit_code": 0,
        "tool_version": "1.2.3",
        "created_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "inherited": False,
        "inherited_from_revision_id": "",
        "reports": {"summary": "/api/catalog/validation/runs/run-1"},
    }


def _validation_summary() -> dict[str, Any]:
    latest = _validation_run()  # no "findings" key: a summary run carries none
    return {
        "status": "warning",
        "enabled": True,
        "release_gate": "warn",
        "revision_id": "rev-1",
        "error_count": 0,
        "warning_count": 1,
        "missing_required_assets": [],
        "assets": [
            {
                "asset_id": "asset-1",
                "asset_type": "symbol",
                "asset_name": "R.kicad_sym",
                "target_library": "Device",
                "target_name": "R",
                "status": "passed",
                "latest_run": latest,
            }
        ],
    }


def _component() -> dict[str, Any]:
    return {
        "id": "cmp-1",
        "slug": "r-10k",
        "external_source": "manual",
        "external_id": "",
        "external_workflow_source": "",
        "external_workflow_id": "",
        "external_workflow_url": "",
        "external_url": "",
        "external_payload": {},
        "external_updated_at": "",
        "sync_status": "",
        "sync_error": "",
        "source": "manual",
        "identity_kind": "mpn",
        "kind": "part",
        "name": "Resistor 10k",
        "value": "10k",
        "manufacturer": "Yageo",
        "mpn": "RC0603FR-0710KL",
        "description": "Thick film resistor",
        "package_name": "0603",
        "category": "Resistors",
        "datasheet_url": "https://example.com/r.pdf",
        "vendor": "",
        "vendor_part_number": "",
        "mass_g": "0.01",
        "rqjc_c_w": "",
        "rqjc_top_c_w": "",
        "temp_max_c": "155",
        "temp_min_c": "-55",
        "power_dissipation_w": "0.1",
        "rate": "",
        "sap_code": "",
        "keywords": ["resistor"],
        "extra_fields": {"tolerance": "1%"},
        "interface": {},
        "source_ref": {},
        "availability_state": "place_ready",
        "missing_assets": [],
        "place_enabled": True,
        "local_inventory": {
            "source": "csv",
            "quantity": 250.0,
            "uom": "pcs",
            "inventory_status": "in_stock",
            "fetch_status": "ok",
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "mixed_units": False,
            "mixed_status": False,
            "mixed_fetch": False,
            "mixed_freshness": False,
        },
        "stock_known": True,
        "stock_quantity": 250.0,
        "stock_uom": "pcs",
        "inventory_status": "in_stock",
        "supply": {
            "sources": [
                {
                    "kind": "local",
                    "id": "csv",
                    "display_name": "CSV",
                    "stock": 250.0,
                    "uom": "pcs",
                    "stock_status": "in_stock",
                    "fetch_status": "ok",
                    "fetched_at": "2026-01-01T00:00:00+00:00",
                    "mixed_units": False,
                    "mixed_status": False,
                    "mixed_fetch": False,
                    "mixed_freshness": False,
                }
            ]
        },
        "serial_number": "",
        "lot_number": "",
        "pedigree": "",
        "last_synced_at": "2026-01-01T00:00:00+00:00",
        "is_active": True,
        "revision_id": "rev-1",
        "revision": 1,
        "version": "1.0.0",
        "parent_revision_id": "",
        "change_kind": "create",
        "change_summary": "Create component",
        "created_by": "admin@example.com",
        "manifest_hash": "abc123",
        "component_created_at": "2026-01-01T00:00:00+00:00",
        "component_updated_at": "2026-01-01T00:00:00+00:00",
        "revision_created_at": "2026-01-01T00:00:00+00:00",
        "revision_updated_at": "2026-01-01T00:00:00+00:00",
        "current_revision_id": "rev-1",
        "released_revision_id": "",
        "is_historical_revision": False,
        "summary": "A resistor",
        "library_name": "Device",
        "symbol_name": "R",
        "representations": [
            {
                "id": "rep-1",
                "revision_id": "rev-1",
                "label": "Default",
                "symbol": {
                    "id": "asset-1",
                    "asset_type": "symbol",
                    "name": "R.kicad_sym",
                    "target_library": "Device",
                    "target_name": "R",
                    "sha256": "sym",
                    "preview_id": "preview-1",
                },
                "footprint": None,
                "is_default": True,
                "display_order": 0,
                "source_internal_part_number": "",
                "provenance": {},
            }
        ],
        "default_representation_id": "rep-1",
        "effective_representation_id": "rep-1",
        "release_status": "released",
        "workflow_stage": "released",
        "released_view": False,
        "assets": [
            {
                "id": "asset-1",
                "asset_type": "symbol",
                "name": "R.kicad_sym",
                "target_library": "Device",
                "target_name": "R",
                "source_group": "",
                "sha256": "sym",
                "size_bytes": 123,
                "content_type": "application/x-kicad-symbol",
                "required": True,
            }
        ],
        "previews": [
            {
                "id": "preview-1",
                "asset_id": "asset-1",
                "kind": "symbol",
                "preview_key": "symbol",
                "unit": "1",
                "unit_label": "Unit A",
                "status": "ready",
                "content_type": "image/svg+xml",
                "file_path": "/store/previews/r.svg.gz",
                "generation_error": "",
                "sha256": "sha",
                "generator_fingerprint": "fp",
                "generator_version": "1",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ],
        "validation": _validation_summary(),
        "future_key": "kept",
    }


def _summary_component() -> dict[str, Any]:
    """The lightweight payload the release queue and metadata grid read."""
    full = _component()
    drop = {
        "external_source", "external_id", "external_workflow_source", "external_workflow_id",
        "external_workflow_url", "external_url", "external_payload", "external_updated_at",
        "sync_status", "sync_error", "supply", "serial_number", "lot_number", "pedigree",
        "last_synced_at", "is_active", "parent_revision_id", "change_kind", "change_summary",
        "manifest_hash", "component_created_at", "revision_created_at", "current_revision_id",
        "released_revision_id", "is_historical_revision", "representations",
        "default_representation_id", "effective_representation_id", "future_key",
    }
    summary = {key: value for key, value in full.items() if key not in drop}
    summary["local_inventory"] = None
    summary["stock_known"] = False
    summary["stock_quantity"] = 0.0
    summary["stock_uom"] = ""
    summary["inventory_status"] = ""
    summary["assets"] = []
    summary["previews"] = []
    return summary


def _job() -> dict[str, Any]:
    return {
        "job_id": "job-1", "kind": "catalog_validation", "status": "completed", "message": "",
        "percent": 100.0, "payload": {"component_ids": ["cmp-1"]},
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:01+00:00",
        "worker_pool": "catalog", "priority": 100, "artifact_key": None, "project_id": None,
        "repository_id": None, "requested_by": USER.email, "stage": "", "result_path": None,
        "result_digest": None, "result_metadata": {}, "log_path": None, "error_code": "",
        "error_message": "", "fence": 1, "lease_owner": "", "lease_expires_at": None,
        "heartbeat_at": None, "available_at": "2026-01-01T00:00:00+00:00", "attempt": 1,
        "max_attempts": 3, "cancel_requested_at": None, "started_at": None, "completed_at": None,
        "resource_requirements": {}, "lock_requirements": [], "id": "job-1",
        "job_type": "catalog_validation", "result": {"queued": 1}, "checkpoint": {},
        "created_by": USER.email, "attempts": 1, "progress": 100.0, "queued": 1,
    }


def _session() -> dict[str, Any]:
    return {
        "id": "session-1", "scope": "project", "project_id": "prj-1", "project_ids": ["prj-1"],
        "project_revisions": {"prj-1": "abc"}, "source_revision": "abc", "selection": {},
        "status": "staged", "error_message": "", "created_by": USER.email,
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        "proposal_count": 1, "candidate_count": 1,
    }


def _proposal() -> dict[str, Any]:
    return {
        "id": "proposal-1", "session_id": "session-1", "dedupe_key": "k", "component_uid": "u",
        "reference": "R1", "metadata": {"value": "10k"}, "assets": [], "provenance": [],
        "findings": [], "status": "candidate", "accepted_component_id": "", "draft": {},
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
    }


def _batch() -> dict[str, Any]:
    return {
        "id": "batch-1", "source": "grid", "status": "ready", "schema_version": "prism.component_metadata_a1",
        "change_summary": "Bulk update", "unknown_fields": [], "created_by": USER.email,
        "total_items": 1, "valid_items": 1, "applied_items": 0, "failed_items": 0,
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        "items": [
            {
                "id": "item-1", "component_id": "cmp-1", "expected_revision_id": "rev-1",
                "name": "Resistor 10k", "mpn": "RC0603FR-0710KL", "patch": {"value": "22k"},
                "diff": [{"field": "value", "label": "Value", "before": "10k", "after": "22k"}],
                "validation_status": "valid", "error_message": "", "applied_revision_id": "",
            }
        ],
    }


def _health() -> dict[str, Any]:
    return {
        "enabled": True, "checker_available": True, "checker_path": "/tools", "release_gate": "warn",
        "total_components": 1, "released": 0, "place_ready": 1, "missing_files": 0,
        "released_assets_missing_on_disk": None, "preview_failed": 0,
        "validation": {"passed": 1, "warning": 0, "failed": 0, "skipped": 0, "not_run": 0},
    }


def _comparison() -> dict[str, Any]:
    asset = {
        "assetId": "asset-1", "assetType": "symbol", "targetLibrary": "Device",
        "targetName": "R", "sha256": "sym", "sizeBytes": 123, "previewId": "preview-1",
        "previewStatus": "ready", "previewSha256": "sha", "previewGeneratorFingerprint": "fp",
        "previews": [{
            "previewId": "preview-1", "previewStatus": "ready", "previewSha256": "sha",
            "previewGeneratorFingerprint": "fp", "unit": "1", "unitLabel": "Unit A",
        }],
    }
    representation = {
        "label": "Default", "symbolAssetId": "asset-1", "footprintAssetId": "",
        "isDefault": True, "displayOrder": 0, "sourceInternalPartNumber": "", "provenance": {},
    }
    return {
        "componentId": "cmp-1",
        "before": {"revisionId": "rev-1", "version": 1, "manifestHash": "a"},
        "after": {"revisionId": "rev-2", "version": 2, "manifestHash": "b"},
        "summary": {"metadataChanges": 1, "assetChanges": 0, "representationChanges": 0},
        "metadataChanges": [{"field": "value", "before": "10k", "after": "22k", "status": "modified"}],
        "assetChanges": [{"key": "symbol:Device:R", "before": asset, "after": None, "status": "removed"}],
        "representationChanges": [
            {"key": "asset-1:", "before": representation, "after": None, "status": "removed"}
        ],
    }


def _audit_event() -> dict[str, Any]:
    return {
        "id": "event-1", "component_id": "cmp-1", "sequence": 1, "revision_id": "rev-1",
        "event_type": "component.created", "actor": USER.email, "details_json": '{"value":"10k"}',
        "details": {"value": "10k"}, "previous_hash": "", "event_hash": "hash",
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _field() -> dict[str, Any]:
    return {
        "id": "builtin:value", "key": "value", "label": "Value", "description": "",
        "group": "identity", "type": "text", "unit": "", "enum_values": [], "storage_kind": "column",
        "storage_key": "value", "built_in": True, "required": True, "display_order": 0,
        "archived": False, "created_by": "system", "updated_by": "system",
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
    }


def _snapshot_row() -> dict[str, Any]:
    """A ``catalog_import_snapshots`` row: timestamps are raw datetimes, as psycopg returns them."""
    return {
        "id": "snap-1", "source_type": "browser", "display_name": "KiCad libraries",
        "source_locator": "", "status": "uploading", "manifest_sha256": "",
        "file_count": 0, "size_bytes": 0, "created_by": USER.email,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "completed_at": None,
    }


def _discovery_match() -> dict[str, Any]:
    return {
        "component_id": "cmp-1", "revision_id": "rev-1", "version": 1, "name": "Resistor 10k",
        "manufacturer": "Yageo", "manufacturer_part_number": "RC0603FR-0710KL", "is_active": True,
    }


def _discovery() -> dict[str, Any]:
    return {
        "components": [
            {
                "id": "disc-1",
                "symbol_name": "R",
                "library": "Device",
                "metadata": {
                    "value": "10k", "description": "Resistor", "datasheet": "",
                    "manufacturer": "Yageo", "manufacturer_part_number": "RC0603FR-0710KL",
                    "fields": {"Value": "10k", "Footprint": "Resistor_SMD:R_0603"},
                },
                "symbol": {"relative_path": "Device.kicad_sym"},
                "footprint_reference": "Resistor_SMD:R_0603",
                "footprint": {
                    "status": "resolved",
                    "selected": {
                        "relative_path": "Resistor_SMD.pretty/R_0603.kicad_mod",
                        "library": "Resistor_SMD", "name": "R_0603", "filename_name": "R_0603",
                    },
                    "candidates": [],
                },
                "models": [{
                    "reference": "${KICAD}/R_0603.step", "status": "resolved",
                    "candidates": [{"relative_path": "models/R_0603.step", "size_bytes": 12,
                                    "suffix": ".step"}],
                }],
                "findings": [{"code": "missing_metadata_datasheet", "severity": "warning",
                              "message": "Metadata is missing: datasheet."}],
            }
        ],
        "required_paths": ["Device.kicad_sym"],
        "inventory_file_count": 2,
        "discovery_file_count": 3,
        "directory_signature": "sig",
    }


class CatalogTypedResponses(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(patch.stopall)
        self.app = FastAPI()
        self.app.include_router(catalog_admin.router)
        self.app.include_router(catalog_part_relations.router)
        for dependency in (
            require_catalog_admin,
            require_catalog_browser,
            require_catalog_reader,
            require_catalog_writer,
        ):
            self.app.dependency_overrides[dependency] = lambda: USER
        self.service = MagicMock()

    def _get(self, path: str) -> Any:
        with patch.object(catalog_admin, "catalog_service", self.service), \
                patch.object(catalog_part_relations, "catalog_service", self.service):
            response = TestClient(self.app, raise_server_exceptions=False).get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def _json(self, path: str) -> Any:
        return self._get(path).json()

    # -- collection reads -------------------------------------------------

    def test_components_preserve_full_and_lightweight_bodies(self) -> None:
        page = {
            "items": [_component(), _summary_component()], "total": 2, "page": 1,
            "page_size": 50, "pages": 1,
        }
        self.service.list_components.return_value = page
        self.assertEqual(self._json("/api/catalog/components"), page)

    def test_categories_and_workflow_summary(self) -> None:
        categories = {"categories": [{"name": "Resistors", "count": 2}]}
        self.service.list_categories.return_value = categories["categories"]
        self.assertEqual(self._json("/api/catalog/categories"), categories)

        workflow = {"stages": [{"workflow_stage": "open", "count": 1}]}
        self.service.workflow_summary.return_value = workflow
        self.assertEqual(self._json("/api/catalog/workflow/summary"), workflow)

    def test_release_queue_keeps_its_summary(self) -> None:
        page = {
            "items": [_summary_component()], "total": 1, "page": 1, "page_size": 50, "pages": 1,
            "summary": {"qa_review": 1, "done": 0, "blocked": 1},
        }
        self.service.list_components.return_value = {
            key: value for key, value in page.items() if key != "summary"
        }
        self.service.release_queue_summary.return_value = page["summary"]
        self.assertEqual(self._json("/api/catalog/release-queue"), page)

    def test_health(self) -> None:
        self.service.catalog_health.return_value = _health()
        self.assertEqual(self._json("/api/catalog/health"), _health())

    # -- component reads --------------------------------------------------

    def test_component_and_revision_reads(self) -> None:
        self.service.get_component.return_value = _component()
        self.assertEqual(self._json("/api/catalog/components/cmp-1"), _component())

        self.service.get_component_revision.return_value = _component()
        self.assertEqual(
            self._json("/api/catalog/components/cmp-1/revisions/rev-1"), _component()
        )

        self.service.list_component_revisions.return_value = [
            {
                "id": "rev-1", "component_id": "cmp-1", "version": 1, "parent_revision_id": "",
                "change_kind": "create", "change_summary": "Create", "created_by": USER.email,
                "manifest_hash": "a", "release_status": "released", "workflow_stage": "released",
                "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        self.assertEqual(
            self._json("/api/catalog/components/cmp-1/revisions"),
            {"items": self.service.list_component_revisions.return_value},
        )

    def test_revision_comparison(self) -> None:
        self.service.compare_component_revisions.return_value = _comparison()
        self.assertEqual(
            self._json("/api/catalog/components/cmp-1/revisions/compare?before=rev-1&after=rev-2"),
            _comparison(),
        )

    def test_audit_reads(self) -> None:
        self.service.list_component_audit_events.return_value = [_audit_event()]
        self.assertEqual(
            self._json("/api/catalog/components/cmp-1/audit"), {"items": [_audit_event()]}
        )

        verification = {
            "valid": True, "coverage": "complete", "reason": "", "event_count": 1,
            "verified_count": 1, "revision_count": 1, "asset_count": 1, "purged_asset_ids": [],
            "first_invalid_event_id": "", "head_hash": "hash", "anchored_head_hash": "hash",
        }
        self.service.verify_component_audit_chain.return_value = verification
        self.assertEqual(self._json("/api/catalog/components/cmp-1/audit/verify"), verification)

    def test_usage_filters_to_visible_projects(self) -> None:
        usage = {
            "id": "usage-1", "component_id": "cmp-1", "project_id": "prj-1",
            "source_revision": "abc", "references_json": '["R1"]', "details_json": "[]",
            "source": "project_import", "is_current": True,
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00", "references": ["R1"], "details": [],
        }
        hidden = {**usage, "id": "usage-2", "project_id": "prj-2"}
        self.service.list_component_usage.return_value = [usage, hidden]
        workspace = MagicMock()
        workspace.get_all_projects.return_value = [{"id": "prj-1"}]
        with patch.object(catalog_admin, "workspace", workspace):
            self.assertEqual(
                self._json("/api/catalog/components/cmp-1/usage"), {"items": [usage]}
            )

    def test_reviews_and_releases(self) -> None:
        review = {
            "id": "review-1", "component_id": "cmp-1", "revision_id": "rev-1",
            "reviewer": USER.email, "reviewer_role": "admin", "decision": "approved",
            "note": "", "manifest_hash": "a", "validation_json": "{}", "policy_json": "{}",
            "created_at": "2026-01-01T00:00:00+00:00", "validation": {}, "policy": {},
        }
        self.service.list_component_review_decisions.return_value = [review]
        self.assertEqual(
            self._json("/api/catalog/components/cmp-1/reviews"), {"items": [review]}
        )

        record = {
            "id": "release-1", "component_id": "cmp-1", "revision_id": "rev-1",
            "release_label": "v1", "manifest_hash": "a", "released_by": USER.email,
            "approval_decision_id": "review-1", "validation_json": "{}", "policy_json": "{}",
            "created_at": "2026-01-01T00:00:00+00:00", "validation": {}, "policy": {},
        }
        self.service.list_component_release_records.return_value = [record]
        self.assertEqual(
            self._json("/api/catalog/components/cmp-1/releases"), {"items": [record]}
        )

    def test_validation_reads(self) -> None:
        run = {**_validation_run(), "findings": []}
        component_validation = {"summary": _validation_summary(), "runs": [run]}
        self.service.get_component_validation.return_value = component_validation
        self.assertEqual(
            self._json("/api/catalog/components/cmp-1/validation"), component_validation
        )

        self.service.get_validation_run.return_value = run
        self.assertEqual(self._json("/api/catalog/validation/runs/run-1"), run)

    # -- import sessions and jobs ----------------------------------------

    def test_import_session_reads(self) -> None:
        self.service.get_project_import_session.return_value = _session()
        self.assertEqual(
            self._json("/api/catalog/import-sessions/session-1"), _session()
        )

        self.service.list_project_import_sessions.return_value = [_session()]
        self.assertEqual(
            self._json("/api/catalog/import-sessions"), {"items": [_session()]}
        )

        self.service.list_project_import_proposals.return_value = [_proposal()]
        self.assertEqual(
            self._json("/api/catalog/import-sessions/session-1/proposals"),
            {"items": [_proposal()]},
        )

    def test_job_reads_keep_events_and_spread_result(self) -> None:
        events = [{
            "id": 1, "event_type": "queued", "stage": "", "percent": None,
            "details": {}, "created_at": "2026-01-01T00:00:00+00:00",
        }]
        jobs = MagicMock()
        jobs.events.return_value = events
        jobs.get.return_value = _job()
        with patch.object(catalog_admin, "catalog_jobs", jobs):
            self.assertEqual(
                self._json("/api/catalog/jobs/job-1"), {**_job(), "events": events}
            )
            self.assertEqual(self._json("/api/catalog/validation/jobs/job-1"), _job())
            self.assertEqual(self._json("/api/catalog/previews/jobs/job-1"), _job())
            self.assertEqual(self._json("/api/catalog/metadata/jobs/job-1"), _job())

    # -- metadata ---------------------------------------------------------

    def test_metadata_reads(self) -> None:
        pages = {"items": [_component()], "total": 1, "page": 1, "page_size": 100, "pages": 1}
        self.service.list_metadata_fields.return_value = [_field()]
        self.assertEqual(
            self._json("/api/catalog/metadata/fields"),
            {"schema": "prism.component_metadata_a1", "items": [_field()]},
        )

        self.service.get_metadata_grid_preferences.return_value = {}
        self.assertEqual(self._json("/api/catalog/metadata/grid-preferences"), {})
        preferences = {"visible": ["value"], "order": ["value"], "widths": {"value": 120}, "pinned": []}
        self.service.get_metadata_grid_preferences.return_value = preferences
        self.assertEqual(self._json("/api/catalog/metadata/grid-preferences"), preferences)

        grid = {**pages, "schema": "prism.component_metadata_a1", "fields": [_field()]}
        self.service.metadata_grid.return_value = grid
        self.assertEqual(self._json("/api/catalog/metadata/grid"), grid)

        self.service.get_metadata_batch.return_value = _batch()
        self.assertEqual(self._json("/api/catalog/metadata/batches/batch-1"), _batch())

    # -- assets, roots and mates -----------------------------------------

    def test_asset_and_import_source_reads(self) -> None:
        self.service.browse_library_assets.return_value = {
            "files": ["Device/R.kicad_sym"], "total": 1,
        }
        self.assertEqual(
            self._json("/api/catalog/assets/browse?asset_type=symbol"),
            {"files": ["Device/R.kicad_sym"], "total": 1},
        )

        item = {
            "id": "asset-1", "asset_type": "footprint", "name": "R.kicad_mod",
            "target_library": "Resistor_SMD", "target_name": "R_0603", "sha256": "sha",
            "size_bytes": 12, "usage_count": 3,
        }
        self.service.search_assets.return_value = [item]
        self.assertEqual(self._json("/api/catalog/assets/search"), {"items": [item]})

        roots = {"lib": Path("/srv/kicad/lib")}
        with patch.object(catalog_admin, "configured_import_roots", return_value=roots):
            self.assertEqual(
                self._json("/api/catalog/import-sources/folder-roots"),
                {"items": [{"name": "lib", "path_hint": "lib"}]},
            )

    def test_mates_and_models(self) -> None:
        mate = {
            "componentId": "cmp-2", "name": "Socket", "mpn": "S-1",
            "manufacturer": "Acme", "createdBy": USER.email,
            "createdAt": "2026-01-01T00:00:00+00:00",
        }
        self.service.part_relations.list_mates_with.return_value = [mate]
        self.assertEqual(
            self._json("/api/catalog/components/cmp-1/mates-with"), {"items": [mate]}
        )

        model = {
            "assetId": "asset-1", "name": "part.step", "stepSha256": "sha",
            "glb": {"key": "k", "converter": "geometer-1", "bounds": {"min": [0, 0, 0]},
                    "materials": 1, "sizeBytes": 12},
            "alignment": {"offsetMm": [0.0, 0.0, 0.0], "rotationDeg": [0.0, 0.0, 0.0],
                          "scale": 1.0, "updatedBy": None, "updatedAt": None},
        }
        self.service.part_relations.list_models.return_value = [model]
        self.assertEqual(self._json("/api/catalog/components/cmp-1/models"), {"items": [model]})

    # -- files, streams and redirects ------------------------------------

    def test_file_and_stream_routes_keep_media_types(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        svg = root / "preview.svg"
        svg.write_text("<svg></svg>", encoding="utf-8")
        self.service.catalog_preview_path.return_value = (svg, "image/svg+xml")
        response = self._get("/api/catalog/previews/preview-1")
        self.assertEqual(response.text, "<svg></svg>")
        self.assertTrue(response.headers["content-type"].startswith("image/svg+xml"))

        symbol = root / "R.kicad_sym"
        symbol.write_text("(kicad_symbol_lib)", encoding="utf-8")
        self.service.catalog_asset_source.return_value = (
            symbol, "application/x-kicad-symbol", "R.kicad_sym"
        )
        response = self._get("/api/catalog/assets/asset-1/content")
        self.assertEqual(response.text, "(kicad_symbol_lib)")
        self.assertEqual(response.headers["content-type"], "application/x-kicad-symbol")

        report = root / "report.json"
        report.write_text('{"ok": true}', encoding="utf-8")
        self.service.validation_report_path.return_value = report
        response = self._get("/api/catalog/validation/runs/run-1/report.json")
        self.assertEqual(json.loads(response.text), {"ok": True})
        self.assertEqual(response.headers["content-type"], "application/json")

        glb = root / "model.glb"
        glb.write_bytes(b"glTF")
        self.service.part_relations.model_glb_path.return_value = glb
        response = self._get(f"/api/catalog/models/{'a' * 64}.glb")
        self.assertEqual(response.content, b"glTF")
        self.assertEqual(response.headers["content-type"], "model/gltf-binary")

        self.service.part_relations.model_preview.return_value = "<svg/>"
        response = self._get("/api/catalog/components/cmp-1/models/asset-1/preview.svg")
        self.assertEqual(response.text, "<svg/>")
        self.assertTrue(response.headers["content-type"].startswith("image/svg+xml"))

    def test_csv_routes_keep_their_bodies(self) -> None:
        self.service.get_project_import_session.return_value = _session()
        self.service.list_project_import_proposals.return_value = [_proposal()]
        response = self._get("/api/catalog/import-sessions/session-1/proposals.csv")
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        self.assertIn("proposal_id,reference,value", response.text)
        self.assertIn("proposal-1,R1,10k", response.text)

        self.service.get_metadata_grid_preferences.return_value = {"visible": ["value"]}
        self.service.iter_metadata_csv.return_value = iter(["value\n", "10k\n"])
        response = self._get("/api/catalog/metadata/export.csv")
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        self.assertEqual(response.text, "\ufeffvalue\n10k\n")

        self.service.get_metadata_batch.return_value = _batch()
        response = self._get("/api/catalog/metadata/batches/batch-1/report.csv")
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        self.assertIn("component_id,mpn,status,error", response.text)

        self.service.export_inventory_csv.return_value = "mpn,quantity\n"
        response = self._get("/api/catalog/inventory/export.csv")
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        self.assertEqual(response.text, "\ufeffmpn,quantity\n")


class CatalogWriteTypedResponses(unittest.TestCase):
    """The catalog write routes' typed bodies are the bodies they returned before.

    Same harness as the read cases above: the route runs through FastAPI, a
    service stub returns the dictionary the catalog service builds, and the
    whole JSON body is compared, so a field dropped, added or coerced fails.
    """

    def setUp(self) -> None:
        self.addCleanup(patch.stopall)
        self.app = FastAPI()
        self.app.include_router(catalog_admin.router)
        self.app.include_router(catalog_part_relations.router)
        for dependency in (
            require_catalog_admin,
            require_catalog_browser,
            require_catalog_reader,
            require_catalog_writer,
        ):
            self.app.dependency_overrides[dependency] = lambda: USER
        self.service = MagicMock()
        self.artifact_store = MagicMock()
        self.jobs = MagicMock()
        self.jobs.enqueue.return_value = {"id": "job-1", "status": "queued"}

    def _call(self, method: str, path: str, *, status: int = 200, **kwargs: Any) -> Any:
        with patch.object(catalog_admin, "catalog_service", self.service), \
                patch.object(catalog_part_relations, "catalog_service", self.service), \
                patch.object(catalog_admin, "artifact_store", self.artifact_store), \
                patch.object(catalog_admin, "catalog_jobs", self.jobs), \
                patch.object(catalog_part_relations, "catalog_jobs", self.jobs):
            response = TestClient(self.app, raise_server_exceptions=False).request(method, path, **kwargs)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    @staticmethod
    def _etag(component_id: str = "cmp-1", revision_id: str = "rev-1") -> dict[str, str]:
        return {"If-Match": f'"component:{component_id}:{revision_id}"'}

    def _session_stub(self) -> None:
        self.service.get_project_import_session.return_value = _session()

    # -- import sessions and proposals ------------------------------------

    def test_project_import_session_start(self) -> None:
        self.service.create_project_import_session.return_value = _session()
        workspace = MagicMock()
        workspace.get_all_projects.return_value = [{"id": "prj-1"}]
        revisions = MagicMock()
        revisions.locate.return_value = MagicMock()
        revisions.resolve_commit.return_value = "abc"
        with patch.object(catalog_admin, "workspace", workspace), \
                patch.object(catalog_admin, "project_revision", revisions):
            body = self._call(
                "POST", "/api/catalog/import-sessions/projects", status=202,
                json={"scope": "project", "project_id": "prj-1", "source_revision": "abc"},
            )
        self.assertEqual(body, {**_session(), "job_id": "job-1"})

    def test_folder_snapshot_writes(self) -> None:
        self.service.get_project_import_session.return_value = _session()
        self.artifact_store.get_snapshot.return_value = {
            "created_by": USER.email, "status": "uploading", "display_name": "KiCad libraries",
        }
        self.artifact_store.create_snapshot.return_value = _snapshot_row()
        self.artifact_store.complete_snapshot.return_value = {**_snapshot_row(), "status": "ready"}
        self.artifact_store.put_stream.return_value = SimpleNamespace(sha256="sha", size_bytes=7)

        created = self._call("POST", "/api/catalog/import-snapshots/folders", status=201,
                             json={"display_name": "KiCad libraries"})
        self.assertEqual(
            created,
            {
                **_snapshot_row(),
                "created_at": _snapshot_row()["created_at"].isoformat(),
                "completed_at": None,
            },
        )

        uploaded = self._call(
            "POST", "/api/catalog/import-snapshots/folders/snap-1/files", status=201,
            data={"relative_path": "lib/R.kicad_sym"},
            files={"file": ("R.kicad_sym", b"(kicad_symbol_lib)", "application/octet-stream")},
        )
        self.assertEqual(uploaded, {"relative_path": "lib/R.kicad_sym", "sha256": "sha", "size_bytes": 7})

        self.service.create_project_import_session.return_value = _session()
        completed = self._call(
            "POST", "/api/catalog/import-snapshots/folders/snap-1/complete", status=202, json={}
        )
        self.assertEqual(completed, {**_session(), "job_id": "job-1"})

        with patch.object(catalog_admin, "resolve_server_import_path", return_value=Path("/srv/lib")):
            server = self._call(
                "POST", "/api/catalog/import-snapshots/folders/server", status=202,
                json={"root_name": "lib", "subpath": ""},
            )
        self.assertEqual(server, {**_session(), "job_id": "job-1"})

    def test_folder_discovery(self) -> None:
        self.artifact_store.get_snapshot.return_value = {
            "created_by": USER.email, "status": "uploading",
        }
        self.artifact_store.snapshot_files.return_value = []
        discovery = _discovery()
        self.service.match_component_identities.return_value = [_discovery_match()]
        with patch.object(catalog_admin, "discover_library", return_value=discovery):
            body = self._call(
                "POST", "/api/catalog/import-snapshots/folders/snap-1/discover",
                json={"files": [{"relative_path": "lib/R.kicad_sym", "size_bytes": 12}]},
            )
        component = {**discovery["components"][0], "existing_component": _discovery_match()}
        self.assertEqual(
            body,
            {**discovery, "components": [component], "existing_component_count": 1},
        )

    def test_proposal_writes(self) -> None:
        self._session_stub()
        self.service.get_project_import_proposal.return_value = _proposal()
        self.service.accept_project_import_proposal.return_value = {
            "proposal": _proposal(), "component": _component(),
        }
        accepted = self._call("POST", "/api/catalog/import-proposals/proposal-1/accept", json={})
        self.assertEqual(accepted, {"proposal": _proposal(), "component": _component()})

        self.service.reject_project_import_proposal.return_value = {**_proposal(), "status": "rejected"}
        rejected = self._call("POST", "/api/catalog/import-proposals/proposal-1/reject")
        self.assertEqual(rejected, {**_proposal(), "status": "rejected"})

        self.service.save_project_import_drafts.return_value = 1
        drafts = self._call(
            "PUT", "/api/catalog/import-sessions/session-1/proposals/drafts",
            json={"drafts": {"proposal-1": {"metadata_overrides": {"value": "22k"}}}},
        )
        self.assertEqual(drafts, {"saved": 1})

        csv_body = "proposal_id,value\nproposal-1,22k\n"
        self.service.list_project_import_proposals.return_value = [_proposal()]
        imported = self._call(
            "POST", "/api/catalog/import-sessions/session-1/proposals.csv",
            files={"file": ("proposals.csv", csv_body.encode(), "text/csv")},
        )
        self.assertEqual(imported, {"saved": 1, "skipped_unknown_rows": 0})

    def test_bulk_accept_reports_each_row(self) -> None:
        self._session_stub()
        self.service.get_project_import_proposal.side_effect = [_proposal(), None]
        self.service.accept_project_import_proposal.return_value = {
            "proposal": _proposal(), "component": {"id": "cmp-1"},
        }
        body = self._call(
            "POST", "/api/catalog/import-sessions/session-1/proposals/bulk-accept",
            json={"items": [{"proposal_id": "proposal-1"}, {"proposal_id": "missing"}]},
        )
        self.assertEqual(
            body,
            {
                "accepted": 1,
                "failed": 1,
                "results": [
                    {"proposal_id": "proposal-1", "status": "accepted", "component_id": "cmp-1"},
                    {"proposal_id": "missing", "status": "failed", "error": "Import proposal not found"},
                ],
            },
        )

    # -- component writes -------------------------------------------------

    def test_component_writes_return_the_component(self) -> None:
        self.service.create_manual_component.return_value = _component()
        created = self._call(
            "POST", "/api/catalog/components",
            json={"value": "10k", "description": "", "datasheet": "",
                  "manufacturer": "Yageo", "manufacturer_part_number": "RC0603FR-0710KL"},
        )
        self.assertEqual(created, _component())

        self.service.update_component_metadata.return_value = _component()
        updated = self._call(
            "PATCH", "/api/catalog/components/cmp-1", headers=self._etag(), json={"value": "22k"}
        )
        self.assertEqual(updated, _component())

        self.service.create_representation.return_value = _component()
        self.assertEqual(
            self._call("POST", "/api/catalog/components/cmp-1/representations", status=201,
                       headers=self._etag(), json={"label": "Second"}),
            _component(),
        )
        self.service.update_representation.return_value = _component()
        self.assertEqual(
            self._call("PATCH", "/api/catalog/components/cmp-1/representations/rep-1",
                       headers=self._etag(), json={"label": "Renamed"}),
            _component(),
        )
        self.service.delete_representation.return_value = _component()
        self.assertEqual(
            self._call("DELETE", "/api/catalog/components/cmp-1/representations/rep-1",
                       headers=self._etag()),
            _component(),
        )

        self.service.get_component.return_value = {**_component(), "workflow_stage": "open"}
        self.service.set_release_status.return_value = _component()
        with patch.object(catalog_admin, "_can_transition_workflow", return_value=True):
            released = self._call(
                "POST", "/api/catalog/components/cmp-1/release",
                json={"workflow_stage": "released", "review_note": "ok"},
            )
        self.assertEqual(released, _component())

        self.service.regenerate_component_previews.return_value = _component()
        self.assertEqual(
            self._call("POST", "/api/catalog/components/cmp-1/previews/regenerate"), _component()
        )

        self.service.delete_component.return_value = True
        self.assertEqual(self._call("DELETE", "/api/catalog/components/cmp-1"), {"ok": True})

    def test_asset_writes(self) -> None:
        upload = {"files": {"file": ("asset.kicad_sym", b"data", "application/octet-stream")}}
        self.service.import_symbol_library.return_value = {
            "mode": "selection_required", "discovered_symbols": ["A", "B"],
        }
        self.assertEqual(
            self._call("POST", "/api/catalog/components/cmp-1/symbol-import", **upload),
            {"mode": "selection_required", "discovered_symbols": ["A", "B"]},
        )
        self.service.import_symbol_library.return_value = {
            "mode": "imported", "discovered_symbols": ["A"], "selected_symbol": "A",
            "component": _component(),
        }
        self.assertEqual(
            self._call("POST", "/api/catalog/components/cmp-1/symbol-import", **upload),
            {"mode": "imported", "discovered_symbols": ["A"], "selected_symbol": "A",
             "component": _component()},
        )

        self.service.import_footprint.return_value = {
            "mode": "imported", "discovered_footprints": ["R_0603"], "selected_footprint": "R_0603",
            "component": _component(),
        }
        self.assertEqual(
            self._call(
                "POST", "/api/catalog/components/cmp-1/footprint-import",
                files={"file": ("R.kicad_mod", b"data", "application/octet-stream")},
            ),
            {"mode": "imported", "discovered_footprints": ["R_0603"],
             "selected_footprint": "R_0603", "component": _component()},
        )

        self.service.attach_auxiliary_asset.return_value = {"component": _component()}
        self.assertEqual(
            self._call(
                "POST", "/api/catalog/components/cmp-1/assets/3dmodel",
                files={"file": ("part.step", b"data", "application/octet-stream")},
            ),
            {"component": _component()},
        )

        self.service.link_library_asset.return_value = {"component": _component()}
        self.assertEqual(
            self._call(
                "POST", "/api/catalog/components/cmp-1/assets/footprint/link",
                json={"file_path": "Resistor_SMD.pretty/R_0603.kicad_mod"},
            ),
            {"component": _component()},
        )

        self.service.detach_asset.return_value = {"component": _component()}
        self.assertEqual(
            self._call("DELETE", "/api/catalog/components/cmp-1/assets/3dmodel"),
            {"component": _component()},
        )
        self.service.detach_asset_by_id.return_value = {"component": _component()}
        self.assertEqual(
            self._call("DELETE", "/api/catalog/components/cmp-1/assets/id/asset-1",
                       headers=self._etag()),
            {"component": _component()},
        )

    # -- metadata ---------------------------------------------------------

    def test_metadata_field_writes(self) -> None:
        self.service.create_metadata_field.return_value = _field()
        self.assertEqual(
            self._call("POST", "/api/catalog/metadata/fields", status=201,
                       json={"key": "tolerance", "label": "Tolerance"}),
            _field(),
        )
        self.service.update_metadata_field.return_value = _field()
        self.assertEqual(
            self._call("PATCH", "/api/catalog/metadata/fields/builtin:value",
                       json={"label": "Value"}),
            _field(),
        )
        archived = {**_field(), "archived": True}
        self.service.set_metadata_field_archived.return_value = archived
        self.assertEqual(
            self._call("POST", "/api/catalog/metadata/fields/builtin:value/archive"), archived
        )
        self.assertEqual(
            self._call("POST", "/api/catalog/metadata/fields/builtin:value/restore"), archived
        )

    def test_metadata_batch_writes(self) -> None:
        preferences = {"visible": ["value"], "order": ["value"], "widths": {"value": 120}, "pinned": []}
        self.service.save_metadata_grid_preferences.return_value = preferences
        self.assertEqual(
            self._call("PUT", "/api/catalog/metadata/grid-preferences", json=preferences),
            preferences,
        )

        self.service.stage_metadata_batch.return_value = _batch()
        self.assertEqual(
            self._call("POST", "/api/catalog/metadata/batches", status=201,
                       json={"items": [{"component_id": "cmp-1", "expected_revision_id": "rev-1",
                                        "patch": {"value": "22k"}}]}),
            _batch(),
        )

        self.service.get_metadata_batch.return_value = _batch()
        self.service.approve_metadata_batch_fields.return_value = _batch()
        self.assertEqual(
            self._call("POST", "/api/catalog/metadata/batches/batch-1/approve-fields"), _batch()
        )

        self.assertEqual(
            self._call("POST", "/api/catalog/metadata/batches/batch-1/apply", status=202,
                       json={"item_ids": ["item-1"]}),
            {"batch_id": "batch-1", "job_id": "job-1", "status": "queued"},
        )

        preview = {**_batch(), "source_rows": 4, "skipped_unchanged_rows": 3}
        self.service.preview_metadata_csv.return_value = preview
        self.assertEqual(
            self._call(
                "POST", "/api/catalog/metadata/import-csv/preview", status=201,
                files={"file": ("metadata.csv", b"mpn,value\nRC0603FR-0710KL,22k\n", "text/csv")},
            ),
            preview,
        )

    # -- queued work, exports and relations --------------------------------

    def test_queued_writes(self) -> None:
        queued = {"job_id": "job-1", "status": "queued"}
        self.assertEqual(
            self._call("POST", "/api/catalog/components/cmp-1/validate"), queued
        )
        self.assertEqual(self._call("POST", "/api/catalog/validation/run"), queued)
        self.assertEqual(self._call("POST", "/api/catalog/previews/generate-missing"), queued)
        self.assertEqual(
            self._call("POST", "/api/catalog/artifacts/maintenance", status=202), queued
        )

    def test_export_and_inventory_writes(self) -> None:
        bundle = {
            "export_root": "/store/exports/dbl", "component_count": 1, "category_count": 1,
            "sqlite_path": "/store/exports/dbl/Prism.sqlite",
            "linux_dbl": "/store/exports/dbl/prism-dbl-linux.kicad_dbl",
            "windows_dbl": "/store/exports/dbl/prism-dbl-windows.kicad_dbl",
            "sym_lib_table": "/store/exports/dbl/sym-lib-table",
            "fp_lib_table": "/store/exports/dbl/fp-lib-table",
            "model_count": 1,
            "missing_models": [{
                "component_id": "cmp-1", "revision_id": "rev-1",
                "asset_id": "asset-1", "reason": "file_missing",
            }],
        }
        self.service.export_kicad_dbl_bundle.return_value = bundle
        self.assertEqual(self._call("POST", "/api/catalog/exports/kicad-dbl"), bundle)

        inventory = {"updated": 1, "not_found": 1, "errors": ["Row 3: unknown unit"]}
        self.service.import_inventory_csv.return_value = inventory
        self.assertEqual(
            self._call(
                "POST", "/api/catalog/inventory/sync-csv",
                files={"file": ("inventory.csv", b"mpn,quantity\nRC0603FR-0710KL,10\n", "text/csv")},
            ),
            inventory,
        )

    def test_part_relation_writes(self) -> None:
        mate = {
            "componentId": "cmp-2", "name": "Socket", "mpn": "S-1",
            "manufacturer": "Acme", "createdBy": USER.email,
            "createdAt": "2026-01-01T00:00:00+00:00",
        }
        self.service.part_relations.set_mate.return_value = [mate]
        self.assertEqual(
            self._call("POST", "/api/catalog/components/cmp-1/mates-with",
                       json={"componentId": "cmp-2"}),
            {"items": [mate]},
        )
        self.assertEqual(
            self._call("DELETE", "/api/catalog/components/cmp-1/mates-with/cmp-2"),
            {"items": [mate]},
        )

        self.service.part_relations.list_models.return_value = []
        converted = self._call("POST", "/api/catalog/components/cmp-1/models/convert")
        self.assertEqual(converted, {"jobId": "job-1", "status": "queued"})

        model = {
            "assetId": "asset-1", "name": "part.step", "stepSha256": "sha",
            "glb": None,
            "alignment": {"offsetMm": [0.0, 0.0, 0.0], "rotationDeg": [0.0, 0.0, 0.0],
                          "scale": 1.0, "updatedBy": USER.email,
                          "updatedAt": "2026-01-01T00:00:00+00:00"},
        }
        self.service.part_relations.set_model_alignment.return_value = [model]
        self.assertEqual(
            self._call(
                "PUT", "/api/catalog/components/cmp-1/models/asset-1/alignment",
                json={"offsetMm": [0.0, 0.0, 0.0], "rotationDeg": [0.0, 0.0, 0.0], "scale": 1.0},
            ),
            {"items": [model]},
        )


if __name__ == "__main__":
    unittest.main()
