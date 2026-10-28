"""The Release Studio step catalogue and its evidence capture (R7).

Every released artifact is produced by one entry here, and every entry names
the KiCad 10.0.4 job type it corresponds to so :mod:`app.release_studio.jobset`
can classify its hermeticity.  Steps run against the materialized input
closure, never against a user's working tree.

DRC and ERC are ordinary steps whose *output is the evidence*.  ``kicad-cli``
returns 0 for a board with violations unless ``--exit-code-violations`` is
passed, so violations are recorded rather than aborting the build; only a
genuine tool failure fails the step.
"""

from __future__ import annotations

import json
import shlex
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.services.kicad_tools import KicadTool, ToolError
from app.services.tree_removal import remove_tree


DEFAULT_CLI_TIMEOUT_SECONDS = 900

# KiCad 10.0.4 `sch export bom --preset` looks up built-in names in the
# temporary vector from BOM_PRESET::BuiltInPresets() and keeps a pointer
# into it (eeschema_jobs_handler.cpp). After the range-for ends that
# vector is gone; copying the preset then throws std::bad_alloc /
# length_error. Translate the built-ins to explicit CLI flags instead.
_CURRENT_BOM_SETTINGS = "Current project settings"
_BUILTIN_BOM_CLI: dict[str, tuple[str, ...]] = {
    "Grouped By Value": (
        "--fields",
        "Reference,QUANTITY,Value,Manufacturer,Manufacturer Part Number,Footprint,Datasheet,DNP",
        "--labels",
        "Reference,Qty,Value,Manufacturer,MPN,Footprint,Datasheet,DNP",
        "--group-by",
        "Value,DNP",
    ),
    "Grouped By Value and Footprint": (
        "--fields",
        "Reference,QUANTITY,Value,Manufacturer,Manufacturer Part Number,Footprint,Datasheet,DNP",
        "--labels",
        "Reference,Qty,Value,Manufacturer,MPN,Footprint,Datasheet,DNP",
        "--group-by",
        "Value,Footprint,DNP",
    ),
    "Attributes": (
        "--fields",
        "Reference,Value,DNP,EXCLUDE_FROM_BOM,EXCLUDE_FROM_BOARD,"
        "EXCLUDE_FROM_SIM,EXCLUDE_FROM_POS_FILES",
        "--labels",
        "Reference,Value,Do Not Place,Exclude from BOM,Exclude from Board,"
        "Exclude from Simulation,Exclude from Position Files",
        "--group-by",
        "Value,Footprint",
    ),
}


def _bom_extra_argv(bom_preset: str) -> tuple[str, ...]:
    name = (bom_preset or "").strip()
    if not name or name == _CURRENT_BOM_SETTINGS:
        return ()
    mapped = _BUILTIN_BOM_CLI.get(name)
    if mapped is not None:
        return mapped
    return ("--preset", name)


#: One `kicad-cli` at a time. Parallel invocations of `sch export bom` have
#: died with `std::bad_alloc` even when DRC/ERC on the same schematic succeed.
_MAX_PARALLEL_STEPS = 1


@dataclass(frozen=True, slots=True)
class StepSpec:
    """One `kicad-cli` invocation and the member class it produces.

    ``argv`` uses ``{board}``, ``{schematic}``, ``{out}`` and ``{variant}``
    placeholders so the recorded argv can be normalized to closure-relative
    text without host paths.
    """

    step_id: str
    step_type: str
    argv: tuple[str, ...]
    source: str  # "board" | "schematic"
    output_kind: str  # "dir" | "file"
    output_name: str
    member_kind: str
    canonicalizer: str
    domains: tuple[str, ...]
    optional: bool = False
    variant_flag: bool = False


@dataclass(frozen=True, slots=True)
class StepOutput:
    """The result of running one catalogue step."""

    step_id: str
    step_type: str
    normalized_argv: tuple[str, ...]
    returncode: int
    files: tuple[Path, ...]
    root: Path
    spec: StepSpec
    stdout: str = ""
    stderr: str = ""
    skipped_reason: str = ""
    #: Wall clock of this invocation. Evidence only — never a fingerprint input.
    elapsed_ms: int = 0

    @property
    def ran(self) -> bool:
        return not self.skipped_reason


