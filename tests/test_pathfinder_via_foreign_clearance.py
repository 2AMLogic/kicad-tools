"""Tests for Issue #2947: pathfinder via foreign-net world-coord clearance.

The bug: ``Router._check_via_placement_cached`` consulted only the
coarse-grid obstacle map via ``_is_via_blocked``, so a via that lands on
a "free" grid cell could still violate world-coord clearance against a
foreign-net pad / committed track (most visibly: board-04 BOOT0 vias
overlapping SWDIO/SWCLK by 0.1-0.2 mm).

The fix: after the per-layer grid check passes, call
``via_clearance.point_clear_of_copper`` against the foreign-net context
pushed by :meth:`Autorouter._update_router_via_foreign_context`.  When
no foreign context is set (default / pre-#2947), behavior is unchanged.

Issue #9923: the foreign-pad half of that check now scores each pad with
the exact clearance kernel (``pad_shape`` keeps the pad's shape and
rotation) instead of the pre-rotation ``(x, y, w, h, net)`` 5-tuple box
``point_clear_of_copper`` rect-distance scored.  The corpus rows below
pin both directions: four rotated pads whose unrotated box accepted a
pinned KiCad 10.0.6 violation are rejected, and six native-legal rotated
pairs the doubled-bounds over-approximation would have rejected stay
accepted.
"""

import dataclasses

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.io import load_pads_for_analysis
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting
from kicad_tools.router.search_radius import search_radius_cells
from tests.conformance.board import write_case
from tests.conformance.generator import generate_case


@pytest.mark.parametrize(
    ("class_diameter", "board_diameter", "fits"),
    [(0.60, 0.95, True), (0.80, 0.60, False), (None, 0.60, True), (None, 0.80, False)],
)
def test_board06_bga_escape_uses_the_physical_via_diameter(class_diameter, board_diameter, fits):
    # Board 06 J1.B2 -> U2.F2. The native via at (151.45, 69.70)
    # is 0.567 mm from the bounding box of the neighbouring U2.F3 pad.
    rules = DesignRules(grid_resolution=0.05, via_diameter=board_diameter, via_clearance=0.20)
    grid = RoutingGrid(width=4.0, height=4.0, rules=rules)
    classes = {"TX2+": NetClassRouting("USB3", via_size=class_diameter)} if class_diameter else {}
    router = Router(grid, rules, net_class_map=classes)
    router.set_net_name_to_id({"TX2+": 1})
    gx, gy = grid.world_to_grid(2.0, 2.0)
    x, y = grid.grid_to_world(gx, gy)
    foreign = Pad(x=x + 0.78, y=y + 0.34, width=0.45, height=0.45, net=2, net_name="TX2-")
    router.set_via_foreign_context(foreign_pads=[foreign])
    radius = search_radius_cells((class_diameter or board_diameter) / 2, 0.20, grid.resolution)

    assert router._check_via_placement_cached(gx, gy, 1, True, radius=radius) is fits


def _make_router() -> tuple[Router, RoutingGrid]:
    """Build a small two-layer router with a known-good origin."""
    stack = LayerStack.two_layer()
    rules = DesignRules(grid_resolution=0.5)
    grid = RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        layer_stack=stack,
    )
    return Router(grid, rules), grid


class TestForeignContextBaseline:
    """Without foreign context, behavior matches pre-#2947 (the existing
    via_placement_regression tests already cover the grid-only path)."""

    def test_no_foreign_context_allows_clear_cell(self):
        """A clear cell with no foreign context returns True."""
        router, grid = _make_router()
        # Pick a cell in the interior with no obstacles.
        can_place = router._check_via_placement_cached(gx=20, gy=20, net=1, allow_sharing=False)
        assert can_place

    def test_setter_default_args_empty(self):
        """Calling the setter with no args clears stale context."""
        router, _ = _make_router()
        # Pre-populate with a foreign pad to ensure a later clear works.
        bad_pad = Pad(x=10.0, y=10.0, width=1.0, height=1.0, net=99, net_name="X")
        router.set_via_foreign_context(foreign_pads=[bad_pad])
        assert router._foreign_pad_tuples  # populated

        router.set_via_foreign_context()  # clear
        assert router._foreign_pad_tuples == []
        assert router._foreign_track_adapters == []


