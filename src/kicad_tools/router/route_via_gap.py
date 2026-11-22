"""Check a reconstructed route for open copper between its vias and returning traces."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from kicad_tools.core.geometry import point_to_segment_distance
from kicad_tools.core.types import copper_span_contains

from .via_clearance import SAME_NET_BAND_PREFILTER_SLACK_MM

if TYPE_CHECKING:
    from .layers import Layer
    from .primitives import Route, Segment, Via


def route_own_via_slit(route: Route, edge: float | None) -> tuple[float, float] | None:
    """Locate a real slit between a candidate's via and its returning leg.

    Grid-step fragments of a straight exit have the same copper as the fused
    trace. Fuse them before the cheap distance screen. Only a via with a
    possible slit needs the fab checker's union/shoulder measurement.
    """
    if edge is None or edge <= 0 or not route.vias or not route.segments:
        return None
    from .pairwise_clearance import _fuse_collinear_runs

    segments = _fuse_collinear_runs(route.segments, tol=1e-9)
    for via in route.vias:
        local: dict[Layer, list[Segment]] = {}
        suspect_layers: set[Layer] = set()
        for segment in segments:
            if not copper_span_contains(*via.drilled_layers, segment.layer):
                continue
            distance = point_to_segment_distance(
                via.x, via.y, segment.x1, segment.y1, segment.x2, segment.y2
            )
            gap = distance - (via.diameter + segment.width) / 2
            if gap <= edge + SAME_NET_BAND_PREFILTER_SLACK_MM:
                local.setdefault(segment.layer, []).append(segment)
                # A trace through the barrel centre is an ordinary via exit.
                # A near-tangent trace can leave facing copper beyond the
                # shoulder even when its nearest point just touches the via.
                if distance > 1e-9:
                    suspect_layers.add(segment.layer)
        for layer, nearby in local.items():
            if layer in suspect_layers:
                location = _open_via_trace_gap(via, layer, nearby, route.vias, edge)
                if location is not None:
                    return location
    return None


def _open_via_trace_gap(
    via: Via, layer: Layer, segments: list[Segment], vias: list[Via], edge: float
) -> tuple[float, float] | None:
    from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

    from kicad_tools.validate.rules.physical_gap import CopperSource, iter_source_gaps

    sources = [
        CopperSource(
            LineString([s.start, s.end]).buffer(s.width / 2, quad_segs=64),
            layer.kicad_name,
            s.net_name,
            f"track-{i}",
            "track",
        )
        for i, s in enumerate(segments)
    ]
    # Another barrel can fill a slit. Include it as copper, while assigning
    # the violation only to the via whose neighbourhood we screened.
    for i, other in enumerate(vias):
        if copper_span_contains(*other.drilled_layers, layer) and (
            math.hypot(other.x - via.x, other.y - via.y)
            <= (other.diameter + via.diameter) / 2 + edge + SAME_NET_BAND_PREFILTER_SLACK_MM
        ):
            sources.append(
                CopperSource(
                    Point(other.x, other.y).buffer(other.diameter / 2, quad_segs=64),
                    layer.kicad_name,
                    other.net_name,
                    "via" if other is via else f"via-{i}",
                    "via",
                )
            )
    for gap in iter_source_gaps(sources, edge):
        if "via" in gap.ids and "track" in gap.kinds:
            return (gap.a.x + gap.b.x) / 2, (gap.a.y + gap.b.y) / 2
    return None