# Ordered so cheap validation runs before expensive artwork: a board that
# cannot be opened fails in seconds rather than after a STEP export.
STEP_CATALOGUE: tuple[StepSpec, ...] = (
    StepSpec(
        step_id="drc",
        step_type="pcb_drc",
        argv=("pcb", "drc", "--format", "json", "--severity-all", "--output", "{out}", "{board}"),
        source="board",
        output_kind="file",
        output_name="evidence/drc.json",
        member_kind="drc_report",
        canonicalizer="drc_erc_json",
        domains=("evidence",),
    ),
    StepSpec(
        step_id="erc",
        step_type="sch_erc",
        argv=("sch", "erc", "--format", "json", "--severity-all", "--output", "{out}", "{schematic}"),
        source="schematic",
        output_kind="file",
        output_name="evidence/erc.json",
        member_kind="erc_report",
        canonicalizer="drc_erc_json",
        domains=("evidence",),
    ),
    StepSpec(
        step_id="board_stats",
        step_type="pcb_export_stats",
        argv=("pcb", "export", "stats", "--format", "json", "--output", "{out}", "{board}"),
        source="board",
        output_kind="file",
        output_name="fabrication/board-stats.json",
        member_kind="board_stats",
        canonicalizer="board_stats_json",
        domains=("bare_board",),
    ),
    StepSpec(
        step_id="gerbers",
        step_type="pcb_export_gerbers",
        argv=("pcb", "export", "gerbers", "--output", "{out}", "{board}"),
        source="board",
        output_kind="dir",
        output_name="fabrication/gerbers",
        member_kind="gerber",
        canonicalizer="",  # per-file, resolved by suffix
        domains=("bare_board",),
        variant_flag=True,
    ),
    StepSpec(
        step_id="drill",
        step_type="pcb_export_drill",
        argv=(
            "pcb", "export", "drill",
            "--format", "excellon", "--excellon-units", "mm",
            "--output", "{out}", "{board}",
        ),
        source="board",
        output_kind="dir",
        output_name="fabrication/drill",
        member_kind="drill",
        canonicalizer="",
        domains=("bare_board",),
    ),
    StepSpec(
        step_id="positions",
        step_type="pcb_export_pos",
        argv=(
            "pcb", "export", "pos",
            "--format", "csv", "--units", "mm", "--side", "both",
            "--output", "{out}", "{board}",
        ),
        source="board",
        output_kind="file",
        output_name="assembly/positions.csv",
        member_kind="position",
        canonicalizer="csv",
        domains=("assembly",),
        variant_flag=True,
    ),
    StepSpec(
        step_id="bom",
        step_type="sch_export_bom",
        argv=("sch", "export", "bom", "--output", "{out}", "{schematic}"),
        source="schematic",
        output_kind="file",
        output_name="assembly/bom.csv",
        member_kind="bom",
        canonicalizer="csv",
        domains=("assembly",),
        variant_flag=True,
    ),
    StepSpec(
        step_id="schematic_pdf",
        step_type="sch_export_plot_pdf",
        argv=("sch", "export", "pdf", "--output", "{out}", "{schematic}"),
        source="schematic",
        output_kind="file",
        output_name="documentation/schematic.pdf",
        member_kind="schematic_pdf",
        canonicalizer="pdf",
        domains=("documentation",),
        optional=True,
    ),
)


STEP_BY_ID: Mapping[str, StepSpec] = {spec.step_id: spec for spec in STEP_CATALOGUE}

_CATALOGUE_TITLES: Mapping[str, str] = {
    "drc": "DRC",
    "erc": "ERC",
    "board_stats": "board statistics",
    "gerbers": "Gerbers",
    "drill": "drill files",
    "positions": "pick-and-place positions",
    "bom": "bill of materials",
    "schematic_pdf": "schematic PDF",
}


def _catalogue_title(spec: StepSpec) -> str:
    return _CATALOGUE_TITLES.get(spec.step_id, spec.step_id.replace("_", " "))

