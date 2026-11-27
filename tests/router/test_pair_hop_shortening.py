"""Same-footprint differential-pair hops keep their pad escapes and clear nearby vias."""

import math

import pytest

from kicad_tools.router.pairplan import tie
from kicad_tools.router.pairplan.geometry import seg_pt_dist


def test_aligned_pad_centers_take_the_clear_straight_hop():
    # The lattice entered the pads along different axes on the seed-42 board.
    points = [(0, 0), (0.15, 0.15), (0.15, 1.85), (-0.05, 2.05), (-0.05, 2.225), (0, 2.275)]
    checked = []

    def clear(a, b):
        checked.append((a, b))
        return True

    assert tie.shorter_pair_hop(points, clear) == [points[0], points[-1]]
    assert checked == [(points[0], points[-1])]


@pytest.mark.parametrize("reserved_lane", [False, True], ids=["foreign-via", "future-pour-via"])
def test_blocked_straight_hop_keeps_the_searched_pad_entries(reserved_lane):
    points = [(0, 0), (0.25, 0.25), (0.5, 0.5), (0.5, 1.5), (0.25, 1.75), (0, 2)]

    def clear(a, b):
        return reserved_lane or seg_pt_dist(a, b, (0, 1)) >= 0.2

    # A future pour via has no copper to probe yet. Its reserved lane limits
    # how far any shortcut can move away from the searched path.
    result = tie.shorter_pair_hop(points, clear, max_shift=0.3 if reserved_lane else None)
    assert result is not None
    assert result[:2] == points[:2] and result[-2:] == points[-2:]
    assert min(seg_pt_dist(a, b, (0, 1)) for a, b in zip(result, result[1:], strict=False)) >= 0.2


@pytest.mark.parametrize("rotate", [False, True])
def test_hop_takes_a_short_octilinear_path_around_the_reserved_barrel(rotate):
    def xy(x, y):
        return (-y, x) if rotate else (x, y)

    points = [
        xy(*p) for p in [(0, 0), (0, 0.15), (0.7, 0.85), (0.7, 1.75), (0.2, 2.25), (0.2, 2.4)]
    ]
    via = xy(-0.32, 1.1)

    def clear(a, b):
        return seg_pt_dist(a, b, via) >= 0.55 - 1e-9

    result = tie.shorter_pair_hop(points, clear)
    assert result is not None
    assert result[:2] == points[:2] and result[-2:] == points[-2:]
    assert all(clear(a, b) for a, b in zip(result, result[1:], strict=False))
    assert all(
        abs(a[0] - b[0]) < 1e-8
        or abs(a[1] - b[1]) < 1e-8
        or abs(abs(a[0] - b[0]) - abs(a[1] - b[1])) < 1e-8
        for a, b in zip(result, result[1:], strict=False)
    )
    assert sum(math.dist(a, b) for a, b in zip(result, result[1:], strict=False)) < 2.7
    assert tie.shorter_pair_hop(points, lambda _a, _b: False) is None


@pytest.mark.parametrize("mirror", [1, -1])
def test_unplaced_neighboring_via_keeps_a_clear_lane(mirror):
    # The stitched via is still absent when the router chooses the hop.
    # A free-space shortcut crosses its landing; staying near the searched
    # corridor keeps room for the barrel, in either pin orientation.
    points = [
        (mirror * x, y)
        for x, y in [(0, 0), (0, 0.15), (0.7, 0.85), (0.7, 1.75), (0.2, 2.25), (0.2, 2.4)]
    ]
    via = (-0.32 * mirror, 1.1)

    def gap(run):
        return min(seg_pt_dist(a, b, via) for a, b in zip(run, run[1:], strict=False))

    direct = tie.shorter_pair_hop(points, lambda _a, _b: True)
    guarded = tie.shorter_pair_hop(points, lambda _a, _b: True, max_shift=0.4)
    assert direct and guarded
    assert gap(direct) < 0.55 <= gap(guarded)
    assert sum(map(math.dist, guarded, guarded[1:])) < sum(map(math.dist, points, points[1:]))


def test_only_the_nearby_plane_reservation_limits_the_hop():
    from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask
    from tests.router.lattice.test_pair_gateway_pass_under_6836 import _dual_row_board

    pf, _ = _dual_row_board()
    site = KeepoutArea(
        polygon=((-0.5, 0), (0, 0.5), (0.5, 0), (0, -0.5)),
        layers=frozenset({0}),
        blocks_tracks=True,
        blocks_vias=True,
        exempt=frozenset({1, 9}),
        name="plane-via-site:GND:J1.2",
    )
    pf.set_keepouts(LatticeKeepoutMask([site]))
    pf.pair_exempt_added = {site.name: {1}}
    mask, shift = pf._pair_hop_keepouts((0.9, -1), (0.9, 1), 1, 0.1)
    assert shift == 0.5
    assert mask and mask.areas[0].applies_to(1)
    assert not mask.areas[0].applies_to(9)
    assert pf._pair_hop_keepouts((4, -1), (4, 1), 1, 0.1)[1] is None
    assert pf._pair_hop_keepouts((0.9, -1), (0.9, 1), 9, 0.1)[1] is None
