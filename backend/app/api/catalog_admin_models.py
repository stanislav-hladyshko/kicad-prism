"""Success-response models for the catalog admin and read routes.

These describe what ``app/api/catalog_admin.py`` already returns. The shapes
come from the catalog read models, the metadata registry and the shared job
queue's decoder; every model accepts keys it does not name
(``extra="allow"``) so a field added downstream still reaches the client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LocalInventory(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source: str
    quantity: float
    uom: str
    inventory_status: str
    fetch_status: str
    fetched_at: str
    mixed_units: bool
    mixed_status: bool
    mixed_fetch: bool
    mixed_freshness: bool


class SupplySource(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    kind: str
    id: str
    display_name: str
    stock: float
    uom: str
    stock_status: str
    fetch_status: str
    fetched_at: str
    mixed_units: bool
    mixed_status: bool
    mixed_fetch: bool
    mixed_freshness: bool


class Supply(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    sources: list[SupplySource]


class RepresentationAsset(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    asset_type: str
    name: str
    target_library: str
    target_name: str
    sha256: str
    preview_id: str


class Representation(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    revision_id: str
    label: str
    symbol: RepresentationAsset | None
    footprint: RepresentationAsset | None
    is_default: bool
    display_order: int
    source_internal_part_number: str
    provenance: dict[str, Any]


class ComponentAsset(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    asset_type: str
    name: str
    target_library: str
    target_name: str
    source_group: str
    sha256: str
    size_bytes: int
    content_type: str
    required: bool


class ComponentPreview(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    asset_id: str
    kind: str
    preview_key: str
    unit: str
    unit_label: str
    status: str
    content_type: str
    file_path: str
    generation_error: str
    sha256: str
    generator_fingerprint: str
    generator_version: str
    updated_at: str


class ValidationFinding(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    run_id: str
    severity: str
    rule_code: str
    rule_url: str
    message: str
    details: list[Any]
    object_name: str
    created_at: str


class ValidationRun(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    component_id: str
    revision_id: str
    asset_id: str
    asset_type: str
    checker_type: str
    status: str
    error_count: int
    warning_count: int
    exit_code: int | None = None
    tool_version: str
    created_at: str
    finished_at: str
    inherited: bool
    inherited_from_revision_id: str
    reports: dict[str, str]
    findings: list[ValidationFinding] | None = None


class ValidationAsset(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    asset_id: str
    asset_type: str
    asset_name: str
    target_library: str
    target_name: str
    status: str
    latest_run: ValidationRun | None


class ValidationSummary(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: str
    enabled: bool
    release_gate: str
    revision_id: str
    error_count: int
    warning_count: int
    missing_required_assets: list[str]
    assets: list[ValidationAsset]


class ComponentValidation(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    summary: ValidationSummary
    runs: list[ValidationRun]


class CatalogComponent(BaseModel):
    """A full component payload, or the lightweight summary the queue returns.

    Fields the summary does not carry are optional; ``response_model_exclude_unset``
    keeps an absent field out of the response instead of emitting ``null``.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    slug: str
    source: str
    identity_kind: str
    kind: str
    name: str
    value: str
    manufacturer: str
    mpn: str
    description: str
    package_name: str
    category: str
    datasheet_url: str
    vendor: str
    vendor_part_number: str
    mass_g: str
    rqjc_c_w: str
    rqjc_top_c_w: str
    temp_max_c: str
    temp_min_c: str
    power_dissipation_w: str
    rate: str
    sap_code: str
    extra_fields: dict[str, Any]
    interface: dict[str, Any]
    source_ref: dict[str, Any]
    summary: str
    revision: int
    version: str
    library_name: str
    symbol_name: str
    availability_state: str
    missing_assets: list[str]
    place_enabled: bool
    stock_known: bool
    stock_quantity: float
    stock_uom: str
    inventory_status: str
    release_status: str
    workflow_stage: str
    released_view: bool
    revision_id: str
    revision_updated_at: str
    created_by: str
    component_updated_at: str
    assets: list[ComponentAsset]
    previews: list[ComponentPreview]
    validation: ValidationSummary

    local_inventory: LocalInventory | None = None
    external_source: str | None = None
    external_id: str | None = None
    external_workflow_source: str | None = None
    external_workflow_id: str | None = None
    external_workflow_url: str | None = None
    external_url: str | None = None
    external_payload: dict[str, Any] | None = None
    external_updated_at: str | None = None
    sync_status: str | None = None
    sync_error: str | None = None
    supply: Supply | None = None
    serial_number: str | None = None
    lot_number: str | None = None
    pedigree: str | None = None
    last_synced_at: str | None = None
    is_active: bool | None = None
    parent_revision_id: str | None = None
    change_kind: str | None = None
    change_summary: str | None = None
    manifest_hash: str | None = None
    component_created_at: str | None = None
    revision_created_at: str | None = None
    current_revision_id: str | None = None
    released_revision_id: str | None = None
    is_historical_revision: bool | None = None
    representations: list[Representation] | None = None
    default_representation_id: str | None = None
    effective_representation_id: str | None = None


