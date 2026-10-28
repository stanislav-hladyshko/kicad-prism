"""The typed write and job routes return the bodies they returned before.

MCP-06f adds a ``response_model`` to routes that already had a shape; ADR
scripting-and-mcp §3.2 forbids changing that shape. Each route is called
through FastAPI with its service stubbed to return the exact dictionary the
service builds, and the whole JSON body is compared with a literal. A key the
model drops, a key it invents, or a value it coerces fails here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import (  # noqa: E402
    design_bundles,
    folders,
    jobs as jobs_api,
    oauth,
    project_import_followups,
    project_semantic_index,
    projects,
)
from app.core.errors import install_error_handlers  # noqa: E402
from app.core.security import (  # noqa: E402
    AuthenticatedUser,
    require_admin,
    require_designer,
    require_viewer,
)
from app.services import (  # noqa: E402
    derived_assets,
    file_service,
    git_access_service,
    path_config_service,
    project_deletion,
    project_import_service,
    project_service,
)
from app.services.workspace_service import workspace  # noqa: E402

USER = AuthenticatedUser(email="designer@example.com", name="Designer", role="designer")


def _project() -> SimpleNamespace:
    return SimpleNamespace(
        id="prj-1",
        name="top",
        display_name="Top",
        description="A board",
        path="/tmp/prj",
        project_file="top.kicad_pro",
    )


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(projects.router, prefix="/api/projects")
    app.include_router(project_semantic_index.router, prefix="/api/projects")
    app.include_router(project_import_followups.router, prefix="/api/projects")
    app.include_router(design_bundles.router)
    app.include_router(folders.router, prefix="/api/folders")
    app.include_router(jobs_api.router, prefix="/api/jobs")
    app.include_router(oauth.router)
    for dependency in (require_viewer, require_designer, require_admin):
        app.dependency_overrides[dependency] = lambda: USER
    return app


class ProjectTypedResponses(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(patch.stopall)
        # Unhandled errors (a response breaking its model in strict mode) raise
        # here with their traceback rather than turning into an empty 500.
        self.client = TestClient(_app(), raise_server_exceptions=True)

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        self.assertLess(response.status_code, 300, response.text)
        return response.json()

    # -- import and sync ---------------------------------------------------

    def test_analyze_and_import_queue_a_job(self) -> None:
        with patch.object(project_import_service, "start_analyze_job", return_value="job-1"):
            self.assertEqual(
                self._json("POST", "/api/projects/analyze", json={"url": "https://example.com/r.git"}),
                {"job_id": "job-1", "status": "started"},
            )
        with patch.object(project_import_service, "start_import_job", return_value="job-2"):
            self.assertEqual(
                self._json(
                    "POST",
                    "/api/projects/import",
                    json={"url": "https://example.com/r.git", "import_type": "type1"},
                ),
                {"job_id": "job-2", "status": "started"},
            )

    def test_access_help_names_the_key_and_the_forge(self) -> None:
        guidance = git_access_service.ForgeGuidance(
            forge="github",
            deploy_key_url="https://github.com/o/r/settings/keys",
            account_key_url=None,
            instructions="Add the key to the repository.",
        )
        key = git_access_service.KeyInfo(
            exists=True, public_key="ssh-ed25519 AAAA prism", fingerprint="SHA256:abc"
        )
        with patch.object(projects, "parse_remote_url", return_value=SimpleNamespace(host="github.com")), \
                patch.object(git_access_service, "describe_key", return_value=key), \
                patch.object(git_access_service, "guidance_for", return_value=guidance), \
                patch.object(git_access_service, "is_host_trusted", return_value=True):
            body = self._json(
                "POST",
                "/api/projects/access-help",
                json={"url": "git@github.com:o/r.git"},
            )
        self.assertEqual(
            body,
            {
                "forge": "github",
                "deploy_key_url": "https://github.com/o/r/settings/keys",
                "account_key_url": None,
                "instructions": "Add the key to the repository.",
                "public_key": "ssh-ed25519 AAAA prism",
                "fingerprint": "SHA256:abc",
                "key_exists": True,
                "host": "github.com",
                "host_trusted": True,
            },
        )

    def test_sync_and_workflow_report_their_job(self) -> None:
        with patch.object(projects, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(project_import_service, "start_sync_job", return_value="job-sync"):
            self.assertEqual(
                self._json("POST", "/api/projects/prj-1/sync"),
                {"job_id": "job-sync", "status": "started", "message": "Sync queued"},
            )
        with patch.object(projects, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(project_service, "start_workflow_job", return_value="job-wf"):
            self.assertEqual(
                self._json("POST", "/api/projects/prj-1/workflows", json={"type": "design"}),
                {"job_id": "job-wf"},
            )

    # -- thumbnails --------------------------------------------------------

    def test_regenerate_thumbnails_queue_one_or_many(self) -> None:
        with patch.object(projects, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(project_import_service, "start_thumbnail_job", return_value="job-t1"):
            self.assertEqual(
                self._json("POST", "/api/projects/prj-1/thumbnail/regenerate"),
                {"status": "queued", "job_id": "job-t1", "message": "Rendering the board thumbnail"},
            )
        with patch.object(projects, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(project_import_service, "start_thumbnail_jobs", return_value=["job-t1", "job-t2"]):
            self.assertEqual(
                self._json(
                    "POST",
                    "/api/projects/thumbnails/regenerate",
                    json={"project_ids": ["prj-1", "prj-2"]},
                ),
                {
                    "status": "queued",
                    "job_ids": ["job-t1", "job-t2"],
                    "count": 2,
                    "message": "Rendering 2 board thumbnails",
                },
            )

    def test_upload_and_clear_thumbnail(self) -> None:
        with patch.object(projects, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(derived_assets, "store_uploaded_thumbnail", return_value=(Path("thumb.png"), "digest", 4)), \
                patch.object(workspace, "update_project", return_value=True):
            self.assertEqual(
                self._json(
                    "PUT",
                    "/api/projects/prj-1/thumbnail",
                    files={"file": ("thumb.png", b"png!", "image/png")},
                ),
                {"status": "success", "thumbnail_source": "custom", "message": "Thumbnail updated"},
            )

    def test_clear_thumbnail_queues_the_fallback_render(self) -> None:
        # Nothing has been rendered yet, so the render the user is falling back
        # to is queued and its id is reported.
        with patch.object(projects, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(derived_assets, "discard_thumbnail", return_value=None), \
                patch.object(project_import_service, "refresh_project_assets", return_value={}), \
                patch.object(project_import_service, "start_thumbnail_job", return_value="job-t3"):
            self.assertEqual(
                self._json("DELETE", "/api/projects/prj-1/thumbnail"),
                {
                    "status": "success",
                    "thumbnail_source": "generated",
                    "job_id": "job-t3",
                    "message": "Rendering the board thumbnail",
                },
            )

    def test_clear_thumbnail_keeps_an_existing_render(self) -> None:
        with patch.object(projects, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(derived_assets, "discard_thumbnail", return_value=None), \
                patch.object(
                    project_import_service,
                    "refresh_project_assets",
                    return_value={"thumbnail_source": "generated"},
                ):
            self.assertEqual(
                self._json("DELETE", "/api/projects/prj-1/thumbnail"),
                {
                    "status": "success",
                    "thumbnail_source": "generated",
                    "job_id": None,
                    "message": "Using the rendered board image",
                },
            )

    # -- deletion ----------------------------------------------------------

    def test_deletion_reports_files_and_an_optional_warning(self) -> None:
        clean = project_deletion.ProjectDeletion(removed_files=True, repository_deleted=True)
        with patch.object(projects, "authorized_project", return_value=None), \
                patch.object(project_deletion, "delete_project", return_value=clean):
            self.assertEqual(
                self._json("DELETE", "/api/projects/prj-1"),
                {
                    "message": "Project deleted successfully",
                    "removed_files": True,
                    "repository_deleted": True,
                },
            )

        warned = project_deletion.ProjectDeletion(
            removed_files=False,
            repository_deleted=True,
            warnings=["comments could not be deleted"],
        )
        with patch.object(projects, "authorized_project", return_value=None), \
                patch.object(project_deletion, "delete_project", return_value=warned):
            self.assertEqual(
                self._json("DELETE", "/api/projects/prj-1"),
                {
                    "message": "Project deleted successfully",
                    "removed_files": False,
                    "warning": "comments could not be deleted",
                    "repository_deleted": True,
                },
            )

    # -- path configuration ------------------------------------------------

    def test_detect_paths_reports_detection_and_validation(self) -> None:
        detected = path_config_service.PathConfig(schematic="main.kicad_sch")
        validation = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "resolved": {"schematic": "/tmp/prj/main.kicad_sch"},
        }
        with patch.object(projects, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(path_config_service, "detect_paths", return_value=detected), \
                patch.object(path_config_service, "validate_config", return_value=validation):
            body = self._json("POST", "/api/projects/prj-1/detect-paths")
        self.assertEqual(
            body,
            {
                "detected": {
                    "schematic": "main.kicad_sch",
                    "pcb": None,
                    "subsheets": None,
                    "designOutputs": None,
                    "manufacturingOutputs": None,
                    "documentation": None,
                    "thumbnail": None,
                    "readme": None,
                    "jobset": None,
                    "project_name": None,
                    "description": None,
                    "workflows": None,
                    "portfolio": None,
                },
                "validation": validation,
            },
        )

    def test_saving_a_config_returns_it_resolved_and_validated(self) -> None:
        resolved = path_config_service.ResolvedPaths(
            project_root="/tmp/prj", schematic="/tmp/prj/main.kicad_sch"
        )
        validation = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "resolved": {"schematic": "/tmp/prj/main.kicad_sch"},
        }
        with patch.object(projects, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(path_config_service, "validate_config", return_value=validation), \
                patch.object(project_service, "save_path_config_for", return_value=None), \
                patch.object(path_config_service, "clear_config_cache", return_value=None), \
                patch.object(file_service, "invalidate_file_listing_cache", return_value=None), \
                patch.object(project_service, "resolved_paths_for", return_value=resolved):
            body = self._json(
                "PUT",
                "/api/projects/prj-1/config",
                json={"schematic": "main.kicad_sch", "project_name": "Top"},
            )
        self.assertEqual(
            body,
            {
                "config": {
                    "schematic": "main.kicad_sch",
                    "pcb": None,
                    "subsheets": None,
                    "designOutputs": None,
                    "manufacturingOutputs": None,
                    "documentation": None,
                    "thumbnail": None,
                    "readme": None,
                    "jobset": None,
                    "project_name": "Top",
                    "description": None,
                    "workflows": None,
                    "portfolio": None,
                },
                "resolved": {
                    "project_root": "/tmp/prj",
                    "schematic": "/tmp/prj/main.kicad_sch",
                    "pcb": None,
                    "subsheets_dir": None,
                    "design_outputs_dir": None,
                    "manufacturing_outputs_dir": None,
                    "documentation_dir": None,
                    "thumbnail_dir": None,
                    "readme_path": None,
                    "jobset_path": None,
                },
                "validation": validation,
            },
        )

    def test_name_and_description_updates(self) -> None:
        with patch.object(projects, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(project_service, "path_config_for", return_value=path_config_service.PathConfig()), \
                patch.object(project_service, "save_path_config_for", return_value=None), \
                patch.object(workspace, "update_project", return_value=True):
            self.assertEqual(
                self._json("PUT", "/api/projects/prj-1/name", json={"display_name": "Main"}),
                {"display_name": "Main", "message": "Project name updated successfully"},
            )
        with patch.object(projects, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(workspace, "update_project", return_value=True), \
                patch.object(project_service, "path_config_for", return_value=path_config_service.PathConfig()), \
                patch.object(project_service, "save_path_config_for", return_value=None):
            self.assertEqual(
                self._json("PUT", "/api/projects/prj-1/description", json={"description": "A board"}),
                {
                    "description": "A board",
                    "message": "Project description updated successfully",
                },
            )

    # -- jobs, bundles and follow-ups -------------------------------------

    def test_cancel_reports_the_status_the_service_chose(self) -> None:
        with patch.object(jobs_api.jobs, "get", return_value={"job_id": "job-1", "status": "queued"}), \
                patch.object(jobs_api, "_authorize_cancel", return_value=None), \
                patch.object(jobs_api.jobs, "request_cancel", return_value="cancelled"):
            response = self.client.post("/api/jobs/job-1/cancel")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"job_id": "job-1", "status": "cancelled"})

    def test_semantic_index_generate(self) -> None:
        with patch.object(project_semantic_index, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(
                    project_semantic_index.semantic_index_service,
                    "start_generate_job",
                    return_value={"job_id": "job-1", "status": "started"},
                ):
            self.assertEqual(
                self._json("POST", "/api/projects/prj-1/semantic-index/generate", json={"force": True}),
                {"job_id": "job-1", "status": "started"},
            )

    def test_import_follow_up_retry_keeps_each_outcome(self) -> None:
        outcomes = [
            {"project_id": "prj-1", "operation": "metadata", "status": "queued", "job_id": "job-m"},
            {"project_id": "prj-1", "operation": "thumbnail", "status": "skipped", "error": "Project not found"},
        ]
        with patch.object(project_import_followups, "get_project_for_role_or_404", return_value=_project()), \
                patch(
                    "app.services.project_import_followups.retry_import_follow_ups",
                    return_value=outcomes,
                ):
            body = self._json("POST", "/api/projects/prj-1/import-follow-ups/retry")
        self.assertEqual(
            body,
            {
                "status": "partial",
                "project_id": "prj-1",
                "follow_ups": outcomes,
                "job_ids": ["job-m"],
                "message": "Some import follow-ups could not be queued",
            },
        )

    def test_design_bundle_regenerate(self) -> None:
        regenerated: Any = MagicMock()
        regenerated.to_dict.return_value = {
            "scope": "commit",
            "commit": None,
            "removedBundles": 2,
            "removedAliases": 1,
            "jobId": "job-b",
        }
        with patch.object(design_bundles, "get_project_for_role_or_404", return_value=_project()), \
                patch.object(design_bundles.regenerate, "regenerate", return_value=regenerated):
            self.assertEqual(
                self._json("POST", "/api/projects/prj-1/design-bundles/regenerate", json={}),
                {
                    "scope": "commit",
                    "commit": None,
                    "removedBundles": 2,
                    "removedAliases": 1,
                    "jobId": "job-b",
                },
            )

    # -- folders -----------------------------------------------------------

    def test_folder_writes_return_the_folder_row(self) -> None:
        row = {
            "id": "fld-1",
            "name": "Boards",
            "parent_id": None,
            "visibility_mode": None,
            "allowed_roles": [],
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        with patch.object(workspace, "create_folder", return_value=row):
            self.assertEqual(
                self._json("POST", "/api/folders/", json={"name": " Boards "}),
                row,
            )
        with patch.object(workspace, "update_folder", return_value=row):
            self.assertEqual(
                self._json("PATCH", "/api/folders/fld-1", json={"name": "Boards"}),
                row,
            )

    def test_folder_delete_and_moves(self) -> None:
        with patch.object(workspace, "delete_folder", return_value=True):
            self.assertEqual(
                self._json("DELETE", "/api/folders/fld-1"),
                {"message": "Folder deleted successfully"},
            )
        with patch.object(workspace, "move_project_to_folder", return_value=True):
            self.assertEqual(
                self._json("POST", "/api/folders/projects/prj-1/move", json={"folder_id": "fld-1"}),
                {"message": "Project moved successfully"},
            )
        with patch.object(workspace, "move_projects_to_folder", return_value=2):
            self.assertEqual(
                self._json(
                    "POST",
                    "/api/folders/projects/move",
                    json={"project_ids": ["prj-1", "prj-2"], "folder_id": "fld-1"},
                ),
                {"message": "2 projects moved successfully", "moved": 2},
            )

    # -- oauth -------------------------------------------------------------

    def test_service_token_body(self) -> None:
        payload = {
            "access_token": "token-value",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "api:read",
        }
        with patch.object(oauth.service_client_service, "issue_client_credentials_token", return_value=payload), \
                patch.object(oauth.rate_limit_service, "enforce", return_value=None), \
                patch.object(oauth.rate_limit_service, "clear", return_value=None):
            response = self.client.post(
                "/api/oauth/token",
                data={"grant_type": "client_credentials", "client_id": "ci", "client_secret": "s3cret"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)


if __name__ == "__main__":
    unittest.main()
