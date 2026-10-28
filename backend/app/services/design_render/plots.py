"""Run kicad-cli plots, crop them, add the highlight overlay, and rasterise.

The user decision (ADR D9, 2026-10-26) is kicad-cli plus SVG, not the browser
viewer. kicad-cli 10 plots a schematic page and a PCB against the page frame:
with ``sch export svg --exclude-drawing-sheet`` and
``pcb export svg --exclude-drawing-sheet --page-size-mode 1`` the root SVG's
``viewBox`` is ``0 0 <page width> <page height>`` in millimetres, and one SVG
user unit is one millimetre. Schematic page coordinates are already KiCad's
page frame; board coordinates are page coordinates too. ``page_mm_to_svg`` is
that mapping, and ``parse_view_box`` reads the page extent back from the plot.

The crop is done by rewriting the root ``viewBox`` rather than by asking
kicad-cli to crop: the CLI has no region option, and rewriting keeps the
overlay in the same coordinate system as the artwork. CairoSVG (already a
runtime dependency for Release Studio artwork) rasterises the result.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree

from app.services.design_render.geometry import Box

SVG_NS = "http://www.w3.org/2000/svg"
OVERLAY_FILL = "#ff3b30"
OVERLAY_FILL_OPACITY = 0.25
OVERLAY_STROKE_WIDTH_PX = 2.0
MAX_RENDER_BYTES = 4 * 1024 * 1024
MIN_WIDTH = 256
MAX_WIDTH = 2048

SCHEMATIC_TIMEOUT_SECONDS = 120
PCB_SVG_TIMEOUT_SECONDS = 120
PCB_RENDER_TIMEOUT_SECONDS = 240

#: Explicit layer lists, so a plot never depends on the board's saved selection.
PCB_LAYERS = {
    "front": "F.Cu,F.SilkS,F.Fab,F.CrtYd,Edge.Cuts",
    "back": "B.Cu,B.SilkS,B.Fab,B.CrtYd,Edge.Cuts",
    "copper": "F.Cu,B.Cu,Edge.Cuts",
    "all": (
        "F.Cu,B.Cu,F.SilkS,B.SilkS,F.Fab,B.Fab,"
        "F.CrtYd,B.CrtYd,Edge.Cuts"
    ),
}

_VIEW_BOX = re.compile(r'viewBox="([^"]+)"')


class PlotError(RuntimeError):
    """kicad-cli could not produce a plot the renderer can use."""


def page_mm_to_svg(x_mm: float, y_mm: float) -> tuple[float, float]:
    """A point on the page or board frame as an SVG user-space point.

    Empirically established for kicad-cli 10.0.6 with the export options above:
    the origin is the top-left of the page (or the board's page coordinate
    origin) and one unit is one millimetre, y down. The identity is deliberate:
    the whole mapping is in the two export flags, and the test beside this
    module pins them.
    """

    return x_mm, y_mm


def parse_view_box(svg: str) -> Box:
    """The plot's root ``viewBox`` as a box in millimetres."""

    match = _VIEW_BOX.search(svg)
    if match is None:
        raise PlotError("the kicad-cli SVG has no viewBox")
    try:
        x, y, width, height = (float(part) for part in match.group(1).replace(",", " ").split())
    except (TypeError, ValueError) as error:
        raise PlotError("the kicad-cli SVG has an unreadable viewBox") from error
    if width <= 0 or height <= 0:
        raise PlotError("the kicad-cli SVG has an empty viewBox")
    return Box(x, y, x + width, y + height)


def _run(tool: Any, args: Sequence[str], *, timeout: float, cwd: Path) -> None:
    result = tool.run(args, timeout=timeout, cwd=cwd)
    if not result.ok:
        raise PlotError(f"kicad-cli exited {result.returncode}: {result.detail()}")


def _one_svg(directory: Path, page: str) -> Path:
    files = sorted(directory.glob("*.svg"))
    if not files:
        raise PlotError("kicad-cli wrote no SVG")
    if len(files) == 1:
        return files[0]
    for candidate in files:
        if candidate.stem.endswith(f"-{page}"):
            return candidate
    return files[0]


def plot_schematic(
    tool: Any,
    work: Path,
    schematic: str,
    *,
    page: str,
    output_dir: Path,
) -> str:
    """Plot one schematic page and return its SVG text."""

    output_dir.mkdir(parents=True, exist_ok=True)
    _run(
        tool,
        [
            "sch",
            "export",
            "svg",
            "-o",
            str(output_dir),
            "--exclude-drawing-sheet",
            "--pages",
            page,
            schematic,
        ],
        timeout=SCHEMATIC_TIMEOUT_SECONDS,
        cwd=work,
    )
    return _one_svg(output_dir, page).read_text(encoding="utf-8")


