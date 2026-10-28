"""PNG renders of a revision for multimodal agents (ADR D9, §3.7).

The job and its route live in the modules beside this one: ``geometry`` turns a
revision's projections into highlight boxes, ``plots`` runs kicad-cli and
rasterises the plot with an SVG overlay, and ``service`` owns the request hash,
the cache record and the ``render_view`` job kind.
"""

from __future__ import annotations

__all__: list[str] = []
