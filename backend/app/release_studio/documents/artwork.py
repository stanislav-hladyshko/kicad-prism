"""Board artwork acquisition and placement (D3).

Prism never re-implements KiCad's plotter.  Artwork is acquired by running
``kicad-cli`` and then *placed*: the SVG backend inlines the emitted geometry
inside a transformed group, and the PDF backend composites KiCad's own PDF page
onto the composed sheet with pikepdf.  In both cases the geometry that reaches
the released drawing is exactly the geometry KiCad produced.

The scale contract is explicit and testable.  ``kicad-cli pcb export svg``
emits a user-unit-per-millimetre document, so a placement at scale *s* means
one board millimetre occupies *s* sheet millimetres; a sheet claiming 1:1 must
measure 50.000 mm across a 50.000 mm board feature.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.core.config import settings
from app.release_studio.documents import render_cache
from app.release_studio.documents.layout import Artwork, Rect
from app.services.kicad_tools import KicadTool, ToolError, ToolResult, locate_kicad_cli

#: Layer and drill-map plots: bounded so a wedged plot fails one sheet.
PLOT_TIMEOUT_SECONDS = 600


def _run_tool(tool: KicadTool, args: Sequence[str], timeout: float, **kwargs: Any) -> ToolResult:
    """Run one acquisition; a tool failure or timeout costs this view only."""

    try:
        return tool.run(args, timeout=timeout, **kwargs)
    except ToolError as exc:
        raise ArtworkError(str(exc)) from exc

_SVG_OPEN = re.compile(r"<svg\b[^>]*>", re.IGNORECASE | re.DOTALL)
_SVG_CLOSE = re.compile(r"</svg\s*>\s*$", re.IGNORECASE)

# `kicad-cli pcb export svg` writes
#   <title>SVG Image created as board.svg date 2026-08-11T17:09:53 </title>
# which carries both the output filename and the plot time. Inlining that into
# a composed sheet makes the sheet vary per build, and hashing it makes the
# clip-path id vary too -- so it is removed at acquisition rather than left for
# a downstream canonicalizer to catch in a nested position.
_KICAD_TITLE = re.compile(
    r"<title>\s*SVG Image created as\b.*?</title>", re.IGNORECASE | re.DOTALL
)
_DATED_COMMENT = re.compile(
    r"<!--(?:(?!-->).)*?(?:date|created|generated|timestamp)(?:(?!-->).)*-->",
    re.IGNORECASE | re.DOTALL,
)


def sanitize_artwork_pdf(payload: bytes) -> bytes:
    """Strip plot-time metadata from an acquired artwork PDF.

    KiCad writes ``/Producer (KiCad PDF)`` and ``/CreationDate``; with those
    removed its output is byte-identical across runs, which is what lets a
    composed sheet carrying that artwork have a stable released digest.
    """

    import pikepdf

    try:
        with pikepdf.open(io.BytesIO(payload)) as document:
            for key in list(document.docinfo.keys()):
                del document.docinfo[key]
            with document.open_metadata(set_pikepdf_as_editor=False) as meta:
                meta.clear()
            out = io.BytesIO()
            document.save(out, deterministic_id=True, linearize=False)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - an unreadable overlay is handled upstream
        return payload


def sanitize_artwork(svg_text: str) -> str:
    """Remove plot-time metadata from acquired artwork.

    Semantically null by construction: it deletes a title and comments, never
    geometry, so the placed drawing is the same drawing.
    """

    cleaned = _KICAD_TITLE.sub("", svg_text)
    return _DATED_COMMENT.sub("", cleaned)
_VIEWBOX = re.compile(r'viewBox\s*=\s*"([^"]+)"', re.IGNORECASE)
_WIDTH = re.compile(r'(?<![-\w])width\s*=\s*"([0-9.]+)([a-z]*)"', re.IGNORECASE)
_HEIGHT = re.compile(r'(?<![-\w])height\s*=\s*"([0-9.]+)([a-z]*)"', re.IGNORECASE)

# `kicad-cli pcb export svg` writes user units per millimetre; anything else
# would make the scale contract below wrong, so it is asserted, not assumed.
_UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72.0, "": 1.0}


class ArtworkError(RuntimeError):
    """Artwork could not be acquired or understood."""


@dataclass(frozen=True, slots=True)
class AcquiredArtwork:
    """One plotted view of the board, with the geometry needed to place it.

    Two coordinate frames are recorded because ``kicad-cli`` gives the two
    formats different pages.  ``pcb export svg --page-size-mode 2`` crops to the
    plotted content, so the SVG's frame *is* the artwork's extent.  ``pcb export
    pdf`` has no such option -- verified against the pinned 10.0.4 CLI -- so its
    page is the board's configured page size with the artwork sitting somewhere
    inside it.  ``page_offset_*`` locates the artwork within that page, which is
    what lets the PDF composite land at the same scale and position as the SVG
    instead of shrinking the whole page into the artwork's window.
    """

    layers: tuple[str, ...]
    svg_text: str
    pdf_bytes: bytes
    #: The artwork's own extents in millimetres.
    view_x: float
    view_y: float
    view_width: float
    view_height: float
    digest: str
    #: Where the artwork's top-left sits on the PDF page, in millimetres from
    #: that page's top-left.  ``None`` when it could not be established, in
    #: which case the PDF composite is skipped rather than placed wrongly.
    page_offset_x: float | None = None
    page_offset_y: float | None = None

    @property
    def body(self) -> str:
        """The artwork's contents with its own ``<svg>`` wrapper removed."""

        opened = _SVG_OPEN.search(self.svg_text)
        if opened is None:
            raise ArtworkError("acquired artwork is not an SVG document")
        inner = self.svg_text[opened.end():]
        return _SVG_CLOSE.sub("", inner).strip()