class TestForeignPadRejection:
    """A via that passes the grid check but encroaches on a foreign-net
    pad's clearance envelope must be rejected."""

    def test_foreign_pad_within_clearance_blocks(self):
        """Foreign pad close enough to violate via clearance -> rejected."""
        router, grid = _make_router()
        # Pick a cell whose world-coord position will be near (but not
        # touching) a foreign-net pad.  With rules.via_clearance=0.2,
        # via_diameter=0.7 (radius=0.35) and pad effective_radius=
        # max(w,h)/2=0.5, the threshold is 0.35 + 0.5 + 0.2 = 1.05 mm.
        gx, gy = 20, 20
        wx, wy = grid.grid_to_world(gx, gy)

        # Place a foreign-net pad 0.6 mm from the via center -- well
        # inside the 1.05 mm threshold.
        foreign_pad = Pad(
            x=wx + 0.6,
            y=wy,
            width=1.0,
            height=1.0,
            net=99,
            net_name="FOREIGN",
        )
        router.set_via_foreign_context(foreign_pads=[foreign_pad])

        # Without foreign-context, the grid check would pass; the
        # world-coord check must now reject.
        can_place = router._check_via_placement_cached(gx=gx, gy=gy, net=1, allow_sharing=False)
        assert not can_place, (
            "Via must be rejected: foreign-net pad at 0.6 mm violates "
            "0.35 (via_r) + 0.5 (pad_r) + 0.2 (clear) = 1.05 mm threshold"
        )

    def test_same_net_pad_does_not_block(self):
        """A pad on the via's own net is filtered out (no rejection)."""
        router, grid = _make_router()
        gx, gy = 20, 20
        wx, wy = grid.grid_to_world(gx, gy)

        same_net_pad = Pad(
            x=wx + 0.6,
            y=wy,
            width=1.0,
            height=1.0,
            net=1,
            net_name="SAME",
        )
        router.set_via_foreign_context(foreign_pads=[same_net_pad])

        can_place = router._check_via_placement_cached(gx=gx, gy=gy, net=1, allow_sharing=False)
        assert can_place, "Same-net pad must not trigger foreign-net rejection"

    def test_distant_foreign_pad_does_not_block(self):
        """Foreign pad far outside the clearance envelope -> allowed."""
        router, grid = _make_router()
        gx, gy = 20, 20
        wx, wy = grid.grid_to_world(gx, gy)

        far_pad = Pad(
            x=wx + 5.0,
            y=wy,  # 5 mm clear -- way beyond 1.05 mm
            width=1.0,
            height=1.0,
            net=99,
            net_name="FAR",
        )
        router.set_via_foreign_context(foreign_pads=[far_pad])

        can_place = router._check_via_placement_cached(gx=gx, gy=gy, net=1, allow_sharing=False)
        assert can_place


class TestForeignTrackRejection:
    """The board-04 BOOT0 cluster: a via that passes the grid check but
    overlaps a committed foreign-net track segment."""

    def test_foreign_track_within_clearance_blocks(self):
        """Foreign track segment 0.15 mm from via center -> rejected."""
        router, grid = _make_router()
        gx, gy = 20, 20
        wx, wy = grid.grid_to_world(gx, gy)

        # A horizontal track on the same Y, 0.15 mm above the via.
        # via_r=0.35 + seg_w/2=0.125 + clear=0.2 = 0.675 mm threshold;
        # 0.15 mm is well inside.  Models the BOOT0 vs SWDIO/SWCLK
        # geometry from the board-04 cluster (-0.204 mm overlap).
        foreign_track = Segment(
            x1=wx - 2.0,
            y1=wy + 0.15,
            x2=wx + 2.0,
            y2=wy + 0.15,
            width=0.25,
            layer=Layer.B_CU,
            net=99,
            net_name="BOOT0_FOE",
        )
        router.set_via_foreign_context(foreign_tracks=[foreign_track])

        can_place = router._check_via_placement_cached(gx=gx, gy=gy, net=1, allow_sharing=False)
        assert not can_place, (
            "Via must be rejected: foreign-net track at 0.15 mm violates "
            "0.35 (via_r) + 0.125 (seg_w/2) + 0.2 (clear) = 0.675 mm threshold"
        )