#: Checks finish before any assembly `kicad-cli` or Cruncher load.
#: The settings each board step's job needs so a jobset writes what today's
#: ``kicad-cli`` command writes. A job's defaults are not the CLI's (KiCad
#: 10.0.7, common/jobs and kicad/cli): gerbers default to precision 5 and to
#: sketched, crossed-out DNP footprints and a drawing sheet; drill to inches;
#: positions to the drill-file origin and per-side files; and a wrongly
#: spelled value makes KiCad drop the whole jobset silently. Checked against
#: the CLI on EDA-04903: every output identical (DRC up to item order).
#: ``severity`` is a bitmask (ERROR 0x20 | WARNING 0x10 | EXCLUSION 0x04, the
#: CLI's ``--severity-all``); stats' ``output_format`` is an unnamed enum (1 = JSON).
JOBSET_SETTINGS: Mapping[str, Mapping[str, Any]] = {
    "drc": {
        "format": "json", "units": "mm", "severity": 52, "fail_on_error": False,
        "report_all_track_errors": False, "refill_zones": False, "save_board": False,
    },
    "board_stats": {
        "output_format": 1, "units": "mm", "exclude_footprints_without_pads": False,
        "subtract_holes_from_board": False, "subtract_holes_from_copper": False,
    },
    "positions": {
        "format": "csv", "units": "mm", "side": "both", "single_file": True,
        "use_drill_place_file_origin": False, "smd_only": False, "exclude_footprints_with_th": False,
        "exclude_dnp": False, "exclude_bom": False, "negate_bottom_x": False, "gerber_board_edge": False,
    },
    "gerbers": {
        "precision": 6, "use_x2_format": True, "include_netlist_attributes": True,
        "disable_aperture_macros": False, "use_protel_file_extension": True, "create_gerber_job_file": True,
        "plot_footprint_values": True, "plot_ref_des": True, "plot_drawing_sheet": False,
        "sketch_pads_on_fab_layers": False, "plot_pad_numbers": False,
        "hide_dnp_footprints_on_fab_layers": False, "sketch_dnp_footprints_on_fab_layers": False,
        "crossout_dnp_footprints_on_fab_layers": False, "subtract_solder_mask_from_silk": False,
        "use_drill_origin": False, "check_zones": False, "layers": [], "layers_to_include_on_all_layers": [],
    },
    "drill": {
        "format": "excellon", "units": "mm", "zero_format": "decimal", "drill_origin": "abs",
        "excellon.mirror_y": False, "excellon.minimal_header": False, "excellon.combine_pth_npth": True,
        "excellon.oval_drill_route": False, "generate_map": False, "generate_tenting": False,
        "generate_report": False, "map_format": "pdf", "gerber_precision": 6,
    },
}
_JOBSET_NAMESPACE = uuid.UUID("6d2b6f4e-2f0b-4b43-9c3b-7b8f1f0d5a11")
#: Where a board jobset writes, inside the project directory it runs in.
JOBSET_DIR = ".prism-jobset"

#: Every board output, from one ``kicad-cli jobset run`` (one board load) when
#: the run can use a jobset (:func:`run_board_jobset`); DRC first so a board
#: that cannot be opened fails before anything is exported.
CATALOGUE_WAVE_BOARD: tuple[str, ...] = ("drc", "board_stats", "positions", "gerbers", "drill")
#: Schematic checks.
CATALOGUE_WAVE_CHECKS: tuple[str, ...] = ("erc",)
#: Schematic BOM, alone. Parallel `kicad-cli` loads have been dying with
#: ``std::bad_alloc`` on the worker, including BOM overlapping positions.
CATALOGUE_WAVE_BOM: tuple[str, ...] = ("bom",)
#: Artwork the document engine does not itself plot.
CATALOGUE_WAVE_ARTWORK: tuple[str, ...] = ("schematic_pdf",)

# The Documentation Engine's sheets, deliberately *outside* `STEP_CATALOGUE`:
# they are composed in-process rather than by a `kicad-cli` invocation, so they
# have no step type in KiCad's job registry and `run_step_catalogue` must not
# try to execute them. The spec exists so composed sheets travel through the
# same member pipeline as everything else, with the canonicalizer resolved per
# file by suffix.
#
# `optional=False`: a compose failure is a failed documents step, not a
# silently missing document set. Manufacturing members still assemble.
DOCUMENT_STEP_SPEC = StepSpec(
    step_id="documents",
    step_type="prism_compose_documents",
    argv=(),
    source="board",
    output_kind="dir",
    output_name="documentation",
    member_kind="document_sheet",
    canonicalizer="",
    domains=("documentation",),
    optional=False,
)

