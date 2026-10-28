"""Settings for the design bundle (ADR native design bundle §4.4, §5, §8).

`Settings` in `app.core.config` inherits these fields; read them through
`app.core.config.settings` like every other setting. They live in their own
module so `config.py` stays untouched while the bundle work lands (ownership
map, WP-K).

- `PRISM_DESIGN_BUNDLE_*`: the `design_bundle` job, its resource slot, the
  per-stage limits it passes to `prism-kicad` (cli-spec.md §7), the store and
  its retention, and the paced backfill.
- `PRISM_KICAD_PYTHON`, `PRISM_ZONE_REFILL_TIMEOUT_SECONDS`: KiCad's Python
  for the zone refill and the board upgrade (cli-spec.md §9.2, §9.4).
"""
from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

GIB = 1024**3
#: cli-spec.md §7.4: the Linux backstop adds 1 GiB plus thread stacks over the heap cap.
BACKSTOP_OVERHEAD_BYTES = GIB + GIB // 4


class DesignBundleSettings(BaseSettings):
    # ===========================================
    # The design_bundle job
    # ===========================================
    PRISM_DESIGN_BUNDLE_AUTO_BUILD: bool = Field(
        default=True,
        description=(
            "Queue a design bundle for the project's HEAD after every import and sync, and run the "
            "paced prebuild of each branch's newest commits and every tagged commit (ADR amendment "
            "K2 §7). On by default since the C2r switch, which relies on warm bundles (C2r §10). "
            "Off: bundles are built only on demand."
        ),
    )
    PRISM_DESIGN_BUNDLE_CLI_PATH: str = Field(
        default="", description="The prism-kicad binary. Unset uses prism-kicad on PATH (/usr/local/bin in the image)."
    )
    PRISM_DESIGN_BUNDLE_ROOT: str = Field(
        default="", description="Bundle store. Defaults to <KICAD_PROJECTS_ROOT>/.kicad-prism/bundles."
    )
    PRISM_DESIGN_BUNDLE_SCHEMA_DIR: str = Field(
        default="",
        description="Committed JSON Schema of the bundle contracts. Unset uses native/contracts/generated/schema.",
    )
    PRISM_ARTWORK_CONFIG_DIR: str = Field(
        default="",
        description=(
            "The native `prism-kicad artwork` configurations (assembly.json, testpoints.json). "
            "Unset uses native/src/rs/prism-artwork/configs, from the checkout or the image."
        ),
    )
    PRISM_DESIGN_BUNDLE_CONCURRENCY: int = Field(
        default=1, ge=1, le=8, description="Capacity of the design_bundle resource slot (bundle builds at once)."
    )
    PRISM_DESIGN_BUNDLE_THREADS: int = Field(default=4, ge=1, le=64, description="prism-kicad --threads.")
    PRISM_DESIGN_BUNDLE_MAX_ATTEMPTS: int = Field(
        default=3, ge=1, le=10, description="Attempts of one design_bundle job before a failing KiCad step ends it."
    )
    PRISM_DESIGN_BUNDLE_FAILED_RETRY_SECONDS: int = Field(
        default=3600,
        ge=0,
        description=(
            "How long a failed design bundle stays failed before a status read queues it again; "
            "0 waits for a deliberate Generate or Regenerate."
        ),
    )
    PRISM_DESIGN_BUNDLE_PARSE_TIMEOUT_MS: int = Field(default=300_000, ge=1_000)
    PRISM_DESIGN_BUNDLE_BOARD_TIMEOUT_MS: int = Field(default=600_000, ge=1_000)
    PRISM_DESIGN_BUNDLE_MODELS_TIMEOUT_MS: int = Field(default=120_000, ge=1_000)
    GEOMETER_EXECUTABLE: str = Field(
        default="",
        description=(
            "The pinned Geometer runtime stage `models` runs for the assembly HLR member "
            "(amendment C3 §3.1). The image sets it; empty leaves the member out."
        ),
    )
    PRISM_DESIGN_BUNDLE_VERIFY_TIMEOUT_MS: int = Field(
        default=300_000,
        ge=1_000,
        description="`prism-kicad bundle verify` when an archive is stored (bundle-format.md §9).",
    )
    PRISM_DESIGN_BUNDLE_PARSE_MEMORY_BYTES: int = Field(default=4 * GIB, ge=64 * 1024**2)
    PRISM_DESIGN_BUNDLE_BOARD_MEMORY_BYTES: int = Field(default=6 * GIB, ge=64 * 1024**2)
    PRISM_DESIGN_BUNDLE_MODELS_MEMORY_BYTES: int = Field(default=2 * GIB, ge=64 * 1024**2)
    PRISM_DESIGN_BUNDLE_MEMORY_BUDGET_BYTES: int = Field(
        default=8 * GIB,
        ge=GIB,
        description=(
            "Worker memory the design_bundle slot may use: CONCURRENCY x (largest stage cap + 1.25 GiB) "
            "must fit (cli-spec.md §7.4). 12 GiB worker minus the other slots by default."
        ),
    )
    PRISM_DESIGN_BUNDLE_GLB_TIMEOUT_SECONDS: int = Field(
        default=600, ge=10, description="kicad-cli pcb export glb for the models stage."
    )
    PRISM_DESIGN_BUNDLE_UPGRADE_TIMEOUT_SECONDS: int = Field(
        default=900, ge=10, description="KiCad's upgrade of one older board or schematic before stage parse."
    )
    PRISM_DESIGN_BUNDLE_UPGRADE_WORKERS: int = Field(
        default=8,
        ge=1,
        description="KiCad upgrades of one source set run at once (each file in its own scratch directory).",
    )
    PRISM_KICAD_PYTHON: str = Field(
        default="",
        description=(
            "KiCad's Python (imports pcbnew) for the zone refill and board upgrades. "
            "Empty finds it: python.exe beside kicad-cli.exe on Windows, KiCad.app's Python on macOS, /usr/bin/python3 on Linux."
        ),
    )
    PRISM_ZONE_REFILL_TIMEOUT_SECONDS: int = Field(default=900, ge=10, description="One pcbnew zone refill.")

    # ===========================================
    # Store, retention and backfill (ADR §5, §8)
    # ===========================================
    PRISM_DESIGN_BUNDLE_RETENTION_DAYS: int = Field(
        default=30,
        ge=1,
        description=(
            "Days a bundle may go unused before retention removes it. The prebuild's commits, "
            "released Release Studio commits and bundles an alias points to are always kept."
        ),
    )
    PRISM_DESIGN_BUNDLE_CACHE_GRACE_SECONDS: int = Field(
        default=3600,
        ge=0,
        description=(
            "How long an unpacked-cache version directory must be untouched before retention may remove it. "
            "A reader's extract refreshes it; this is the floor that keeps a directory a reader may still "
            "be using (a slow comparison or release build) out of the sweep."
        ),
    )
    PRISM_DESIGN_BUNDLE_CACHE_IDLE_DAYS: int = Field(
        default=7,
        ge=0,
        description=(
            "Drop an archive's unpacked caches once no read has used it for this many days, even while the "
            "archive itself stays. 0 keeps them as long as the archive is kept. Caches are re-extracted on "
            "the next read."
        ),
    )
    PRISM_DESIGN_BUNDLE_CACHE_MAX_BYTES: int = Field(
        default=4 * GIB,
        ge=0,
        description=(
            "Total unpacked-cache bytes across the bundle store. Over the cap, the least recently used "
            "version directories go first. 0 disables the cap."
        ),
    )
    PRISM_DESIGN_BUNDLE_CACHE_PROJECT_MAX_BYTES: int = Field(
        default=1 * GIB,
        ge=0,
        description=(
            "Unpacked-cache bytes one project may hold. Over the cap, that project's least recently used "
            "version directories go first. 0 disables the cap."
        ),
    )
    PRISM_DESIGN_BUNDLE_BACKFILL_INTERVAL_SECONDS: int = Field(
        default=60,
        ge=0,
        description=(
            "Seconds between prebuild ticks; each aliases the commits it can to stored bundles and "
            "queues at most one build. 0 disables it."
        ),
    )
    PRISM_DESIGN_BUNDLE_PREBUILD_COMMITS: int = Field(
        default=10,
        ge=0,
        description=(
            "How many of each branch's newest commits (first parent) the background prebuild keeps "
            "bundles for, besides every tagged commit. 0 prebuilds branch heads and tags only."
        ),
    )
    PRISM_DESIGN_BUNDLE_REUSE_CANDIDATES: int = Field(
        default=3,
        ge=0,
        description=(
            "How many stored bundles a new revision is checked against with prism-kicad bundle reuse "
            "before it is built. 0 disables reuse."
        ),
    )

    @model_validator(mode="after")
    def _memory_fits_the_slot(self) -> "DesignBundleSettings":
        largest = max(
            self.PRISM_DESIGN_BUNDLE_PARSE_MEMORY_BYTES,
            self.PRISM_DESIGN_BUNDLE_BOARD_MEMORY_BYTES,
            self.PRISM_DESIGN_BUNDLE_MODELS_MEMORY_BYTES,
        )
        needed = self.PRISM_DESIGN_BUNDLE_CONCURRENCY * (largest + BACKSTOP_OVERHEAD_BYTES)
        if needed > self.PRISM_DESIGN_BUNDLE_MEMORY_BUDGET_BYTES:
            raise ValueError(
                "PRISM_DESIGN_BUNDLE_CONCURRENCY x (largest stage memory cap + 1.25 GiB) "
                f"is {needed} bytes, over PRISM_DESIGN_BUNDLE_MEMORY_BUDGET_BYTES"
            )
        return self