class TestCacheInvariants:
    """Cache invariant: setting foreign context must invalidate cached
    results (mirrors the ``add_routed_segments`` / ``clear_via_cache``
    pattern)."""

    def test_setter_clears_via_cache(self):
        """``set_via_foreign_context`` clears stale cache entries."""
        router, grid = _make_router()

        # Prime the cache with a positive result.
        can_place_1 = router._check_via_placement_cached(gx=20, gy=20, net=1, allow_sharing=False)
        assert can_place_1
        assert (20, 20, 1, router._via_half_cells) in router._via_cache

        # Push a foreign context that should now block.
        wx, wy = grid.grid_to_world(20, 20)
        blocker = Pad(
            x=wx,
            y=wy,  # dead-center -> definite overlap
            width=1.0,
            height=1.0,
            net=99,
            net_name="X",
        )
        router.set_via_foreign_context(foreign_pads=[blocker])

        # Cache must have been cleared.
        assert (20, 20, 1, router._via_half_cells) not in router._via_cache

        # Re-check: now blocked.
        can_place_2 = router._check_via_placement_cached(gx=20, gy=20, net=1, allow_sharing=False)
        assert not can_place_2

    def test_cache_key_is_net_keyed(self):
        """Per-#2947 acceptance: cache key includes net so cross-net
        positives can't go stale.  (Documents existing invariant.)"""
        router, _ = _make_router()
        # Two different nets at the same (gx, gy) -> separate cache entries.
        router._check_via_placement_cached(20, 20, net=1, allow_sharing=False)
        router._check_via_placement_cached(20, 20, net=2, allow_sharing=False)
        keys = list(router._via_cache.keys())
        nets = {k[2] for k in keys}
        assert {1, 2}.issubset(nets), f"Cache must discriminate by net for #2947 safety: {keys}"


# ---------------------------------------------------------------------------
# Issue #9923: rotated foreign pads scored by shape, not pre-rotation box
# ---------------------------------------------------------------------------

#: Pinned corpus pad-via rows (KiCad 10.0.6, rule 0.20 mm), measured on
#: ``c89f194b7``: the four rejects are native clearance violations the old
#: pre-rotation-box encoding accepted; the six accepts are native-legal pairs
#: a doubled (rotated-AABB) bound would have rejected.  The kernel's measured
#: gaps for the legal controls: 0.2409, 0.2467, 0.2475, 0.2240, 0.2468 and
#: 0.2136 mm -- every decision matched KiCad on all 13 measured pairs.
CORPUS_PAD_VIA_ROWS = [
    # (seed, pad net, via net, expect the via placement rejected)
    (1, "N6", "N7", True),  # rect pad, 250.500 deg -- native gap 0.1933 mm
    (44, "N5", "N6", True),  # oval pad, 249.664 deg -- native gap 0.1600 mm
    (135, "N5", "N6", True),  # oval pad, 97.851 deg -- native gap 0.1861 mm
    (151, "N7", "N8", True),  # oval pad, 312.682 deg -- native gap 0.1633 mm
    (30, "N8", "N9", False),  # roundrect, native-legal control
    (58, "N6", "N7", False),
    (79, "N6", "N7", False),
    (106, "N7", "N8", False),
    (124, "N5", "N6", False),
    (125, "N6", "N7", False),
]


def _corpus_pad_via_pair(
    seed: int,
    pad_net: str,
    via_net: str,
    tmp_path,
) -> tuple:
    """Rebuild one pinned corpus pad-via pair through the production loader.

    Generates the deterministic conformance case, serialises it as a real
    ``.kicad_pcb`` and reads the pad back with
    :func:`~kicad_tools.router.io.load_pads_for_analysis`, so the pad carries
    the parser's real shape / rotation (the round-trip is what makes the
    rotation meaningful for the world-coordinate check).
    """
    case = generate_case(seed)
    pair = next(
        p for p in case.pairs if p.kind == "pad-via" and p.net_a == pad_net and p.net_b == via_net
    )
    via = next(v for v in case.vias if v.net == via_net)
    written = write_case(case, tmp_path / f"seed{seed}")
    pad = next(p for p in load_pads_for_analysis(written.pcb_path) if p.net_name == pad_net)
    return pair, via, pad