# Which steps produce release evidence rather than shipped artwork.
EVIDENCE_STEPS: Mapping[str, str] = {"drc": "drc", "erc": "erc"}


class StepExecutionError(RuntimeError):
    """A catalogue step failed for a reason that is not a design violation."""


def selected_steps(
    *,
    board: Path | None,
    schematic: Path | None,
    only: Sequence[str] | None = None,
) -> tuple[StepSpec, ...]:
    """Return catalogue steps whose source document exists."""

    chosen: list[StepSpec] = []
    for spec in STEP_CATALOGUE:
        if only is not None and spec.step_id not in only:
            continue
        if spec.source == "board" and board is None:
            continue
        if spec.source == "schematic" and schematic is None:
            continue
        chosen.append(spec)
    return tuple(chosen)


def run_step_catalogue(
    *,
    closure_root: Path,
    board_rel: str,
    schematic_rel: str | None,
    output_root: Path,
    variant: str = "",
    tool: KicadTool | None,
    only: Sequence[str] | None = None,
    timeout_seconds: int = DEFAULT_CLI_TIMEOUT_SECONDS,
    progress: Any = None,
    on_step: Any = None,
    bom_preset: str = "",
    board_path: Path | None = None,
    schematic_path: Path | None = None,
    board_label: str | None = None,
    schematic_label: str | None = None,
    project_dir: Path | None = None,
) -> tuple[StepOutput, ...]:
    """Run the catalogue against the design files with ``tool`` (kicad-cli).

    ``project_dir`` is a scratch project directory holding the board (named
    after its project file), the project file and the schematics. With it the
    board steps run there: together as one jobset when :func:`run_board_jobset`
    can, and DRC with schematic parity whenever a schematic is configured.

    ``board_path``/``schematic_path`` name the files the tool opens when they
    are not inside the materialized closure (the bundle's ``refilled/`` board
    and ``sources/`` tree, C3c).  ``board_label``/``schematic_label`` are the
    stable closure-independent names recorded in the argv; they default to the
    configured repository paths.
    """

    if tool is None:
        raise StepExecutionError("kicad-cli is not available on PATH")
    if board_path is not None:
        board = Path(board_path)
    else:
        board = (closure_root / board_rel) if board_rel else None
    if schematic_path is not None:
        schematic = Path(schematic_path)
    else:
        schematic = (closure_root / schematic_rel) if schematic_rel else None
    if board is not None and not board.is_file():
        raise StepExecutionError(f"board not found in closure: {board_rel}")
    if schematic is not None and not schematic.is_file():
        # A configured schematic that is not there is a broken input, not a
        # board-only project: dropping ERC and BOM silently shipped a release
        # without either (EDA-04903, 2026-10-23).
        if selected_steps(board=None, schematic=schematic, only=only):
            raise StepExecutionError(f"schematic not found in closure: {schematic_rel or schematic}")
        schematic = None

    specs = selected_steps(board=board, schematic=schematic, only=only)
    if not specs:
        return ()

    board_label = board_label or board_rel
    schematic_label = schematic_label or (schematic_rel or "")
    parity = schematic is not None and project_dir is not None
    if project_dir is not None and board is not None:
        board = project_dir / board.name
    results: dict[str, StepOutput] = {}
    batched = [spec for spec in specs if spec.step_id in JOBSET_SETTINGS]
    if project_dir is not None and board is not None and len(batched) > 1 and not (
        variant and variant.casefold() != "default"
    ):
        for spec in batched:
            print(f"[{spec.step_id}] Running {_catalogue_title(spec)} (board jobset)", flush=True)
            if on_step is not None:
                on_step(spec.step_id, "in_progress", message=f"Running {_catalogue_title(spec)}")
        for output in run_board_jobset(
            batched,
            tool=tool,
            project_dir=project_dir,
            board=board,
            output_root=output_root,
            board_label=board_label,
            parity=parity,
            timeout_seconds=timeout_seconds * len(batched),
        ):
            results[output.step_id] = output
            if on_step is not None:
                print(f"[{output.step_id}] Finished {_catalogue_title(output.spec)}", flush=True)
                on_step(output.step_id, "success", elapsed_ms=output.elapsed_ms, log=output.stdout, message=f"Finished {_catalogue_title(output.spec)}")
        # A step the jobset did not write runs on its own below.
        specs_to_run = [spec for spec in specs if spec.step_id not in results]
    else:
        specs_to_run = list(specs)
    # Every step is an independent `kicad-cli` process. They used to overlap;
    # `sch export bom` then died with std::bad_alloc / basic_string::_M_create
    # while DRC/ERC still held the design. One process at a time.
    run = partial(
        _run_one,
        tool=tool,
        closure_root=closure_root,
        board=board,
        schematic=schematic,
        board_rel=board_label,
        schematic_rel=schematic_label,
        output_root=output_root,
        variant=variant,
        timeout_seconds=timeout_seconds,
        bom_preset=bom_preset,
        parity=parity,
    )
    completed = len(results)

    with ThreadPoolExecutor(max_workers=max(1, min(len(specs_to_run), _MAX_PARALLEL_STEPS))) as pool:
        futures = {}
        for spec in specs_to_run:
            title = _catalogue_title(spec)
            print(f"[{spec.step_id}] Running {title}", flush=True)
            if on_step is not None:
                on_step(spec.step_id, "in_progress", message=f"Running {title}")
            futures[pool.submit(run, spec)] = spec
        for future in as_completed(futures):
            spec = futures[future]
            completed += 1
            title = _catalogue_title(spec)
            if progress is not None:
                progress(
                    stage="generate",
                    message=f"Finished {title} ({completed}/{len(specs)})",
                    percent=10 + int(60 * completed / len(specs)),
                )
            # A failing step still raises, exactly as it did serially; the
            # pool's context manager lets the others finish first so a retry
            # is not starting from nothing.
            try:
                output = future.result()
            except Exception as exc:
                if on_step is not None:
                    on_step(spec.step_id, "failure", message=str(exc))
                raise
            results[spec.step_id] = output
            if on_step is not None:
                log = "\n".join(
                    part for part in (output.stdout, output.stderr) if part
                )
                status = "skipped" if output.skipped_reason else "success"
                done = output.skipped_reason or f"Finished {title}"
                print(f"[{spec.step_id}] {done}", flush=True)
                on_step(
                    spec.step_id,
                    status,
                    elapsed_ms=output.elapsed_ms,
                    log=log,
                    message=done,
                )
    # Catalogue order, not completion order: the dossier's member ordering and
    # every digest built from it have to be independent of scheduling.
    return tuple(results[spec.step_id] for spec in specs)


