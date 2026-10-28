"""Response models for the Release Studio routes (ADR scripting-and-mcp §3.2).

These describe what ``app/api/release_studio.py`` already answers. Records read
straight out of PostgreSQL keep the column names and the ``isoformat()``
spelling FastAPI's encoder has always given their timestamps; ``extra="allow"``
keeps any column a later migration adds on the wire.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Optional

from pydantic import BaseModel, ConfigDict, PlainSerializer


class WireModel(BaseModel):
    """A response model that names the fields it knows and keeps the rest."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


#: psycopg hands back tz-aware datetimes, which the JSON response has always
#: rendered with ``isoformat()``. Pydantic's own JSON mode writes ``Z`` for UTC,
#: so an explicitly serialized field keeps the existing bytes.
Timestamp = Annotated[datetime, PlainSerializer(lambda value: value.isoformat(), return_type=str)]


# ---------------------------------------------------------------------------
# Jobs and candidates
# ---------------------------------------------------------------------------


class JobRecord(WireModel):
    """One ``ws_jobs`` row as ``JobService.enqueue`` answers it."""

    job_id: str
    kind: str
    worker_pool: str
    status: str
    message: str
    percent: float
    payload: dict[str, Any]
    priority: int
    artifact_key: Optional[str]
    project_id: Optional[str]
    repository_id: Optional[str]
    requested_by: str
    max_attempts: int
    resource_requirements: dict[str, Any]
    lock_requirements: list[Any]
    stage: str
    result_path: Optional[str]
    result_digest: Optional[str]
    result_metadata: dict[str, Any]
    log_path: Optional[str]
    error_code: str
    error_message: str
    fence: int
    lease_owner: str
    lease_expires_at: Optional[str]
    heartbeat_at: Optional[str]
    available_at: Optional[str]
    attempt: int
    cancel_requested_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    deduplicated: bool
    cache_hit: bool


class CandidateRecord(WireModel):
    """One ``ws_release_candidates`` row."""

    id: str
    project_id: str
    repository_id: str
    config_key: str
    commit_sha: str
    variant: str
    technical_config_digest: str
    input_closure_digest: str
    toolchain_digest: str
    generator_build: str
    build_key: str
    status: str
    hermetic: bool
    non_hermetic_reasons: list[Any]
    authored_overrides: dict[str, Any]
    policy_snapshot_captured: bool
    policy_document: Optional[dict[str, Any]]
    configuration_snapshot_captured: bool
    configuration_document: Optional[dict[str, Any]]
    self_contained: Optional[bool]
    created_by: str
    created_at: Timestamp
    updated_at: Timestamp


class BuildRecord(WireModel):
    """One ``ws_release_builds`` row."""

    id: str
    candidate_id: str
    job_id: Optional[str]
    fence: int
    attempt: int
    status: str
    manifest_digest: str
    dossier_digest: str
    dossier_artifact_id: Optional[str]
    evidence_artifact_id: Optional[str]
    toolchain: dict[str, Any]
    timings: Any
    warnings: list[Any]
    error_code: str
    error_message: str
    started_at: Optional[Timestamp]
    completed_at: Optional[Timestamp]
    created_at: Timestamp


class ListedBuild(BuildRecord):
    """A build as ``store.list_builds`` joins it to its publish record."""

    published: bool
    published_tag: str


class CandidateWithBuilds(CandidateRecord):
    builds: list[ListedBuild]
    latest_build: Optional[ListedBuild]


class CandidateList(WireModel):
    candidates: list[CandidateWithBuilds]


class CandidateCreated(WireModel):
    job: JobRecord


# ---------------------------------------------------------------------------
# Sources, tags, vendor profiles
# ---------------------------------------------------------------------------


class SourceDiscovery(WireModel):
    boards: list[str]
    schematics: list[str]
    board: str
    schematic: str
    project: str
    variants: list[str]
    bom_presets: list[str]
    default_bom_preset: str
    variant: str


class IpcOption(WireModel):
    value: str
    label: str