def _corpus_via_router(pad: Pad, via) -> tuple[Router, RoutingGrid, tuple[int, int], Pad]:
    """A 0.1 mm-grid router whose central cell IS the corpus via center.

    The via keeps its physical diameter; the pad is translated by the same
    offset so the pinned pad-to-via geometry is preserved exactly (the
    curator's probe measured at most 0.000064 mm of center rounding this
    way).  The raster starts empty, isolating the world-coordinate check.
    """
    rules = DesignRules(grid_resolution=0.1, via_diameter=via.diameter, via_clearance=0.20)
    grid = RoutingGrid(width=20.0, height=20.0, rules=rules, layer_stack=LayerStack.two_layer())
    router = Router(grid, rules)
    gx, gy = grid.world_to_grid(10.0, 10.0)
    wx, wy = grid.grid_to_world(gx, gy)
    moved = dataclasses.replace(pad, x=wx + (pad.x - via.x), y=wy + (pad.y - via.y))
    return router, grid, (gx, gy), moved


class TestRotatedForeignPadShapeKernel:
    """Issue #9923: a rotated foreign pad must be scored against its real
    copper (shape + rotation), not its pre-rotation local box."""

    @pytest.mark.parametrize(
        ("seed", "pad_net", "via_net", "expect_reject"),
        CORPUS_PAD_VIA_ROWS,
    )
    def test_corpus_pair_verdict_matches_kicad(
        self, seed, pad_net, via_net, expect_reject, tmp_path
    ):
        pair, via, pad = _corpus_pad_via_pair(seed, pad_net, via_net, tmp_path)
        router, _grid, (gx, gy), moved = _corpus_via_router(pad, via)
        router.set_via_foreign_context(foreign_pads=[moved])
        # Any net id other than the pad's models the via's own (foreign) net.
        via_net_id = moved.net + 100

        can_place = router._check_via_placement_cached(gx, gy, via_net_id, allow_sharing=False)

        assert can_place is (not expect_reject), (
            f"seed {seed} {pad_net}-{via_net}: shape={pad.shape} rotation={pad.rotation:.3f} "
            f"deg, target gap {pair.target_gap_mm:.4f} mm vs 0.20 mm rule -- KiCad says "
            f"{'violation' if expect_reject else 'legal'}"
        )

    def test_rotated_pad_on_same_net_does_not_block(self, tmp_path):
        """The rotated-geometry rejection is a foreign-net judgement: the
        same seed-1 pad registered under the via's own net stays accepted
        (via-in-pad is a caller policy question, not this check's)."""
        _pair, via, pad = _corpus_pad_via_pair(1, "N6", "N7", tmp_path)
        router, _grid, (gx, gy), moved = _corpus_via_router(pad, via)
        same_net_pad = dataclasses.replace(moved, net=moved.net + 100, net_name="VIA_NET")
        router.set_via_foreign_context(foreign_pads=[same_net_pad])

        assert router._check_via_placement_cached(gx, gy, same_net_pad.net, allow_sharing=False)

    def test_rotated_pad_also_on_raster_stays_rejected(self, tmp_path):
        """Raster + world-check interaction: with the seed-1 pad registered
        through :meth:`RoutingGrid.add_pad` too, the coarse raster alone
        already rejects the cell (its dilation carries the clearance), and
        composing the world context on top keeps the rejection -- the two
        checks agree rather than double-count or contradict."""
        _pair, via, pad = _corpus_pad_via_pair(1, "N6", "N7", tmp_path)
        router, grid, (gx, gy), moved = _corpus_via_router(pad, via)
        via_net_id = moved.net + 100
        grid.add_pad(moved)

        raster_only = router._check_via_placement_cached(gx, gy, via_net_id, allow_sharing=False)
        assert not raster_only, "raster with the pad registered must reject the via cell"

        router.set_via_foreign_context(foreign_pads=[moved])
        assert not router._check_via_placement_cached(gx, gy, via_net_id, allow_sharing=False)