def _run_one(
    spec: StepSpec,
    *,
    tool: KicadTool,
    closure_root: Path,
    board: Path | None,
    schematic: Path | None,
    board_rel: str,
    schematic_rel: str,
    output_root: Path,
    variant: str,
    timeout_seconds: int,
    bom_preset: str = "",
    parity: bool = False,
) -> StepOutput:
    destination = output_root / spec.output_name
    if spec.output_kind == "dir":
        destination.mkdir(parents=True, exist_ok=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)

    substitutions = {
        "board": str(board) if board else "",
        "schematic": str(schematic) if schematic else "",
        "out": str(destination),
        "variant": variant,
    }
    # The recorded argv must be host-path independent: it feeds the domain
    # fingerprints, so an identical build from a different checkout directory
    # has to normalize to identical text.
    normalized_substitutions = {
        "board": board_rel,
        "schematic": schematic_rel,
        "out": spec.output_name,
        "variant": variant,
    }
    argv = [part.format(**substitutions) for part in spec.argv]
    normalized = ["kicad-cli", *(part.format(**normalized_substitutions) for part in spec.argv)]
    if spec.variant_flag and variant and variant.casefold() != "default":
        argv.extend(("--variant", variant))
        normalized.extend(("--variant", variant))
    if spec.step_id == "bom":
        extra = _bom_extra_argv(bom_preset)
        argv.extend(extra)
        normalized.extend(extra)
    if spec.step_id == "drc" and parity:
        # The board runs beside its project and schematics, so DRC also checks
        # schematic parity, as the jobset does.
        argv.insert(-1, "--schematic-parity")
        normalized.insert(-1, "--schematic-parity")

    started = time.perf_counter()
    print(f"[{spec.step_id}] $ {shlex.join([tool.executable(), *argv])}", flush=True)
    try:
        # Tee'd to the worker stdout: that is the build's live log.
        result = tool.run(argv, cwd=closure_root, timeout=timeout_seconds, tee=True)
    except ToolError as exc:
        raise StepExecutionError(str(exc)) from exc
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    files = _collect_outputs(destination, spec)
    # A directory export writes one file per layer, so "some output exists" says
    # nothing about completeness: a gerber run that dies after F.Cu leaves a
    # non-empty directory behind. Only a single-file step -- DRC and ERC, which
    # exit non-zero precisely *because* they wrote a report of violations -- may
    # treat a produced file as success despite the exit code.
    partial_directory = spec.output_kind == "dir" and result.returncode != 0
    if result.returncode != 0 and (not files or partial_directory):
        message = (result.stderr or result.stdout or "").strip()
        if spec.optional:
            return StepOutput(
                step_id=spec.step_id,
                step_type=spec.step_type,
                normalized_argv=tuple(normalized),
                returncode=result.returncode,
                files=(),
                root=output_root,
                spec=spec,
                stdout=result.stdout,
                stderr=result.stderr,
                skipped_reason=f"optional step failed: {message[:400]}",
                elapsed_ms=elapsed_ms,
            )
        raise StepExecutionError(
            f"step {spec.step_id} ({spec.step_type}) failed with exit code "
            f"{result.returncode}: {message[:800]}"
        )
    return StepOutput(
        step_id=spec.step_id,
        step_type=spec.step_type,
        normalized_argv=tuple(normalized),
        returncode=result.returncode,
        files=files,
        root=output_root,
        spec=spec,
        stdout=result.stdout,
        stderr=result.stderr,
        elapsed_ms=elapsed_ms,
    )