def acquire(
    cli: KicadTool,
    board: Path,
    layers: Sequence[str],
    workdir: Path,
    *,
    variant: str = "",
    black_and_white: bool = True,
) -> AcquiredArtwork:
    """Plot *layers* of *board* to SVG and PDF via ``kicad-cli``.

    Both formats come from the same invocation set so the SVG placed in an SVG
    sheet and the PDF composited into a PDF sheet show the same geometry.
    ``--exclude-drawing-sheet`` matters: the composed sheet supplies its own
    frame, and KiCad's would collide with it.
    """

    workdir.mkdir(parents=True, exist_ok=True)
    stem = "-".join(layer.replace(".", "_") for layer in layers) or "board"
    svg_path = workdir / f"{stem}.svg"
    page_svg_path = workdir / f"{stem}-page.svg"
    pdf_path = workdir / f"{stem}.pdf"

    # Three plots of one view.  The cropped SVG is the artwork; the PDF is what
    # gets composited; the full-page SVG exists only to locate the artwork
    # inside the PDF's page, because `pcb export pdf` cannot crop.  They share
    # no output path, so they run together rather than queueing three board
    # loads behind each other.
    plots = (
        ("svg", svg_path, ("--exclude-drawing-sheet", "--page-size-mode", "2")),
        ("svg", page_svg_path, ("--exclude-drawing-sheet",)),
        ("pdf", pdf_path, ()),
    )

    def _plot(fmt: str, out: Path, extra: tuple[str, ...]) -> None:
        argv = [
            "pcb", "export", fmt,
            "--layers", ",".join(layers),
            # `--mode-single` makes `--output` a file rather than a directory.
            "--mode-single",
            "--output", str(out),
        ]
        # Verified against the pinned 10.0.4 CLI: `pcb export svg` takes
        # `--exclude-drawing-sheet` and `--page-size-mode`, while `pcb export
        # pdf` takes neither and omits the border unless asked.  There is no
        # `--svgprecision` on `pcb export` at all -- that flag belongs to the
        # schematic exporter.
        argv.extend(extra)
        if black_and_white:
            argv.append("--black-and-white")
        if variant:
            argv.extend(["--variant", variant])
        argv.append(str(board))

        result = _run_tool(cli, argv, PLOT_TIMEOUT_SECONDS)
        if not result.ok:
            # kicad-cli reports argument errors on stdout; detail() keeps both.
            raise ArtworkError(
                f"kicad-cli pcb export {fmt} failed for layers {','.join(layers)}: "
                f"{result.detail()[:400]}"
            )
        if not out.is_file():
            raise ArtworkError(f"kicad-cli produced no {fmt} for layers {','.join(layers)}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_plot, fmt, out, extra) for fmt, out, extra in plots]
        for future in futures:
            future.result()

    return _acquired(tuple(layers), svg_path, page_svg_path, pdf_path)


def acquire_layers(
    cli: KicadTool,
    board: Path,
    layers: Sequence[str],
    workdir: Path,
    *,
    variant: str = "",
    black_and_white: bool = True,
) -> dict[str, AcquiredArtwork | ArtworkError]:
    """Every layer page's artwork from one board load per format.

    :func:`acquire` costs three ``kicad-cli`` processes per layer, and each
    loads the board: about forty loads for a twelve-layer board, minutes each
    on a 50 MB one. Here each format is one process: ``--mode-multi`` (SVG)
    and ``--mode-separate`` (PDF) write a file per layer with ``Edge.Cuts`` as
    a common layer, so every plot can still be located on the board.

    Keyed by layer. A layer whose files did not appear maps to its
    :class:`ArtworkError`; a failed invocation raises, and the caller falls
    back to :func:`acquire` per layer.
    """

    workdir.mkdir(parents=True, exist_ok=True)
    crop_dir, page_dir, pdf_dir = workdir / "svg", workdir / "page", workdir / "pdf"
    plots = (
        ("svg", crop_dir, ("--mode-multi", "--exclude-drawing-sheet", "--page-size-mode", "2")),
        ("svg", page_dir, ("--mode-multi", "--exclude-drawing-sheet")),
        ("pdf", pdf_dir, ("--mode-separate",)),
    )

    def _plot(fmt: str, out: Path, extra: tuple[str, ...]) -> None:
        out.mkdir(parents=True, exist_ok=True)
        argv = [
            "pcb", "export", fmt,
            "--layers", ",".join(layers),
            "--common-layers", "Edge.Cuts",
            "--output", str(out),
            *extra,
        ]
        if black_and_white:
            argv.append("--black-and-white")
        if variant:
            argv.extend(["--variant", variant])
        argv.append(str(board))
        result = _run_tool(cli, argv, PLOT_TIMEOUT_SECONDS)
        if not result.ok:
            raise ArtworkError(
                f"kicad-cli pcb export {fmt} failed for layers {','.join(layers)}: "
                f"{result.detail()[:400]}"
            )

    with ThreadPoolExecutor(max_workers=len(plots)) as pool:
        futures = [pool.submit(_plot, fmt, out, extra) for fmt, out, extra in plots]
        for future in futures:
            future.result()

    acquired: dict[str, AcquiredArtwork | ArtworkError] = {}
    plotted_names = plot_file_layer_names(board)
    for layer in layers:
        name = f"{board.stem}-{plotted_names.get(layer, layer).replace('.', '_')}"
        files = (crop_dir / f"{name}.svg", page_dir / f"{name}.svg", pdf_dir / f"{name}.pdf")
        missing = [path.name for path in files if not path.is_file()]
        if missing:
            acquired[layer] = ArtworkError(f"kicad-cli produced no {', '.join(missing)} for layer {layer}")
            continue
        plotted = ("Edge.Cuts", layer) if layer != "Edge.Cuts" else ("Edge.Cuts",)
        acquired[layer] = _acquired(plotted, *files)
    return acquired


#: KiCad's pre-v7 canonical layer names, as an older board's layer table still
#: spells them, against the names plots are requested by.
_LEGACY_LAYER_NAMES = {
    "F.SilkS": "F.Silkscreen",
    "B.SilkS": "B.Silkscreen",
    "F.Adhes": "F.Adhesive",
    "B.Adhes": "B.Adhesive",
    "F.CrtYd": "F.Courtyard",
    "B.CrtYd": "B.Courtyard",
    "Dwgs.User": "User.Drawings",
    "Cmts.User": "User.Comments",
    "Eco1.User": "User.Eco1",
    "Eco2.User": "User.Eco2",
}
_LAYER_ROW = re.compile(r'\(\s*\d+\s+"((?:[^"\\]|\\.)*)"\s+[\w.]+(?:\s+"((?:[^"\\]|\\.)*)")?\s*\)')


def plot_file_layer_names(board: Path) -> dict[str, str]:
    """The name KiCad gives each layer's file in a multi-file plot, by requested layer.

    KiCad suffixes the file with the layer's display name: the board's own
    name for it when the layer table sets one (EDA-04903 calls F.Cu "Top
    Layer"), else the canonical name. Dots become underscores, spaces stay. A
    layer missing here keeps its requested name.
    """

    try:
        with board.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(256 * 1024)
    except OSError:
        return {}
    start = head.find("(layers")
    if start < 0:
        return {}
    depth, end = 0, len(head)
    for index in range(start, len(head)):
        if head[index] == "(":
            depth += 1
        elif head[index] == ")":
            depth -= 1
            if depth == 0:
                end = index
                break
    names: dict[str, str] = {}
    for canonical, user in _LAYER_ROW.findall(head[start + 1 : end]):
        requested = _LEGACY_LAYER_NAMES.get(canonical, canonical)
        names[requested] = user or requested
    return names


def _acquired(layers: tuple[str, ...], svg_path: Path, page_svg_path: Path, pdf_path: Path) -> AcquiredArtwork:
    svg_text = sanitize_artwork(svg_path.read_text(encoding="utf-8"))
    pdf_bytes = sanitize_artwork_pdf(pdf_path.read_bytes())
    x, y, width, height = extents(svg_text)
    offset = page_offset(svg_text, page_svg_path.read_text(encoding="utf-8"))
    return AcquiredArtwork(
        layers=layers,
        svg_text=svg_text,
        pdf_bytes=pdf_bytes,
        view_x=x,
        view_y=y,
        view_width=width,
        view_height=height,
        # Hashed *after* sanitizing: this digest becomes the clip-path id in the
        # composed sheet, so a volatile input here would move the sheet even
        # when the geometry is identical.
        digest=hashlib.sha256(svg_text.encode("utf-8")).hexdigest(),
        page_offset_x=offset[0] if offset else None,
        page_offset_y=offset[1] if offset else None,
    )


_PATH_DATA = re.compile(r'\bd\s*=\s*"([^"]*)"', re.IGNORECASE)
# Each subpath's move-to, whatever separator the plotter used.  KiCad writes
# `M 0.0000,0.0000` for filled shapes and `M126.0000 90.0000` for strokes, so a
# comma-only reader finds nothing at all on an outline-only plot -- which is
# exactly the drill sheet.
_MOVE_TO = re.compile(r"[Mm]\s*(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)")


def _coordinate_pairs(svg_text: str, limit: int = 64) -> list[tuple[float, float]]:
    """The first *limit* subpath origins in the document's path data.

    Move-to points are enough: the two plots being compared contain the same
    subpaths in the same order, so their origins differ by exactly the crop
    translation and nothing else has to be parsed.
    """

    pairs: list[tuple[float, float]] = []
    for match in _PATH_DATA.finditer(svg_text):
        for pair in _MOVE_TO.finditer(match.group(1)):
            pairs.append((float(pair.group(1)), float(pair.group(2))))
            if len(pairs) >= limit:
                return pairs
    return pairs


#: Assembly view name per assembly side.  The names are the native artwork
#: configuration's view ids; the testpoint ones are the second config's.
ASSEMBLY_VIEWS: dict[str, str] = {
    "top": "assembly_top_view",
    "bottom": "assembly_bottom_view",
}

#: The same board with only the testpoints labelled.
TESTPOINT_VIEWS: dict[str, str] = {
    "top": "testpoint_top_view",
    "bottom": "testpoint_bottom_view",
}

#: What the artwork *actually* drew each component from.
#:
#: Deliberately not ``data-projection``, which records the mode the config
#: asked for and is therefore the same string on every component whether or not
#: a model resolved.  ``data-bounds-kind`` is the outcome -- ``model``,
#: ``holes`` or ``pads`` -- which is the only one of the two that can tell a
#: reader whether this is the HLR drawing the sheet claims to be.
_BOUNDS_KIND_ATTR = re.compile(r'\bdata-bounds-kind\s*=\s*"([^"]+)"', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DnpStyle:
    """How the artwork draws a do-not-populate part: colour and hatch."""

    colour: str
    hatch: bool
    spacing: float
    angle: float
    line_width: float


def dnp_style(config_path: Path | None = None) -> DnpStyle:
    """The DNP swatch, read from the same config the artwork consumes.

    The assembly legend sits under a drawing it has to agree with, so its
    colour and hatch come from the native artwork configuration rather than
    from a second constant that could drift.
    """

    import json

    if config_path is None:
        from app.release_studio.documents.artwork_bundle import (
            ASSEMBLY_CONFIG,
            config_path as native_config_path,
        )

        config_path = native_config_path(ASSEMBLY_CONFIG)
    config = config_path
    try:
        document = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ArtworkError(f"the artwork configuration is unreadable: {config}") from error
    dnp = document.get("dnp") or {}
    missing = [
        key
        for key in ("color", "hatch_spacing_mm", "hatch_angle_deg", "hatch_line_width_mm")
        if key not in dnp
    ]
    if missing:
        raise ArtworkError(
            "the pcb-svg configuration is missing the DNP fields: " + ", ".join(missing)
        )
    return DnpStyle(
        colour=str(dnp["color"]),
        hatch=bool(dnp.get("hatch", False)),
        spacing=float(dnp["hatch_spacing_mm"]),
        angle=float(dnp["hatch_angle_deg"]),
        line_width=float(dnp["hatch_line_width_mm"]),
    )


#: Sides with at least this many placements get a density warning.  Density is
#: scale-invariant: past this, one sheet cannot carry every designator legibly,
#: and detail/zone sheets are the real answer (not yet generated).
DENSE_PLACEMENT_WARN = 400


def assembly_projection_mix(svg_text: str) -> dict[str, int]:
    """Count what each component in one assembly SVG was actually drawn from.

    ``model`` is the 3D model's own silhouette -- the drawing this sheet is
    meant to be.  ``holes`` and ``pads`` are the rectangular fallbacks Cruncher
    substitutes, per component, when no model resolves for that footprint.
    """

    counts: dict[str, int] = {}
    for match in _BOUNDS_KIND_ATTR.finditer(svg_text or ""):
        key = match.group(1).strip() or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def assembly_projection_warnings(side: str, mix: Mapping[str, int]) -> list[str]:
    """Warn when components fell back to a bounding box instead of the model.

    The fallback is correct behaviour, not a failure -- a part with no 3D model
    has no silhouette to draw.  It is still worth stating: a reader comparing
    the sheet against the boards in front of them should know which outlines
    are the part and which are a box around it.
    """

    total = sum(mix.values())
    if total <= 0:
        return []
    boxed = int(mix.get("pads") or 0) + int(mix.get("holes") or 0)
    if boxed <= 0:
        return []
    if boxed == total:
        return [
            f"assembly {side}: no component resolved a 3D model; all {total} are "
            "drawn as bounding boxes (check that the models are embedded or that "
            "${KIPRJMOD} resolves)"
        ]
    if boxed / total >= 0.10:
        return [
            f"assembly {side}: {boxed}/{total} components have no 3D model and are "
            "drawn as bounding boxes"
        ]
    return []


def assembly_density_warnings(side: str, placement_count: int) -> list[str]:
    """Warn when one sheet cannot honestly carry every designator."""

    if placement_count < DENSE_PLACEMENT_WARN:
        return []
    return [
        f"assembly {side}: {placement_count} placements — designators may be "
        "incomplete at any scale; detail/zone sheets are not yet generated; "
        "positions.csv remains authoritative"
    ]


@dataclass(frozen=True, slots=True)
class BoardRender:
    """A raytraced isometric picture of the board, for the cover."""

    png_bytes: bytes
    width_px: int
    height_px: int
    digest: str

    @property
    def aspect(self) -> float:
        return (self.width_px / self.height_px) if self.height_px else 1.0


def acquire_board_render(
    cli: KicadTool,
    board: Path,
    workdir: Path,
    *,
    width: int = 1200,
    height: int = 900,
    timeout_seconds: int = 600,
) -> BoardRender:
    """Render the board the way the project thumbnail is rendered.

    `kicad-cli pcb render` is KiCad's own raytracer, and this is deliberately
    the same invocation `project_import_service.generate_thumbnail_for_project`
    uses -- same quality, floor, perspective and rotation -- so the picture on
    a release cover is recognisably the picture of the project.

    The result is cached by the pixels' inputs (see ``render_cache``): the same
    board rendered into another build directory is a cache hit, and a hit does
    not run the raytracer at all.  The cache also makes a repeat build
    reproducible, because KiCad's sampler is not.
    """

    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "board.png"
    argv = [
        "pcb", "render",
        "--quality", "high",
        "--floor",
        "--perspective",
        "--rotate", "-45,0,45",
        "--width", str(width),
        "--height", str(height),
        "-o", str(out),
        str(board),
    ]
    # The key is the command with the two host paths replaced by markers: the
    # output path and the board's location are not render inputs, its bytes are.
    signature = [
        "<board>" if part == str(board) else "<output>" if part == str(out) else part
        for part in argv
    ]
    key = render_cache.cache_key(
        board=board,
        render_args=signature,
        cli_version=cli.version(),
        environ=os.environ,
    )
    cached = render_cache.load(key)
    if cached is not None:
        try:
            return _board_render(cached)
        except Exception:  # noqa: BLE001 - an unusable entry is a cache miss
            pass

    result = _run_tool(cli, argv, timeout_seconds)
    if not result.ok or not out.is_file():
        raise ArtworkError(f"kicad-cli pcb render failed: {result.detail()[:400]}")

    payload = out.read_bytes()
    render = _board_render(payload)
    try:
        render_cache.store(key, payload)
    except OSError:
        # A cache that cannot be written must not cost the render that succeeded.
        pass
    return render


def _board_render(payload: bytes) -> BoardRender:
    """A ``BoardRender`` carrying *payload*, its decoded size and its digest."""

    from PIL import Image as PILImage

    with PILImage.open(io.BytesIO(payload)) as image:
        image.load()
        size = image.size
    return BoardRender(
        png_bytes=payload,
        width_px=int(size[0]),
        height_px=int(size[1]),
        digest=hashlib.sha256(payload).hexdigest(),
    )


def render_pdf_page(svg_text: str) -> bytes:
    """Render *svg_text* to a one-page PDF whose page is the SVG's viewport.

    Used where the producing tool emits SVG only.  cairo draws the document it
    is given -- paths stay paths and text stays text -- so this is a change of
    container, not of content, and the PDF sheet shows the same drawing the SVG
    sheet does.
    """

    _configure_cairo_library_path()
    import cairosvg

    try:
        return cairosvg.svg2pdf(bytestring=svg_text.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any cairo failure is one failure here
        raise ArtworkError(f"the assembly view could not be rendered to PDF: {exc}") from exc


def _configure_cairo_library_path() -> None:
    """Make Homebrew Cairo visible to uv's portable macOS Python.

    Framework Python happens to search Homebrew's library directory, while the
    standalone interpreter managed by uv does not. Without this explicit path,
    an otherwise identical locked venv can build SVG documents but fails only
    when Release Studio asks CairoSVG for the matching PDF page.
    """

    if sys.platform == "win32":
        _add_windows_cairo_directory()
        return
    if sys.platform != "darwin":
        return
    candidates = [settings.PRISM_CAIRO_LIBRARY_DIR, "/opt/homebrew/lib", "/usr/local/lib"]
    library_dir = next(
        (
            Path(candidate)
            for candidate in candidates
            if candidate and (Path(candidate) / "libcairo.2.dylib").is_file()
        ),
        None,
    )
    if library_dir is None:
        return
    variable = "DYLD_FALLBACK_LIBRARY_PATH"
    current = os.environ.get(variable, "")
    entries = [entry for entry in current.split(os.pathsep) if entry]
    if str(library_dir) not in entries:
        os.environ[variable] = os.pathsep.join([str(library_dir), *entries])


def _add_windows_cairo_directory() -> None:
    """Let CairoSVG load ``cairo-2.dll`` from ``PRISM_CAIRO_LIBRARY_DIR`` or KiCad's ``bin``."""

    candidates = [settings.PRISM_CAIRO_LIBRARY_DIR]
    kicad_cli = locate_kicad_cli()
    if kicad_cli:
        candidates.append(str(Path(kicad_cli).parent))
    for candidate in candidates:
        if candidate and (Path(candidate) / "cairo-2.dll").is_file():
            os.add_dll_directory(candidate)
            return


def acquire_drill_map(
    cli: KicadTool,
    board: Path,
    workdir: Path,
) -> AcquiredArtwork:
    """Plot the drill map -- hole symbols, outline, and symbol key.

    A drill drawing that shows only the board outline documents nothing, and
    the holes are not a layer that ``pcb export svg`` can plot.  They come from
    ``pcb export drill --generate-map``, which writes a full page in both
    formats, so the placement extent is measured from the plotted ink rather
    than taken from a crop KiCad cannot perform here.

    The symbol key below the board is deliberately inside the placed extent:
    it is what tells a reader which mark is which diameter, and the sheet's own
    drill schedule does not carry the symbols.
    """

    workdir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}
    for fmt in ("svg", "pdf"):
        target = workdir / fmt
        target.mkdir(parents=True, exist_ok=True)
        argv = [
            "pcb", "export", "drill",
            "--generate-map", "--map-format", fmt,
            "--output", f"{target}/",
            str(board),
        ]
        result = _run_tool(cli, argv, PLOT_TIMEOUT_SECONDS)
        if not result.ok:
            raise ArtworkError(f"kicad-cli pcb export drill failed: {result.detail()[:400]}")
        found = sorted(target.glob(f"*drl_map.{fmt}"))
        if not found:
            raise ArtworkError(f"kicad-cli produced no drill map in {fmt}")
        outputs[fmt] = found[0]

    svg_text = sanitize_artwork(outputs["svg"].read_text(encoding="utf-8"))
    pdf_bytes = sanitize_artwork_pdf(outputs["pdf"].read_bytes())
    extent = ink_extent(svg_text)
    if extent is None:
        raise ArtworkError("the drill map carries no plotted geometry")
    x, y, width, height = extent
    return AcquiredArtwork(
        layers=("Drill.Map",),
        svg_text=svg_text,
        pdf_bytes=pdf_bytes,
        view_x=x,
        view_y=y,
        view_width=width,
        view_height=height,
        digest=hashlib.sha256(svg_text.encode("utf-8")).hexdigest(),
        # Both formats are the same full page, so the ink sits at the same
        # place in each and no cross-plot comparison is needed.
        page_offset_x=x,
        page_offset_y=y,
    )


# One SVG path command letter, or one number.
_PATH_TOKEN = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")
#: Parameters each path command consumes, per SVG 1.1.
_PATH_ARITY = {
    "m": 2, "l": 2, "h": 1, "v": 1, "c": 6, "s": 4, "q": 4, "t": 2, "a": 7, "z": 0,
}
_STROKE_WIDTH = re.compile(r"stroke-width\s*:\s*([0-9.]+)", re.IGNORECASE)
_CIRCLE = re.compile(
    r'<circle\b[^>]*\bcx\s*=\s*"([-0-9.]+)"[^>]*\bcy\s*=\s*"([-0-9.]+)"'
    r'[^>]*\br\s*=\s*"([0-9.]+)"',
    re.IGNORECASE,
)


def _path_points(data: str) -> list[tuple[float, float, float]]:
    """Absolute ``(x, y, pad)`` points a path visits.

    ``pad`` is how far the drawn curve may bulge past that point: zero for a
    line, the arc radius for an arc.  Bounding an arc by its endpoints alone
    would clip a circle in half, and solving arcs exactly buys nothing here --
    over-reaching only pads the placement, while under-reaching cuts ink off.
    """

    points: list[tuple[float, float, float]] = []
    numbers: list[float] = []
    command = ""
    x = y = 0.0

    for token in _PATH_TOKEN.finditer(data):
        letter, number = token.group(1), token.group(2)
        if letter is not None:
            command = letter
            numbers = []
            continue
        if not command:
            continue
        numbers.append(float(number))
        arity = _PATH_ARITY.get(command.lower(), 0)
        if arity == 0 or len(numbers) < arity:
            continue

        chunk, numbers = numbers, []
        lower = command.lower()
        relative = command.islower()
        radius = 0.0
        if lower == "h":
            x = x + chunk[0] if relative else chunk[0]
        elif lower == "v":
            y = y + chunk[0] if relative else chunk[0]
        else:
            if lower == "a":
                radius = max(abs(chunk[0]), abs(chunk[1]))
            dx, dy = chunk[-2], chunk[-1]
            x = x + dx if relative else dx
            y = y + dy if relative else dy
        points.append((x, y, radius))

        # Repeated parameter sets continue the same command, except that a
        # move-to's repeats are line-tos -- which is exactly how KiCad writes
        # a polyline: one `M`, then bare coordinate pairs.
        if lower == "m":
            command = "l" if relative else "L"

    return points


def ink_extent(svg_text: str) -> tuple[float, float, float, float] | None:
    """The drawn extent of *svg_text*, in millimetres, or ``None`` if empty.

    Needed where ``kicad-cli`` cannot crop for us.  ``pcb export drill`` always
    writes a full page, so the only way to place a drill map beside a board
    drawn at a stated ratio is to measure where its ink actually is.
    """

    try:
        units = user_units_per_mm(svg_text)
    except ArtworkError:
        return None
    if units <= 0:
        return None

    widths = [float(match.group(1)) for match in _STROKE_WIDTH.finditer(svg_text)]
    pad = (max(widths) / 2.0) if widths else 0.0

    xs: list[float] = []
    ys: list[float] = []

    def note(x: float, y: float, radius: float) -> None:
        xs.extend((x - radius - pad, x + radius + pad))
        ys.extend((y - radius - pad, y + radius + pad))

    for match in _PATH_DATA.finditer(svg_text):
        for x, y, radius in _path_points(match.group(1)):
            note(x, y, radius)
    for match in _CIRCLE.finditer(svg_text):
        note(float(match.group(1)), float(match.group(2)), float(match.group(3)))

    if not xs:
        return None
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    return left / units, top / units, (right - left) / units, (bottom - top) / units


def page_offset(cropped_svg: str, page_svg: str) -> tuple[float, float] | None:
    """Where the cropped artwork sits on the full page, in millimetres.

    The two documents are the same plot of the same layers, so their geometry
    differs by exactly the crop translation.  Reading that translation off the
    coordinates is exact, and it is the only way to learn it -- ``pcb export
    pdf`` cannot crop, and nothing in the PDF says where the ink is.

    Returns ``None`` rather than a guess when the two plots do not line up, so
    the caller can skip the composite instead of placing artwork wrongly.
    """

    try:
        cropped_units = user_units_per_mm(cropped_svg)
        page_units = user_units_per_mm(page_svg)
    except ArtworkError:
        return None
    if cropped_units <= 0 or page_units <= 0:
        return None

    cropped = _coordinate_pairs(cropped_svg)
    full = _coordinate_pairs(page_svg)
    if not cropped or len(cropped) != len(full):
        return None
    # Both documents are read in their own user units; the comparison is only
    # meaningful in millimetres.
    deltas = {
        (round(fx / page_units - cx / cropped_units, 3),
         round(fy / page_units - cy / cropped_units, 3))
        for (cx, cy), (fx, fy) in zip(cropped, full)
    }
    if len(deltas) != 1:
        return None
    return deltas.pop()


def extents(svg_text: str) -> tuple[float, float, float, float]:
    """Return the artwork's ``(x, y, width, height)`` in millimetres.

    The viewBox gives the user-unit extents and the ``width``/``height``
    attributes give the same extents in real units; their ratio is the
    user-units-per-millimetre factor.  Reading both is what makes the scale
    contract checkable rather than assumed.
    """

    opened = _SVG_OPEN.search(svg_text)
    if opened is None:
        raise ArtworkError("artwork is not an SVG document")
    header = opened.group(0)

    box = _VIEWBOX.search(header)
    if box is None:
        raise ArtworkError("artwork has no viewBox and cannot be placed")
    try:
        vx, vy, vw, vh = (float(part) for part in box.group(1).replace(",", " ").split())
    except ValueError as exc:
        raise ArtworkError(f"artwork viewBox is unreadable: {box.group(1)!r}") from exc
    if vw <= 0 or vh <= 0:
        raise ArtworkError("artwork viewBox has no area")

    width_attr = _WIDTH.search(header)
    height_attr = _HEIGHT.search(header)
    if width_attr is None or height_attr is None:
        # No physical size to check against; the viewBox is then taken as
        # millimetres, which is what kicad-cli emits.
        return vx, vy, vw, vh

    width_mm = float(width_attr.group(1)) * _UNIT_TO_MM.get(width_attr.group(2).lower(), 1.0)
    height_mm = float(height_attr.group(1)) * _UNIT_TO_MM.get(height_attr.group(2).lower(), 1.0)
    if width_mm <= 0 or height_mm <= 0:
        raise ArtworkError("artwork declares a non-positive physical size")

    # Report extents in millimetres so callers reason in one unit only.
    return vx * width_mm / vw, vy * height_mm / vh, width_mm, height_mm


#: How each bounds kind is described on a sheet, best first.  Cruncher's own
#: vocabulary is terse enough to mislead -- "pads" on a drawing would read as a
#: layer, not as "this outline is a box around the pads".
BOUNDS_KIND_LABELS: dict[str, str] = {
    "model": "3D model outline",
    "holes": "hole bounds",
    "pads": "pad bounds",
}


def assembly_projection_label(mix: Mapping[str, int]) -> str:
    """Compact statement of what the components on a sheet were drawn from."""

    ordered = ("model", "holes", "pads", "unknown")
    parts: list[str] = []
    seen: set[str] = set()
    for key in ordered:
        count = int(mix.get(key) or 0)
        if count <= 0:
            continue
        parts.append(f"{BOUNDS_KIND_LABELS.get(key, key)} {count}")
        seen.add(key)
    for key, count in sorted(mix.items()):
        if key in seen or int(count or 0) <= 0:
            continue
        parts.append(f"{key} {int(count)}")
    return " · ".join(parts)


#: ISO A-series sheet sizes in millimetres (portrait).  Landscape is the swap.
_ISO_A_SHEETS: tuple[tuple[float, float], ...] = (
    (1189.0, 841.0),
    (841.0, 594.0),
    (594.0, 420.0),
    (420.0, 297.0),
    (297.0, 210.0),
    (210.0, 148.0),
)


def looks_like_drawing_sheet(width: float, height: float, *, tol: float = 2.0) -> bool:
    """True when *width*×*height* matches an ISO A drawing sheet."""

    for sheet_w, sheet_h in _ISO_A_SHEETS:
        if (
            abs(width - sheet_w) <= tol and abs(height - sheet_h) <= tol
        ) or (
            abs(width - sheet_h) <= tol and abs(height - sheet_w) <= tol
        ):
            return True
    return False


def content_view(
    art: AcquiredArtwork,
    board_width: float = 0.0,
    board_height: float = 0.0,
) -> AcquiredArtwork:
    """Prefer content bounds when the declared SVG frame is a drawing sheet.

    ``kicad-cli`` / Cruncher sometimes emit a page-sized viewport with the board
    drawn inside it.  Placing that page at 1:1 leaves the board looking tiny in
    an empty artwork window.  Ink bounds (or the board outline as a last resort)
    keep placement honest without changing the SVG body.
    """

    page_like = looks_like_drawing_sheet(art.view_width, art.view_height)
    oversized = (
        board_width > 0
        and board_height > 0
        and (
            art.view_width > board_width * 1.75
            or art.view_height > board_height * 1.75
        )
        and (
            art.view_width > board_width + 40.0
            or art.view_height > board_height + 40.0
        )
    )
    if not page_like and not oversized:
        return art

    ink = ink_extent(art.svg_text)
    if ink is not None:
        left, top, width, height = ink
        if width > 0 and height > 0:
            ink_still_page = looks_like_drawing_sheet(width, height) or (
                board_width > 0
                and board_height > 0
                and (
                    width > board_width * 1.75
                    or height > board_height * 1.75
                )
                and (
                    width > board_width + 40.0
                    or height > board_height + 40.0
                )
            )
            if not ink_still_page:
                return replace(
                    art,
                    view_x=left,
                    view_y=top,
                    view_width=width,
                    view_height=height,
                )

    if board_width > 0 and board_height > 0:
        cx = art.view_x + art.view_width / 2.0
        cy = art.view_y + art.view_height / 2.0
        return replace(
            art,
            view_x=cx - board_width / 2.0,
            view_y=cy - board_height / 2.0,
            view_width=board_width,
            view_height=board_height,
        )
    return art


def user_units_per_mm(svg_text: str) -> float:
    """How many artwork user units make one millimetre."""

    opened = _SVG_OPEN.search(svg_text)
    if opened is None:
        raise ArtworkError("artwork is not an SVG document")
    header = opened.group(0)
    box = _VIEWBOX.search(header)
    width_attr = _WIDTH.search(header)
    if box is None or width_attr is None:
        return 1.0
    _vx, _vy, vw, _vh = (float(part) for part in box.group(1).replace(",", " ").split())
    width_mm = float(width_attr.group(1)) * _UNIT_TO_MM.get(width_attr.group(2).lower(), 1.0)
    if width_mm <= 0:
        raise ArtworkError("artwork declares a non-positive width")
    return vw / width_mm


def place(
    art: AcquiredArtwork,
    window: Rect,
    *,
    scale: float | None = None,
    label: str = "",
) -> tuple[Artwork, float]:
    """Place *art* inside *window*, returning the element and the scale used.

    With ``scale`` given, the placement is at exactly that ratio and the caller
    is responsible for the artwork fitting -- a drawing labelled 1:1 must be
    1:1 even if that means it overruns.  With ``scale`` omitted, the largest
    ratio that fits is chosen and reported back so the sheet can state it.
    """

    if art.view_width <= 0 or art.view_height <= 0:
        raise ArtworkError("artwork has no extent to place")

    if scale is None:
        scale = min(window.width / art.view_width, window.height / art.view_height)
    if scale <= 0:
        raise ArtworkError(f"invalid artwork scale: {scale}")

    # Centre the artwork in its window, then undo the artwork's own origin.
    drawn_width = art.view_width * scale
    drawn_height = art.view_height * scale
    left = window.x + (window.width - drawn_width) / 2
    top = window.y + (window.height - drawn_height) / 2

    units = user_units_per_mm(art.svg_text)
    # `scale` is sheet-mm per board-mm; the group transform works in the
    # artwork's user units, so convert once here.
    group_scale = scale / units
    return (
        Artwork(
            rect=window,
            scale=group_scale,
            offset_x=left - art.view_x * scale,
            offset_y=top - art.view_y * scale,
            source_digest=art.digest,
            svg_body=art.body,
            label=label,
        ),
        scale,
    )


def scale_label(scale: float) -> str:
    """Render a placement ratio the way a drawing states it."""

    def trim(value: float) -> str:
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        return text or "0"

    if abs(scale - 1.0) < 1e-9:
        return "1:1"
    if scale > 1.0:
        return f"{trim(scale)}:1"
    return f"1:{trim(1 / scale)}"


def composite_pdf(
    sheet_pdf: bytes,
    art: AcquiredArtwork,
    window: Rect,
    scale: float,
    *,
    page_index: int = 0,
) -> bytes:
    """Stamp KiCad's own artwork PDF onto a composed sheet page.

    Keeping the artwork as a composited PDF page rather than re-drawing it
    preserves KiCad's geometry exactly, which is the same guarantee the SVG
    path gives by inlining the emitted markup.

    The placement is computed for the *artwork*, not for the page that carries
    it.  Fitting the overlay page into the artwork's window -- which is what
    the obvious `calc_form_xobject_placement` call does -- puts the board on
    the sheet at the ratio of board to page, roughly eight times smaller than
    the ``SCALE`` the sheet states, and disagreeing with the SVG rendering of
    the same sheet.
    """

    import pikepdf

    from app.release_studio.documents.pdf import MM_TO_PT

    if art.page_offset_x is None or art.page_offset_y is None:
        raise ArtworkError(
            "the artwork's position on its PDF page is unknown; the composite "
            "would be placed at the wrong scale"
        )
    if art.view_width <= 0 or art.view_height <= 0:
        raise ArtworkError("artwork has no extent to place")

    with pikepdf.open(io.BytesIO(sheet_pdf)) as base, pikepdf.open(
        io.BytesIO(art.pdf_bytes)
    ) as overlay:
        if len(overlay.pages) == 0:
            raise ArtworkError("artwork PDF has no pages")
        if page_index >= len(base.pages):
            raise ArtworkError(
                f"page {page_index + 1} does not exist in a "
                f"{len(base.pages)}-page document"
            )
        page = base.pages[page_index]
        source = overlay.pages[0]

        drawn_width = art.view_width * scale
        drawn_height = art.view_height * scale
        left = window.x + (window.width - drawn_width) / 2
        top = window.y + (window.height - drawn_height) / 2

        media = page.mediabox
        sheet_height_pt = float(media[3]) - float(media[1])

        source_box = source.mediabox
        source_x0 = float(source_box[0])
        source_y1 = float(source_box[3])

        # The artwork's top-left on the overlay page, expressed in that page's
        # own PDF coordinates (origin bottom-left).
        art_x_pt = source_x0 + art.page_offset_x * MM_TO_PT
        art_top_pt = source_y1 - art.page_offset_y * MM_TO_PT
        art_bottom_pt = art_top_pt - art.view_height * MM_TO_PT

        # Target: the same artwork corner, on the sheet, at the stated ratio.
        target_x_pt = left * MM_TO_PT
        target_bottom_pt = sheet_height_pt - (top + drawn_height) * MM_TO_PT

        # `scale` is sheet-millimetres per board-millimetre, and both pages are
        # in points, so it is also the point-to-point factor.
        tx = target_x_pt - scale * art_x_pt
        ty = target_bottom_pt - scale * art_bottom_pt

        # `Page.add_overlay` invents a *random* XObject resource name, which
        # would make the composed sheet's bytes differ on every render even
        # though the drawing is identical. Placing the form explicitly under a
        # fixed name is the same operation with a name we control.
        form = source.as_form_xobject()
        name = pikepdf.Name(f"/PrismArtwork{page_index}")
        page.add_resource(form, pikepdf.Name("/XObject"), name=name)

        def number(value: float) -> str:
            text = f"{round(value, 4):.4f}".rstrip("0").rstrip(".")
            return text or "0"

        # Clip to the window so the rest of the overlay page -- which is mostly
        # empty, but is a whole A-series page -- cannot spill over the frame,
        # the title block, or the tables.
        clip = (
            f"{number(window.x * MM_TO_PT)} "
            f"{number(sheet_height_pt - window.bottom * MM_TO_PT)} "
            f"{number(window.width * MM_TO_PT)} {number(window.height * MM_TO_PT)} re W n"
        )
        page.contents_add(
            (
                f"q\n{clip}\n"
                f"{number(scale)} 0 0 {number(scale)} {number(tx)} {number(ty)} cm\n"
                f"/PrismArtwork{page_index} Do\nQ\n"
            ).encode("ascii")
        )

        # KiCad's PDF carries /CreationDate and a Producer banner; strip
        # anything the copy may have brought across.
        for key in ("/CreationDate", "/ModDate", "/Producer", "/Creator"):
            if key in base.docinfo:
                del base.docinfo[key]

        out = io.BytesIO()
        base.save(out, deterministic_id=True, linearize=False)
        return out.getvalue()