def plot_pcb(
    tool: Any,
    work: Path,
    board: str,
    *,
    layers: str,
    output: Path,
) -> str:
    """Plot one PCB layer set against the page frame and return its SVG text."""

    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        tool,
        [
            "pcb",
            "export",
            "svg",
            "-o",
            str(output),
            "--exclude-drawing-sheet",
            "--page-size-mode",
            "1",
            "--mode-single",
            "--layers",
            PCB_LAYERS[layers],
            board,
        ],
        timeout=PCB_SVG_TIMEOUT_SECONDS,
        cwd=work,
    )
    if not output.is_file():
        raise PlotError("kicad-cli wrote no PCB SVG")
    return output.read_text(encoding="utf-8")


def render_3d(
    tool: Any,
    work: Path,
    board: str,
    *,
    side: str,
    width: int,
    height: int,
    output: Path,
) -> bytes:
    """Render the board in 3D; highlights are never drawn on this view."""

    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        tool,
        [
            "pcb",
            "render",
            "-o",
            str(output),
            "--side",
            side,
            "--width",
            str(width),
            "--height",
            str(height),
            "--background",
            "opaque",
            board,
        ],
        timeout=PCB_RENDER_TIMEOUT_SECONDS,
        cwd=work,
    )
    if not output.is_file():
        raise PlotError("kicad-cli wrote no 3D image")
    png = output.read_bytes()
    # kicad-cli rounds the 3D image to its own block size (592x312 for a
    # 600x338 request on KiCad 10.0.6). The request's width is the contract, so
    # put the pixels back on the exact canvas.
    from PIL import Image
    import io

    with Image.open(io.BytesIO(png)) as image:
        if image.size != (width, height):
            resized = image.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG", optimize=True)
            png = buffer.getvalue()
    return png


def crop_svg(
    svg: str,
    region: Box,
    boxes: Iterable[Box],
    *,
    output_width: int,
) -> str:
    """Crop ``svg`` to ``region`` and draw translucent highlight boxes above it."""

    ElementTree.register_namespace("", SVG_NS)
    root = ElementTree.fromstring(svg)
    root.set("viewBox", f"{region.min_x:.6f} {region.min_y:.6f} {region.width:.6f} {region.height:.6f}")
    root.set("width", f"{region.width:.6f}mm")
    root.set("height", f"{region.height:.6f}mm")

    stroke = OVERLAY_STROKE_WIDTH_PX * region.width / max(1, output_width)
    group = ElementTree.SubElement(
        root,
        f"{{{SVG_NS}}}g",
        {
            "id": "prism-render-highlights",
            "fill": OVERLAY_FILL,
            "fill-opacity": str(OVERLAY_FILL_OPACITY),
            "stroke": OVERLAY_FILL,
            "stroke-opacity": "1",
            "stroke-width": f"{stroke:.6f}",
        },
    )
    for box in boxes:
        ElementTree.SubElement(
            group,
            f"{{{SVG_NS}}}rect",
            {
                "x": f"{box.min_x:.6f}",
                "y": f"{box.min_y:.6f}",
                "width": f"{box.width:.6f}",
                "height": f"{box.height:.6f}",
            },
        )
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def output_size(region: Box, width: int) -> tuple[int, int]:
    """The PNG size: the requested width and the region's aspect ratio."""

    if width < MIN_WIDTH or width > MAX_WIDTH:
        raise ValueError(f"width must be between {MIN_WIDTH} and {MAX_WIDTH}")
    height = round(width * region.height / region.width)
    return width, max(1, height)


def rasterize_svg(svg: str, width: int, height: int) -> bytes:
    """Rasterise an SVG at exactly ``width`` by ``height`` pixels."""

    import cairosvg

    return cairosvg.svg2png(
        bytestring=svg.encode("utf-8"),
        output_width=width,
        output_height=height,
    )


def cap_png(png: bytes, width: int, height: int) -> tuple[bytes, int, int]:
    """Keep a PNG under :data:`MAX_RENDER_BYTES`, shrinking it if it is over.

    A PNG is lossless, so the only lever is fewer pixels. The caller reports the
    size actually stored, which is why this returns the width and height too.
    """

    if len(png) <= MAX_RENDER_BYTES:
        return png, width, height
    from PIL import Image
    import io

    with Image.open(io.BytesIO(png)) as image:
        while len(png) > MAX_RENDER_BYTES and width > MIN_WIDTH:
            scale = 0.8
            width, height = max(MIN_WIDTH, int(width * scale)), max(1, int(height * scale))
            resized = image.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG", optimize=True)
            png = buffer.getvalue()
    if len(png) > MAX_RENDER_BYTES:
        raise PlotError("the render is larger than 4 MB even at the minimum width")
    return png, width, height


def scratch_directory(prefix: str = "prism-render-") -> tempfile.TemporaryDirectory[str]:
    """A temporary plot directory that does not depend on the platform's /tmp path."""

    return tempfile.TemporaryDirectory(prefix=prefix)


__all__ = [
    "MAX_RENDER_BYTES",
    "MAX_WIDTH",
    "MIN_WIDTH",
    "PCB_LAYERS",
    "PlotError",
    "cap_png",
    "crop_svg",
    "output_size",
    "page_mm_to_svg",
    "parse_view_box",
    "plot_pcb",
    "plot_schematic",
    "rasterize_svg",
    "render_3d",
]