def run_board_jobset(
    specs: Sequence[StepSpec],
    *,
    tool: KicadTool,
    project_dir: Path,
    board: Path,
    output_root: Path,
    board_label: str,
    parity: bool,
    timeout_seconds: int,
) -> tuple[StepOutput, ...]:
    """The board steps from one ``kicad-cli jobset run``: one board load, not one per step.

    Each job gets its own output folder under :data:`JOBSET_DIR`, and its files
    move to the step's usual destination afterwards (positions come out as
    ``<name>-all-pos.csv``; the CLI's "naked filename" has no jobset setting).
    Returns the steps whose output appeared. KiCad drops a jobset it cannot
    read without failing, so a missing step is the caller's to run on its own.
    The recorded argv names the job type and its exact settings, host-independent.
    """

    project = project_dir / f"{board.stem}.kicad_pro"
    if not project.is_file():
        return ()
    work = project_dir / JOBSET_DIR
    remove_tree(work, ignore_errors=True)
    jobs, outputs, settings_of = [], [], {}
    for spec in specs:
        settings = dict(JOBSET_SETTINGS[spec.step_id])
        if spec.step_id == "drc":
            settings["parity"] = parity
        settings_of[spec.step_id] = settings
        job_id = str(uuid.uuid5(_JOBSET_NAMESPACE, f"job:{spec.step_id}"))
        output_key = {"output_dir": ""} if spec.output_kind == "dir" else {"output_filename": Path(spec.output_name).name}
        jobs.append({"description": spec.step_id, "id": job_id, "type": spec.step_type,
                     "settings": {"description": spec.step_id, **settings, **output_key}})
        outputs.append({"description": spec.step_id, "id": str(uuid.uuid5(_JOBSET_NAMESPACE, f"output:{spec.step_id}")),
                        "only": [job_id], "settings": {"output_path": f"./{JOBSET_DIR}/{spec.step_id}"}, "type": "folder"})
    work.mkdir(parents=True)
    jobset = work / "release.kicad_jobset"
    jobset.write_text(json.dumps({"jobs": jobs, "meta": {"version": 1}, "outputs": outputs}, indent=2, sort_keys=True), encoding="utf-8")

    # The runner stages each output in a folder under the system temp
    # directory and never removes it, so it gets a temp directory of its own.
    temp = work / "tmp"
    temp.mkdir()
    argv = ["jobset", "run", "-f", str(jobset), str(project)]
    started = time.perf_counter()
    print(f"[board-jobset] $ {shlex.join([tool.executable(), *argv])}", flush=True)
    try:
        result = tool.run(argv, cwd=project_dir, env={"TMPDIR": str(temp)}, timeout=timeout_seconds, tee=True)
    except ToolError as exc:
        print(f"[board-jobset] {exc}; running the steps one by one", flush=True)
        return ()
    finally:
        remove_tree(temp, ignore_errors=True)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    done: list[StepOutput] = []
    for spec in specs:
        produced = sorted((path for path in (work / spec.step_id).rglob("*") if path.is_file()), key=lambda path: path.as_posix())
        destination = output_root / spec.output_name
        if spec.output_kind == "dir":
            if not produced:
                continue
            destination.mkdir(parents=True, exist_ok=True)
            for path in produced:
                target = destination / path.relative_to(work / spec.step_id)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), target)
        else:
            if len(produced) != 1:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(produced[0]), destination)
        settings = json.dumps(settings_of[spec.step_id], sort_keys=True, separators=(",", ":"))
        done.append(
            StepOutput(
                step_id=spec.step_id,
                step_type=spec.step_type,
                normalized_argv=("kicad-cli", "jobset", "run", spec.step_type, settings, board_label),
                returncode=result.returncode,
                files=_collect_outputs(destination, spec),
                root=output_root,
                spec=spec,
                stdout=result.stdout,
                stderr=result.stderr,
                elapsed_ms=elapsed_ms,
            )
        )
    remove_tree(work, ignore_errors=True)
    return tuple(done)


