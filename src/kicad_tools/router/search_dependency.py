"""The grid cells a failed negotiated search read (issue #8486).

A net that returns no new connection under the per-net wall-clock cap is
searched again at the next iteration.  Its search budget may only shrink
while nothing the failed search *read* has changed.  The reachable-space
scope used so far (``Autorouter._search_copper_scope``) cannot tell copper
that could have affected the search from copper that merely sits nearby, so
a routine re-route pass kept restarting the streak (comments on #8486).

:class:`SearchDependency` is the precise answer: a coarse, layer-agnostic
occupancy mask of every grid cell the C++ A* discovered (closed set plus open
frontier) while it failed.  Every blocked/free decision the search made was
about a cell in that mask or one step beyond it, so a foreign shape whose
clearance envelope stays clear of the mask -- grown by the routing copper's
half-extent -- cannot have changed what the search saw.  A shape that does
touch it may have opened a detour, and restores the full budget.

The mask only ever over-approximates (coarse blocks, layer-agnostic, a
conservative margin): the safe direction, since a wrongly "touched" shape can
only keep the full budget, never shrink one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = ["SearchDependency"]


@dataclass(frozen=True)
class SearchDependency:
    """Coarse occupancy of the cells a failed search read.

    ``mask[i, j]`` covers grid cells ``y in [i*block, (i+1)*block)`` and
    ``x in [j*block, (j+1)*block)``.  ``margin_mm`` is the routing copper's
    half-extent plus one grid step of slack, added to every shape's
    copper clearance envelope. For a foreign via, ``drill_margin_mm`` is the
    routing drill radius plus the board's hole-to-hole clearance and grid slack.
    """

    mask: np.ndarray
    block: int
    resolution: float
    origin_x: float
    origin_y: float
    margin_mm: float
    drill_margin_mm: float = 0.0

    @property
    def cell_mm(self) -> float:
        return self.block * self.resolution

    def compatible(self, other: SearchDependency) -> bool:
        return (
            self.mask.shape == other.mask.shape
            and self.block == other.block
            and self.resolution == other.resolution
            and self.origin_x == other.origin_x
            and self.origin_y == other.origin_y
        )

    def union(self, other: SearchDependency) -> SearchDependency | None:
        """Cells read by either search, or ``None`` when the grids differ."""
        if not self.compatible(other):
            return None
        return SearchDependency(
            self.mask | other.mask,
            self.block,
            self.resolution,
            self.origin_x,
            self.origin_y,
            max(self.margin_mm, other.margin_mm),
            max(self.drill_margin_mm, other.drill_margin_mm),
        )

    def touches(self, item: Any) -> bool:
        """True when a witness item's clearance envelope may overlap the mask.

        ``item`` is one entry of the search witness built by
        ``Autorouter._search_copper_obstacles``: a ``"segment"`` or ``"via"``
        tuple whose last element is the required clearance.  Anything that
        does not parse is reported as touching (the conservative answer).
        """
        try:
            kind = item[0]
            copper_margin = float(item[-1]) + self.margin_mm
            if kind == "segment":
                (x1, y1), (x2, y2) = item[2], item[3]
                radius = float(item[4]) / 2.0 + copper_margin
            elif kind == "via":
                x1 = x2 = float(item[1])
                y1 = y2 = float(item[2])
                radius = max(
                    float(item[3]) / 2.0 + copper_margin,
                    float(item[4]) / 2.0 + self.drill_margin_mm,
                )
            else:
                return True
            return self._envelope_hits(float(x1), float(y1), float(x2), float(y2), radius)
        except (TypeError, ValueError, IndexError):
            return True

    def _envelope_hits(self, x1: float, y1: float, x2: float, y2: float, radius: float) -> bool:
        cell = self.cell_mm
        rows, cols = self.mask.shape
        # A cell's centre sits half a grid step inside its block; the extra
        # half-diagonal covers the rest of the block.
        reach = radius + cell * math.sqrt(2.0) / 2.0
        j0 = max(int(math.floor((min(x1, x2) - reach - self.origin_x) / cell)), 0)
        j1 = min(int(math.floor((max(x1, x2) + reach - self.origin_x) / cell)) + 1, cols)
        i0 = max(int(math.floor((min(y1, y2) - reach - self.origin_y) / cell)), 0)
        i1 = min(int(math.floor((max(y1, y2) + reach - self.origin_y) / cell)) + 1, rows)
        if j0 >= j1 or i0 >= i1:
            return False
        window = self.mask[i0:i1, j0:j1]
        if not window.any():
            return False
        ii, jj = np.nonzero(window)
        cx = self.origin_x + ((jj + j0) + 0.5) * cell
        cy = self.origin_y + ((ii + i0) + 0.5) * cell
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        if length2 <= 0.0:
            dist = np.hypot(cx - x1, cy - y1)
        else:
            t = np.clip(((cx - x1) * dx + (cy - y1) * dy) / length2, 0.0, 1.0)
            dist = np.hypot(cx - x1 - t * dx, cy - y1 - t * dy)
        return bool((dist <= reach).any())