class IpcOptions(WireModel):
    manufacturing: list[IpcOption]
    assembly: list[IpcOption]
    solder_mask_colour: list[IpcOption]
    silkscreen_colour: list[IpcOption]
    via_treatment: list[IpcOption]


class ForgeSummary(WireModel):
    kind: str
    name: str
    host: str
    owner_repo: str
    token_configured: bool
    token_hint: str


class SourceResponse(WireModel):
    source: SourceDiscovery
    ipc: IpcOptions
    forge: ForgeSummary


class SourceDefaults(WireModel):
    board: str
    schematic: str
    variant: str
    bom_preset: str


class SourceDefaultsResponse(WireModel):
    defaults: SourceDefaults


class ReleaseTagCheck(WireModel):
    tag: str
    exists: bool


class VendorProfileOut(WireModel):
    id: str
    title: str
    pack_filename: str
    description: str
    required_pack_artifacts: list[str]


class VendorProfileList(WireModel):
    profiles: list[VendorProfileOut]


# ---------------------------------------------------------------------------
# Build detail, approvals and publishing
# ---------------------------------------------------------------------------


class MemberRecord(WireModel):
    id: str
    build_id: str
    path: str
    member_kind: str
    media_type: str
    size_bytes: int
    released_digest: str
    source_raw_digest: str
    canonicalizer: str
    domains: list[str]
    created_at: Timestamp


class EvidenceRecord(WireModel):
    id: str
    build_id: str
    kind: str
    report_digest: str
    counts: dict[str, Any]
    created_at: Timestamp


class FingerprintRecord(WireModel):
    build_id: str
    domain: str
    fingerprint: str
    inputs: dict[str, Any]
    fidelity: str


class VendorReadiness(WireModel):
    vendor_id: str
    title: str
    ready: bool
    missing_requirements: list[str]
    required_artifacts: list[str]


class ReviewDecision(WireModel):
    id: str
    project_id: str
    build_id: str
    slot: str
    actor: str
    decision: str
    note: str
    dossier_digest: str
    acknowledged_models: list[str]
    created_at: Timestamp


class PublishRecord(WireModel):
    id: str
    project_id: str
    build_id: str
    tag: str
    commit_sha: str
    dossier_digest: str
    published_by: str
    forge_url: str
    asset_names: list[str]
    acknowledged_models: list[str]
    created_at: Timestamp


class UnpinnedModel(WireModel):
    key: str
    source_path: str
    reference: str
    location: str


class ApprovalState(WireModel):
    designer: Optional[ReviewDecision]
    qa: Optional[ReviewDecision]
    both_approved: bool
    published: Optional[PublishRecord]
    electrical_errors: list[str]
    can_approve_designer: bool
    can_approve_qa: bool
    can_withdraw: bool
    can_publish: bool
    blocked_reason: str
    unpinned_models: list[UnpinnedModel]


class ForgeRelease(WireModel):
    tag: str
    url: str


class BuildDetail(WireModel):
    build: BuildRecord
    candidate: CandidateRecord
    configuration: dict[str, Any]
    members: list[MemberRecord]
    evidence: list[EvidenceRecord]
    fingerprints: dict[str, FingerprintRecord]
    vendor_readiness: list[VendorReadiness]
    forge: ForgeSummary
    approvals: ApprovalState
    forge_release: Optional[ForgeRelease]


class ApprovalDecision(WireModel):
    approvals: ApprovalState


class PublishResult(WireModel):
    release: ForgeRelease
    filename: str
    publish: PublishRecord


# ---------------------------------------------------------------------------
# Documentation sheets and build logs
# ---------------------------------------------------------------------------


class SheetPdf(WireModel):
    path: str
    released_digest: str
    media_type: str


class DocumentSheet(WireModel):
    key: str
    pdf: SheetPdf


class DocumentSheetList(WireModel):
    sheets: list[DocumentSheet]


class BuildLogStep(WireModel):
    step_id: str
    step_type: str
    returncode: Optional[int]
    elapsed_ms: int | float
    skipped_reason: str
    argv: list[str]
    status: str


class BuildLogs(WireModel):
    timings: list[Any]
    steps: list[BuildLogStep]