def gerber_manifest_gaps(files: Sequence[Path]) -> tuple[bool, tuple[str, ...]]:
    """Check a Gerber export against the ``.gbrjob`` manifest it wrote.

    Returns ``(manifest_found, missing_paths)``.

    KiCad writes the job file last and lists every plot it produced under
    ``FilesAttributes``, so the manifest is the export's own statement of what
    the set should contain. Comparing against it beats an allowlist of layer
    names: copper layers are renamed freely by the designer -- the boards in
    this repository plot ``TOP``/``GND1``/``BOT`` rather than ``F_Cu``/``B_Cu``
    -- so a fixed expected set would be wrong for most real projects.
    """

    present = {path.name for path in files}
    job = next((path for path in files if path.suffix.casefold() == ".gbrjob"), None)
    if job is None:
        return False, ()
    try:
        manifest = json.loads(job.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return False, ()
    attributes = manifest.get("FilesAttributes") if isinstance(manifest, Mapping) else None
    if not isinstance(attributes, list):
        return False, ()
    missing = [
        str(entry["Path"])
        for entry in attributes
        if isinstance(entry, Mapping)
        and str(entry.get("Path") or "")
        and str(entry["Path"]) not in present
    ]
    return True, tuple(sorted(missing))


def _collect_outputs(destination: Path, spec: StepSpec) -> tuple[Path, ...]:
    if spec.output_kind == "file":
        return (destination,) if destination.is_file() else ()
    if not destination.is_dir():
        return ()
    return tuple(
        sorted(
            (path for path in destination.rglob("*") if path.is_file()),
            key=lambda path: path.as_posix(),
        )
    )


def evidence_counts(report: Mapping[str, Any]) -> dict[str, int]:
    """Count violations by severity in a canonicalized DRC/ERC report.

    KiCad nests violations under several keys and, for ERC, under per-sheet
    lists.  Everything is folded into one severity histogram plus a total.
    """

    counts: dict[str, int] = {}
    total = 0
    for item in _iter_violations(report):
        if item.get("excluded") or item.get("ignored") or item.get("waived"):
            continue
        severity = str(item.get("severity") or "unknown").strip().lower() or "unknown"
        counts[severity] = counts.get(severity, 0) + 1
        total += 1
    counts["total"] = total
    return counts


def _iter_violations(report: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for key in ("violations", "unconnected_items", "schematic_parity"):
        for item in report.get(key) or ():
            if isinstance(item, Mapping):
                yield item
    for sheet in report.get("sheets") or ():
        if isinstance(sheet, Mapping):
            for item in sheet.get("violations") or ():
                if isinstance(item, Mapping):
                    yield item


__all__ = [
    "EVIDENCE_STEPS",
    "STEP_BY_ID",
    "STEP_CATALOGUE",
    "StepExecutionError",
    "StepOutput",
    "StepSpec",
    "evidence_counts",
    "gerber_manifest_gaps",
    "run_step_catalogue",
    "run_board_jobset",
    "selected_steps",
]
