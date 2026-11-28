"""Tests for the trace-copper polygon helper (Issue #4176)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.geometry.copper import (
    point_segment_distance,
    segment_centerline_distance,
    segment_copper_polygon,
    segments_copper_touch,
)

pytest.importorskip("shapely")


def test_segment_polygon_has_expected_area():
    # A 10mm-long, 0.25mm-wide trace: rectangle 10 x 0.25 = 2.5 plus two
    # semicircular end caps of radius 0.125 (area pi * 0.125**2 ~= 0.049).
    poly = segment_copper_polygon((0.0, 0.0), (10.0, 0.0), 0.25)
    assert poly is not None
    expected = 10.0 * 0.25 + 3.141592653589793 * 0.125**2
    assert poly.area == pytest.approx(expected, rel=1e-3)


def test_two_touching_segments_intersect():
    a = segment_copper_polygon((0.0, 0.0), (5.0, 0.0), 0.25)
    b = segment_copper_polygon((5.0, 0.0), (5.0, 5.0), 0.25)
    assert a.intersects(b)


def test_two_apart_segments_do_not_intersect():
    # Endpoints 0.009mm apart but width 0.001 (buffer 0.0005 each side): the
    # copper does not bridge the gap.
    a = segment_copper_polygon((0.0, 0.0), (5.0, 0.0), 0.001)
    b = segment_copper_polygon((5.0, 0.009), (5.0, 5.0), 0.001)
    assert not a.intersects(b)


def test_zero_width_returns_line_geometry():
    poly = segment_copper_polygon((0.0, 0.0), (10.0, 0.0), 0.0)
    assert poly is not None
    assert poly.area == 0.0
    # A zero-length + zero-width segment reduces to a point.
    pt = segment_copper_polygon((1.0, 1.0), (1.0, 1.0), 0.0)
    assert pt is not None
    assert pt.area == 0.0


def test_zero_length_positive_width_is_disk():
    disk = segment_copper_polygon((1.0, 1.0), (1.0, 1.0), 0.5)
    assert disk is not None
    # Round end cap disk of radius 0.25.
    assert disk.area == pytest.approx(3.141592653589793 * 0.25**2, rel=1e-2)


# ---------------------------------------------------------------------------
# Full-copper contact primitives (Issue #5060)
# ---------------------------------------------------------------------------


def test_point_segment_distance_clamps_to_the_segment():
    # Perpendicular foot inside the segment.
    assert point_segment_distance((5.0, 2.0), (0.0, 0.0), (10.0, 0.0)) == pytest.approx(2.0)
    # Perpendicular foot beyond the end: distance to the endpoint.
    assert point_segment_distance((13.0, 4.0), (0.0, 0.0), (10.0, 0.0)) == pytest.approx(5.0)
    # Degenerate zero-length segment behaves as a point.
    assert point_segment_distance((3.0, 4.0), (0.0, 0.0), (0.0, 0.0)) == pytest.approx(5.0)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        # Proper crossing: the minimum is interior to BOTH segments, so a
        # naive endpoint-only minimum would report a positive distance.
        ((((-5.0, 0.0), (5.0, 0.0))), (((0.0, -5.0), (0.0, 5.0))), 0.0),
        # T-junction against an interior point.
        ((((0.0, 0.0), (10.0, 0.0))), (((5.0, 0.0), (5.0, 5.0))), 0.0),
        # Collinear overlap.
        ((((0.0, 0.0), (10.0, 0.0))), (((4.0, 0.0), (6.0, 0.0))), 0.0),
        # Collinear but disjoint.
        ((((0.0, 0.0), (4.0, 0.0))), (((6.0, 0.0), (10.0, 0.0))), 2.0),
        # Parallel offset.
        ((((0.0, 0.0), (10.0, 0.0))), (((0.0, 3.0), (10.0, 3.0))), 3.0),
        # T-junction stopping short of the trunk.
        ((((0.0, 0.0), (10.0, 0.0))), (((5.0, 1.5), (5.0, 5.0))), 1.5),
    ],
)
def test_segment_centerline_distance_matches_shapely(a, b, expected):
    assert segment_centerline_distance(a[0], a[1], b[0], b[1]) == pytest.approx(expected)
    # The analytic result must agree with shapely's LineString distance.
    from shapely.geometry import LineString

    assert segment_centerline_distance(a[0], a[1], b[0], b[1]) == pytest.approx(
        LineString([a[0], a[1]]).distance(LineString([b[0], b[1]]))
    )


def test_segments_copper_touch_uses_full_width():
    trunk = ((0.0, 0.0), (10.0, 0.0), 0.6)
    # Branch stopping exactly on the trunk's copper edge: capsules overlap.
    assert segments_copper_touch(*trunk, (5.0, 0.3), (5.0, 5.0), 0.4)
    # Branch stopping 1 um past the combined reach (0.3 + 0.2): open.
    assert not segments_copper_touch(*trunk, (5.0, 0.501), (5.0, 5.0), 0.4)
    # Exactly tangent copper counts as touching (a ZERO gap, not a positive one).
    assert segments_copper_touch(*trunk, (5.0, 0.5), (5.0, 5.0), 0.4)


def test_segments_copper_touch_agrees_with_buffered_polygons():
    """The analytic predicate matches the buffered-polygon ground truth."""
    cases = [
        ((0.0, 0.0), (10.0, 0.0), 0.6, (5.0, 0.0), (5.0, 5.0), 0.4),
        ((0.0, 0.0), (10.0, 0.0), 0.6, (3.0, 0.5), (7.0, 0.5), 0.6),
        ((0.0, 0.0), (10.0, 0.0), 0.6, (5.0, 0.8), (9.0, 0.8), 0.6),
        ((0.0, 0.0), (10.0, 0.0), 0.6, (5.0, 5.0), (9.0, 5.0), 0.6),
        ((0.0, 0.0), (0.0, 0.0), 0.4, (0.1, 0.0), (5.0, 0.0), 0.2),
        ((0.0, 0.0), (0.0, 0.0), 0.4, (0.4, 0.0), (5.0, 0.0), 0.2),
    ]
    for a_start, a_end, a_w, b_start, b_end, b_w in cases:
        poly_a = segment_copper_polygon(a_start, a_end, a_w)
        poly_b = segment_copper_polygon(b_start, b_end, b_w)
        # buffer() facets the round caps inwards, so shapely can only ever
        # UNDER-report contact; a mismatch is allowed solely in that direction.
        if poly_a.intersects(poly_b):
            assert segments_copper_touch(a_start, a_end, a_w, b_start, b_end, b_w)


def test_segments_copper_touch_is_invariant_under_collinear_splits():
    """Splitting a segment cannot create or destroy contact (#5060)."""
    trunk_start, trunk_end, trunk_w = (0.0, 0.0), (10.0, 0.0), 0.6
    for branch, expected in (
        (((5.0, 0.0), (5.0, 5.0)), True),  # T on the interior
        (((5.0, 0.601), (5.0, 5.0)), False),  # 1 um positive gap
    ):
        whole = segments_copper_touch(trunk_start, trunk_end, trunk_w, *branch, 0.6)
        assert whole is expected
        for parts in (2, 3, 7):
            touched = any(
                segments_copper_touch(
                    (10.0 * i / parts, 0.0),
                    (10.0 * (i + 1) / parts, 0.0),
                    trunk_w,
                    *branch,
                    0.6,
                )
                for i in range(parts)
            )
            assert touched is expected, f"{parts}-way split changed the verdict"


@pytest.mark.parametrize("offset", [(0.0, 0.0), (150.0, 150.0), (-4.0, 92.0), (1024.0, -2048.0)])
def test_segments_copper_touch_is_translation_invariant_at_a_tie(offset):
    """#7224: ``point_segment_distance`` uses translation-invariant differences.

    Coordinates are dyadic rationals and offsets integers, so every coordinate
    difference is exact under translation; the verdict at the exact tie
    (``reach == distance``) must therefore be identical for every offset.
    """
    from kicad_tools.geometry.copper import point_segment_distance

    ox, oy = offset
    a0, a1 = (0.0, 0.0), (7.5, 2.25)
    p = (3.25, 1.5)
    base = point_segment_distance(p, a0, a1)
    sh = lambda q: (q[0] + ox, q[1] + oy)  # noqa: E731
    assert point_segment_distance(sh(p), sh(a0), sh(a1)) == base

    # Two-segment capsule tie: B is a degenerate segment at p; widths chosen
    # so reach equals the exact centerline distance.
    width = 2 * base
    assert segments_copper_touch(sh(a0), sh(a1), width, sh(p), sh(p), 0.0)
    assert not segments_copper_touch(sh(a0), sh(a1), width * (1 - 1e-9), sh(p), sh(p), 0.0)


# ---------------------------------------------------------------------------
# Chamfered roundrect copper (Issue #10669)
# ---------------------------------------------------------------------------

# P2.1: a 2 x 2 mm roundrect whose copper is offset from its drill anchor.
# ``offset`` per footprint angle keeps the copper centred at (11.7, 13.7), so
# it spans x 10.7..12.7 and y 12.7..14.7 whatever the rotation.
_OFFSET_PAD = """(kicad_pcb (version 20240108) (generator "t") (generator_version "8.0")
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "SENSE")
  (footprint "T:P" (layer "F.Cu") (uuid "p2") (at 12 14 {angle})
    (property "Reference" "P2" (at 0 -3 0) (layer "F.SilkS") (uuid "r2"))
    (pad "1" thru_hole roundrect (at 0 0 {angle}) (size 2 2) {tokens}
      (drill 0.4 (offset {offset})) (layers "*.Cu") (net 1 "SENSE") (uuid "pp2"))))
"""

# 0.042 mm in from the pad's bottom-right and top-left corners in the board frame.
_BOTTOM_RIGHT = (12.67, 14.67)
_TOP_LEFT = (10.73, 12.73)
_SQUARE = "(roundrect_rratio 0)"
_CUT_AREA_LOSS = (0.14 * 2) ** 2 / 2


def _offset_pad_outline(
    tmp_path: Path, tokens: str = _SQUARE, *, angle: int = 0, offset: str = "-0.3 -0.3"
) -> tuple[object, bool]:
    """``(pad_copper_outline, pad_outline_is_exact)`` of the parsed P2.1."""
    from kicad_tools.geometry.copper import pad_copper_outline, pad_outline_is_exact
    from kicad_tools.schema.pcb import PCB

    path = tmp_path / "offset_pad.kicad_pcb"
    path.write_text(_OFFSET_PAD.format(angle=angle, tokens=tokens, offset=offset))
    footprint = PCB.load(str(path)).get_footprint("P2")
    pad = footprint.pads[0]
    return (
        pad_copper_outline(pad, footprint.position, footprint.rotation),
        pad_outline_is_exact(pad),
    )


@pytest.mark.parametrize(
    ("corner", "angle", "offset"),
    [
        ("bottom_right", 0, "-0.3 -0.3"),
        # At -90 the pad's own top-right corner sits at the board's bottom right,
        # at 90 its bottom-left does.
        ("top_right", -90, "-0.3 0.3"),
        ("bottom_left", 90, "0.3 -0.3"),
    ],
)
def test_single_corner_chamfer_cuts_only_that_corner(
    tmp_path: Path, corner: str, angle: int, offset: str
):
    """One selected corner is a straight cut; the detector used to need two."""
    from shapely.geometry import Point

    outline, exact = _offset_pad_outline(
        tmp_path, f"{_SQUARE} (chamfer_ratio 0.14) (chamfer {corner})", angle=angle, offset=offset
    )
    assert exact
    assert not outline.covers(Point(*_BOTTOM_RIGHT))
    assert outline.covers(Point(*_TOP_LEFT))
    assert outline.area == pytest.approx(4.0 - _CUT_AREA_LOSS, abs=1e-9)


def test_chamfer_on_a_far_corner_leaves_the_near_copper(tmp_path: Path):
    from shapely.geometry import Point

    outline, exact = _offset_pad_outline(
        tmp_path, f"{_SQUARE} (chamfer_ratio 0.14) (chamfer top_left)"
    )
    assert exact
    assert outline.covers(Point(*_BOTTOM_RIGHT))
    assert not outline.covers(Point(*_TOP_LEFT))
    assert outline.area == pytest.approx(4.0 - _CUT_AREA_LOSS, abs=1e-9)


def test_every_selected_corner_is_cut(tmp_path: Path):
    from shapely.geometry import Point

    outline, exact = _offset_pad_outline(
        tmp_path, f"{_SQUARE} (chamfer_ratio 0.14) (chamfer top_left bottom_right)"
    )
    assert exact
    assert not outline.covers(Point(*_BOTTOM_RIGHT))
    assert not outline.covers(Point(*_TOP_LEFT))
    assert outline.area == pytest.approx(4.0 - 2 * _CUT_AREA_LOSS, abs=1e-9)


def test_chamfer_replaces_the_round_on_its_own_corner_only(tmp_path: Path):
    """A cut corner is square up to its cut line; the other corners keep the radius."""
    from shapely.geometry import Point, Polygon

    # rratio 0.5 rounds the three uncut corners into one circle of radius 1 mm;
    # the 0.05 cut takes a 0.1 mm triangle off the bottom-right corner.
    outline, exact = _offset_pad_outline(
        tmp_path, "(roundrect_rratio 0.5) (chamfer_ratio 0.05) (chamfer bottom_right)"
    )
    assert exact
    assert outline.covers(Point(12.6, 14.6))  # inside the cut corner
    assert not outline.covers(Point(12.68, 14.68))  # past the cut line x + y = 27.1
    assert not outline.covers(Point(10.98, 12.98))  # 1.02 mm from the centre: past the round
    circle = Point(11.7, 13.7).buffer(1.0, quad_segs=32)
    corner = Polygon([(12.7, 13.7), (12.7, 14.6), (12.6, 14.7), (11.7, 14.7)])
    assert outline.symmetric_difference(circle.union(corner)).area == pytest.approx(0.0, abs=1e-9)


def test_uncut_corners_keep_the_authored_rounding(tmp_path: Path):
    """With no ``(roundrect_rratio)`` the uncut corners round at KiCad's default 0.25."""
    from shapely.geometry import Point

    outline, exact = _offset_pad_outline(tmp_path, "(chamfer_ratio 0.14) (chamfer bottom_right)")
    assert exact
    assert not outline.covers(Point(10.8, 12.8))  # inside a rounded corner's box
    assert outline.covers(Point(10.75, 13.5))
    rounded = 4.0 - 3 * (1.0 - math.pi / 4) * 0.5**2
    assert outline.area == pytest.approx(rounded - _CUT_AREA_LOSS, abs=1e-3)


@pytest.mark.parametrize(
    ("plain", "tokens"),
    [
        (_SQUARE, f"{_SQUARE} (chamfer_ratio 0.14)"),  # a ratio without a corner selection
        (
            _SQUARE,
            f"{_SQUARE} (chamfer_ratio 0) (chamfer bottom_right)",
        ),  # a selection without a cut
        (_SQUARE, f"{_SQUARE} (chamfer_ratio 0.14) (chamfer)"),  # an empty selection
        # Default rounding: every corner rounded by the shared construction.
        ("", "(chamfer_ratio 0.14) (chamfer)"),
    ],
    ids=["no-selection", "zero-ratio", "empty-selection", "empty-selection-rounded"],
)
def test_without_a_cut_corner_the_roundrect_is_ordinary(tmp_path: Path, plain: str, tokens: str):
    plain_outline, _ = _offset_pad_outline(tmp_path, plain)
    outline, exact = _offset_pad_outline(tmp_path, tokens)
    assert exact
    assert outline.symmetric_difference(plain_outline).area == pytest.approx(0.0, abs=1e-9)
