"""Check optimizer rewrites against the fabrication gap around their own SMD pads."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..primitives import Segment

if TYPE_CHECKING:
    from ..grid import RoutingGrid


def _leg(seg: Segment) -> tuple:
    """Geometric identity in a uniform-width, single-net/layer chain."""
    a, b = (seg.x1, seg.y1), (seg.x2, seg.y2)
    return (a, b, seg.width) if a <= b else (b, a, seg.width)


def pad_gap_check(
    grid: RoutingGrid | None,
) -> Callable[[list[Segment], list[Segment], list[Segment]], bool] | None:
    """Bind the resolved pad-spacing limit and the router's stationary pad copper."""
    edge = getattr(getattr(grid, "rules", None), "same_net_pad_gap_band_edge_mm", None)
    if grid is None or not isinstance(edge, (int, float)) or edge <= 0:
        return None

    from shapely.geometry import LineString  # type: ignore[import-untyped]
    from shapely.strtree import STRtree  # type: ignore[import-untyped]

    from kicad_tools.geometry.copper import standard_pad_polygon
    from kicad_tools.validate.rules.physical_gap import CopperSource, iter_source_gaps

    from ..clearance_shapes import pad_kernel_params

    pads = defaultdict(list)
    for pad in grid.pads:
        if pad.through_hole or pad.steiner_point or not pad.net:
            continue
        shape, ratio = pad_kernel_params(pad)
        copper = standard_pad_polygon(
            pad.x, pad.y, pad.width, pad.height, shape, pad.rotation, ratio
        )
        if copper is not None:
            pads[pad.net, pad.layer].append(copper)
    trees = {key: STRtree(polygons) for key, polygons in pads.items()}

    def replacement_is_clear(
        original: list[Segment], candidate: list[Segment], kept: list[Segment]
    ) -> bool:
        if not candidate or candidate == original:
            return True
        first = candidate[0]
        key = first.net, first.layer
        tree = trees.get(key)
        if tree is None:
            return True
        previous = {_leg(seg) for seg in original}
        changed = [
            LineString([(s.x1, s.y1), (s.x2, s.y2)]) for s in candidate if _leg(s) not in previous
        ]
        if not changed:
            return True
        # Uniform-width legs. Only pads within the fab band of new copper can gain a slit.
        _, hits = tree.query(changed, predicate="dwithin", distance=edge + first.width / 2)
        layer, net = first.layer.name, str(first.net)
        for idx in set(hits):
            pad_copper = pads[key][idx]
            left, bottom, right, top = pad_copper.bounds
            sources = [CopperSource(pad_copper, layer, net, "pad", "pad")]
            for neighbour in tree.query(pad_copper, predicate="dwithin", distance=edge):
                if neighbour != idx:
                    sources.append(
                        CopperSource(pads[key][neighbour], layer, net, "neighbour", "pad")
                    )
            for segments, is_candidate in ((candidate, True), (kept, False)):
                for seg in segments:
                    reach = edge + seg.width / 2
                    if (
                        seg.layer != first.layer
                        or seg.net != first.net
                        or min(seg.x1, seg.x2) > right + reach
                        or max(seg.x1, seg.x2) < left - reach
                        or min(seg.y1, seg.y2) > top + reach
                        or max(seg.y1, seg.y2) < bottom - reach
                    ):
                        continue
                    copper = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)]).buffer(
                        seg.width / 2, quad_segs=64
                    )
                    owner = "replacement" if is_candidate and _leg(seg) not in previous else "kept"
                    sources.append(CopperSource(copper, layer, net, owner))
            # The closest point alone misses a slit beside an actual pad join.
            # Grade facing edges of the copper union the way fabrication DRC does.
            if any(
                "replacement" in gap.ids and "pad" in gap.ids
                for gap in iter_source_gaps(sources, edge)
            ):
                return False
        return True

    return replacement_is_clear