class CatalogComponentPage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[CatalogComponent]
    total: int
    page: int
    page_size: int
    pages: int


class CatalogCategory(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    count: int


class CatalogCategories(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    categories: list[CatalogCategory]


class WorkflowStageCount(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    workflow_stage: str
    count: int


class WorkflowSummary(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    stages: list[WorkflowStageCount]


class ReleaseQueueSummary(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    qa_review: int
    done: int
    blocked: int


class ReleaseQueuePage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[CatalogComponent]
    total: int
    page: int
    page_size: int
    pages: int
    summary: ReleaseQueueSummary


class CatalogHealth(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    enabled: bool
    checker_available: bool
    checker_path: str
    release_gate: str
    total_components: int
    released: int
    place_ready: int
    missing_files: int
    released_assets_missing_on_disk: int | None
    preview_failed: int
    validation: dict[str, int]


class ImportSession(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    scope: str
    project_id: str
    project_ids: list[str]
    project_revisions: dict[str, str]
    source_revision: str
    selection: dict[str, Any]
    status: str
    error_message: str
    created_by: str
    created_at: str
    updated_at: str
    proposal_count: int
    candidate_count: int


class ImportSessionPage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[ImportSession]


class ImportProposal(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    session_id: str
    dedupe_key: str
    component_uid: str
    reference: str
    metadata: dict[str, Any]
    assets: list[dict[str, Any]]
    provenance: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    status: str
    accepted_component_id: str
    draft: dict[str, Any]
    created_at: str
    updated_at: str


class ImportProposalPage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[ImportProposal]


class JobEvent(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: int
    event_type: str
    stage: str
    percent: float | None
    details: dict[str, Any]
    created_at: str


class CatalogJob(BaseModel):
    """A ``ws_jobs`` row after the catalog adapter and result spread.

    The adapter spreads a completed job's ``result`` at the top level, so this
    model accepts keys it does not name.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    job_id: str
    kind: str
    status: str
    message: str
    percent: float
    payload: dict[str, Any]
    created_at: str | None
    updated_at: str | None
    worker_pool: str
    priority: int
    artifact_key: str | None
    project_id: str | None
    repository_id: str | None
    requested_by: str
    stage: str
    result_path: str | None
    result_digest: str | None
    result_metadata: dict[str, Any]
    log_path: str | None
    error_code: str
    error_message: str
    fence: int
    lease_owner: str
    lease_expires_at: str | None
    heartbeat_at: str | None
    available_at: str | None
    attempt: int
    max_attempts: int
    cancel_requested_at: str | None
    started_at: str | None
    completed_at: str | None
    resource_requirements: dict[str, Any]
    lock_requirements: list[Any]
    id: str
    job_type: str
    result: dict[str, Any]
    checkpoint: dict[str, Any]
    created_by: str
    attempts: int
    progress: float


class CatalogJobWithEvents(CatalogJob):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    events: list[JobEvent]


class ComponentRevision(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    component_id: str
    version: int
    parent_revision_id: str
    change_kind: str
    change_summary: str
    created_by: str
    manifest_hash: str
    release_status: str
    workflow_stage: str
    created_at: str
    updated_at: str


class ComponentRevisionPage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[ComponentRevision]


class ComparedPreview(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    previewId: str
    previewStatus: str
    previewSha256: str
    previewGeneratorFingerprint: str
    unit: str
    unitLabel: str


class ComparedAsset(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    assetId: str
    assetType: str
    targetLibrary: str
    targetName: str
    sha256: str
    sizeBytes: int
    previewId: str
    previewStatus: str
    previewSha256: str
    previewGeneratorFingerprint: str
    previews: list[ComparedPreview]


class ComparedRevision(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    revisionId: str
    version: int
    manifestHash: str


class RevisionComparisonSummary(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    metadataChanges: int
    assetChanges: int
    representationChanges: int


class MetadataChange(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    field: str
    before: str
    after: str
    status: str


class AssetChange(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    key: str
    before: ComparedAsset | None
    after: ComparedAsset | None
    status: str


class ComparedRepresentation(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    label: str
    symbolAssetId: str
    footprintAssetId: str
    isDefault: bool
    displayOrder: int
    sourceInternalPartNumber: str
    provenance: dict[str, Any]


class RepresentationChange(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    key: str
    before: ComparedRepresentation | None
    after: ComparedRepresentation | None
    status: str


class RevisionComparison(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    componentId: str
    before: ComparedRevision
    after: ComparedRevision
    summary: RevisionComparisonSummary
    metadataChanges: list[MetadataChange]
    assetChanges: list[AssetChange]
    representationChanges: list[RepresentationChange]


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    component_id: str
    sequence: int
    revision_id: str
    event_type: str
    actor: str
    details_json: str
    details: dict[str, Any]
    previous_hash: str
    event_hash: str
    created_at: str


class AuditEventPage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[AuditEvent]


class AuditVerification(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    valid: bool
    coverage: str
    reason: str
    event_count: int
    verified_count: int
    first_invalid_event_id: str
    head_hash: str
    anchored_head_hash: str | None = None
    first_invalid_revision_id: str | None = None
    first_invalid_asset_id: str | None = None
    revision_count: int | None = None
    asset_count: int | None = None
    purged_asset_ids: list[str] | None = None


class ComponentUsage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    component_id: str
    project_id: str
    source_revision: str
    references_json: str
    details_json: str
    source: str
    is_current: bool
    first_seen_at: str
    last_seen_at: str
    references: list[Any]
    details: list[Any]


class ComponentUsagePage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[ComponentUsage]


class ComponentReviewDecision(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    component_id: str
    revision_id: str
    reviewer: str
    reviewer_role: str
    decision: str
    note: str
    manifest_hash: str
    validation_json: str
    policy_json: str
    created_at: str
    validation: dict[str, Any]
    policy: dict[str, Any]


class ComponentReviewDecisionPage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[ComponentReviewDecision]


class ComponentReleaseRecord(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    component_id: str
    revision_id: str
    release_label: str
    manifest_hash: str
    released_by: str
    approval_decision_id: str
    validation_json: str
    policy_json: str
    created_at: str
    validation: dict[str, Any]
    policy: dict[str, Any]


class ComponentReleaseRecordPage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[ComponentReleaseRecord]


class FolderImportRoot(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    path_hint: str


class FolderImportRootPage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[FolderImportRoot]


class AssetSearchItem(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    asset_type: str
    name: str
    target_library: str
    target_name: str
    sha256: str
    size_bytes: int
    usage_count: int


class AssetSearchPage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[AssetSearchItem]


class AssetBrowseResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    files: list[str]
    total: int


class MetadataField(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    key: str
    label: str
    description: str
    group: str
    type: str
    unit: str
    enum_values: list[str]
    storage_kind: str
    storage_key: str
    built_in: bool
    required: bool
    display_order: int
    archived: bool
    created_by: str
    updated_by: str
    created_at: str
    updated_at: str


class MetadataFieldPage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_: str = Field(alias="schema")
    items: list[MetadataField]


class MetadataGridPreferences(BaseModel):
    """The saved layout, or ``{}`` when this user has never saved one."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    visible: list[str] | None = None
    order: list[str] | None = None
    widths: dict[str, int] | None = None
    pinned: list[str] | None = None


class MetadataGridPage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[CatalogComponent]
    total: int
    page: int
    page_size: int
    pages: int
    schema_: str = Field(alias="schema")
    fields: list[MetadataField]


class MetadataBatchItem(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    component_id: str
    expected_revision_id: str
    name: str
    mpn: str
    patch: dict[str, str]
    diff: list[dict[str, str]]
    validation_status: str
    error_message: str
    applied_revision_id: str


class MetadataBatch(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    source: str
    status: str
    schema_version: str
    change_summary: str
    unknown_fields: list[Any]
    created_by: str
    total_items: int
    valid_items: int
    applied_items: int
    failed_items: int
    created_at: str
    updated_at: str
    items: list[MetadataBatchItem]


# ─── Write responses ────────────────────────────────────────────────────────
#
# ``ImportSessionCreated`` and its siblings describe what a write route returns
# today. Optional fields are absent from a body when the service omits the key;
# every route that can omit one is served with ``response_model_exclude_unset``.


class ImportSessionCreated(ImportSession):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    job_id: str


class FolderSnapshot(BaseModel):
    """A ``catalog_import_snapshots`` row as ``create_snapshot`` returns it.

    ``created_at`` and ``completed_at`` arrive as ``datetime`` values from
    PostgreSQL. The route's JSON has always carried their ISO spelling
    (``+00:00``, not Pydantic's ``Z``), so the model states the wire type and
    converts the raw value instead of re-serializing it.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    source_type: str
    display_name: str
    source_locator: str
    status: str
    manifest_sha256: str
    file_count: int
    size_bytes: int
    created_by: str
    created_at: str
    completed_at: str | None

    @field_validator("created_at", "completed_at", mode="before")
    @classmethod
    def _wire_timestamp(cls, value: Any) -> Any:
        return value.isoformat() if isinstance(value, datetime) else value


class FolderSnapshotFile(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    relative_path: str
    sha256: str
    size_bytes: int


class FolderDiscoveryFootprintFile(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    relative_path: str
    library: str
    name: str
    filename_name: str


class FolderDiscoveryFootprint(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: str
    selected: FolderDiscoveryFootprintFile | None
    candidates: list[FolderDiscoveryFootprintFile]


class FolderDiscoveryModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    reference: str
    status: str
    candidates: list[dict[str, Any]]


class FolderDiscoveryFinding(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    code: str
    severity: str
    message: str


class FolderDiscoveryMetadata(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    value: str
    description: str
    datasheet: str
    manufacturer: str
    manufacturer_part_number: str
    fields: dict[str, str]


class FolderDiscoverySymbol(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    relative_path: str


class FolderDiscoveryMatch(BaseModel):
    """The catalog part an identity already belongs to (``existing_component``)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    component_id: str
    revision_id: str
    version: int
    name: str
    manufacturer: str
    manufacturer_part_number: str
    is_active: bool


class FolderDiscoveryComponent(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    symbol_name: str
    library: str
    metadata: FolderDiscoveryMetadata
    symbol: FolderDiscoverySymbol
    footprint_reference: str
    footprint: FolderDiscoveryFootprint
    models: list[FolderDiscoveryModel]
    findings: list[FolderDiscoveryFinding]
    existing_component: FolderDiscoveryMatch | None


class FolderDiscovery(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    components: list[FolderDiscoveryComponent]
    required_paths: list[str]
    inventory_file_count: int
    discovery_file_count: int
    directory_signature: str
    existing_component_count: int


class ImportProposalAcceptance(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    proposal: ImportProposal | None
    component: CatalogComponent | None


class BulkImportAcceptanceItem(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    proposal_id: str
    status: str
    component_id: str | None = None
    error: str | None = None


class BulkImportAcceptance(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    accepted: int
    failed: int
    results: list[BulkImportAcceptanceItem]


class SavedImportDrafts(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    saved: int


class ImportedProposalCsv(SavedImportDrafts):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    skipped_unknown_rows: int


class ComponentMutation(BaseModel):
    """A write that answers with the component's fresh payload."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    component: CatalogComponent | None


class ComponentDeleteResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    ok: bool


class SymbolImportResult(BaseModel):
    """``selection_required`` stops before writing; ``imported`` carries the component."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    mode: str
    discovered_symbols: list[str] | None = None
    selected_symbol: str | None = None
    component: CatalogComponent | None = None


class FootprintImportResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    mode: str
    discovered_footprints: list[str] | None = None
    selected_footprint: str | None = None
    component: CatalogComponent | None = None


class CatalogJobQueued(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    job_id: str
    status: str


class MetadataBatchStarted(CatalogJobQueued):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    batch_id: str


class MetadataCsvPreview(MetadataBatch):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source_rows: int
    skipped_unchanged_rows: int


class KicadDblMissingModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    component_id: str
    revision_id: str
    asset_id: str
    reason: str


class KicadDblBundle(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    export_root: str
    component_count: int
    category_count: int
    sqlite_path: str
    linux_dbl: str
    windows_dbl: str
    sym_lib_table: str
    fp_lib_table: str
    model_count: int
    missing_models: list[KicadDblMissingModel]


class InventoryImportResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    updated: int
    not_found: int
    errors: list[str]


__all__ = [
    "AssetBrowseResult",
    "AssetChange",
    "AssetSearchItem",
    "AssetSearchPage",
    "AuditEvent",
    "AuditEventPage",
    "AuditVerification",
    "BulkImportAcceptance",
    "BulkImportAcceptanceItem",
    "CatalogCategories",
    "CatalogCategory",
    "CatalogComponent",
    "CatalogComponentPage",
    "CatalogHealth",
    "CatalogJob",
    "CatalogJobQueued",
    "CatalogJobWithEvents",
    "ComponentAsset",
    "ComponentDeleteResult",
    "ComponentMutation",
    "ComponentPreview",
    "ComponentReleaseRecord",
    "ComponentReleaseRecordPage",
    "ComponentReviewDecision",
    "ComponentReviewDecisionPage",
    "ComponentRevision",
    "ComponentRevisionPage",
    "ComponentUsage",
    "ComponentUsagePage",
    "ComponentValidation",
    "FolderDiscovery",
    "FolderDiscoveryComponent",
    "FolderDiscoveryFinding",
    "FolderDiscoveryFootprint",
    "FolderDiscoveryFootprintFile",
    "FolderDiscoveryMatch",
    "FolderDiscoveryMetadata",
    "FolderDiscoveryModel",
    "FolderDiscoverySymbol",
    "FolderImportRoot",
    "FolderImportRootPage",
    "FolderSnapshot",
    "FolderSnapshotFile",
    "FootprintImportResult",
    "ImportProposal",
    "ImportProposalAcceptance",
    "ImportProposalPage",
    "ImportSession",
    "ImportSessionCreated",
    "ImportSessionPage",
    "ImportedProposalCsv",
    "InventoryImportResult",
    "KicadDblBundle",
    "KicadDblMissingModel",
    "MetadataBatch",
    "MetadataBatchItem",
    "MetadataBatchStarted",
    "MetadataChange",
    "MetadataCsvPreview",
    "MetadataField",
    "MetadataFieldPage",
    "MetadataGridPage",
    "MetadataGridPreferences",
    "ReleaseQueuePage",
    "ReleaseQueueSummary",
    "RepresentationChange",
    "RevisionComparison",
    "RevisionComparisonSummary",
    "SavedImportDrafts",
    "SymbolImportResult",
    "ValidationRun",
    "ValidationSummary",
    "WorkflowSummary",
]
