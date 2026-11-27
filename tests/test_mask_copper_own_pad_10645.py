"""One verdict for a straight own-pad entry, wherever its author split it (issue #10645).

The ``within_own_pad`` class (issue #8858) grants one pad-edge crossing to a
straight segment with an authored end strictly inside the pad. A track divided
exactly on the pad edge has no such end on its outer piece, so that piece read
as an error while the identical unsplit entry -- and the same split 0.2 mm
further in -- read as info. The outer piece now follows its authored
continuation (same net, layer and width, collinear, the only two same-net
segments at each joint); when the chain ends inside the pad the piece gets the
same allowance, still measured on its own copper.

The issue's control -- an outer piece with no continuation -- and every other
negative keep their verdict. Geometry is synthetic at the issue's scale: U1/1
a 2 x 1 mm pad at (10, 10), J1/1 a 1 x 1 mm pad at (7, 10), one net, 0.2 mm
tracks along y = 10.
"""

import pytest
from shapely.geometry import LineString, box

from kicad_tools.validate.mask_copper import (
    MaskCopperPolicy,
    assess_attributed_geometry,
    assessment_results,
)

# Interim JLCPCB values written literally (issue #8842), as in the #8858 suite.
POLICY = MaskCopperPolicy(
    clearance_mm=0.09,
    source="order",
    process="process",
    revision="rev",
    warning_clearance_mm=0.15,
)
U1 = "00000000-0000-0000-0000-000000000001"
J1 = "00000000-0000-0000-0000-000000000002"
U1_PAD = box(9.0, 9.5, 11.0, 10.5)
J1_PAD = box(6.5, 9.5, 7.5, 10.5)
Y = 10.0


def _board(*tracks, margin=0.0, max_error=0.0):
    """U1 and J1 (one net) and straight tracks along y = 10.

    A track is ``(a, b)`` of authored ends, or ``(a, b, options)`` overriding
    ``width``, ``side`` or ``net``. The tracks are the objects ``t0`` ...
    """
    from shapely.ops import unary_union

    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    def pad(identity, shape, ref):
        opening = shape.buffer(margin, join_style="mitre") if margin else shape
        return identity, {
            "kind": "pad:rect",
            "net": "N1",
            "pad_ref": ref,
            "layers_geometry": {"F.Mask": opening, "F.Cu": shape},
            "margin_mm": {"F.Mask": margin},
        }

    objects = dict((pad(U1, U1_PAD, "U1/1"), pad(J1, J1_PAD, "J1/1")))
    coppers = [U1_PAD, J1_PAD]
    for position, track in enumerate(tracks):
        a, b = track[0], track[1]
        options = track[2] if len(track) > 2 else {}
        width = options.get("width", 0.2)
        side = options.get("side", "F")
        copper = LineString([a, b]).buffer(width / 2, quad_segs=64)
        objects[f"t{position}"] = {
            "kind": "segment",
            "net": options.get("net", "N1"),
            "layers_geometry": {f"{side}.Cu": copper},
            "margin_mm": {},
            "width_mm": width,
            "endpoints_mm": (tuple(a), tuple(b)),
        }
        if side == "F":
            coppers.append(copper)
    exported = ExportedMaskGeometry(
        "a" * 64,
        None,
        None,
        "10.0.6",
        {},
        layers={
            "F.Mask": unary_union(
                [objects[U1]["layers_geometry"]["F.Mask"], objects[J1]["layers_geometry"]["F.Mask"]]
            ),
            "F.Cu": unary_union(coppers),
        },
    )
    geometry = AttributedMaskGeometry(exported, objects)
    if max_error:
        geometry.provenance["max_error_mm_per_construction"] = max_error
    return geometry


def _at_u1(geometry):
    """``(result, {track: measurement})`` for every track measured against U1/1."""
    result = assess_attributed_geometry(geometry, POLICY)
    assert result.coverage == "complete"
    return result, {m.conductor_uuid: m for m in result.measurements if m.opening_uuids == (U1,)}


def _severity(result, conductor):
    found = [
        v.severity
        for v in assessment_results(result).violations
        if v.rule_id == "mask_to_copper" and U1 in v.items and v.items[-1] == conductor
    ]
    return found[0] if found else None


def _errors(geometry):
    """The tracks that read as ``mask_to_copper`` errors against U1/1."""
    result, measured = _at_u1(geometry)
    return sorted(t for t in measured if _severity(result, t) == "error")


# --- the issue's table -----------------------------------------------------------------


@pytest.mark.parametrize(
    "tracks,errors",
    [
        ((((7, Y), (10, Y)),), []),
        ((((7, Y), (10, Y)), ((9, Y), (10, Y))), []),  # plus a covered inner piece
        ((((7, Y), (9, Y)), ((9, Y), (10, Y))), []),  # split on the edge
        ((((7, Y), (9.2, Y)), ((9.2, Y), (10, Y))), []),  # split inside the pad
        ((((7, Y), (9, Y)),), ["t0"]),  # terminates on the edge: the control
    ],
    ids=["one-segment", "covered-piece", "split-on-edge", "split-inside", "ends-on-edge"],
)
def test_errors_at_u1_follow_the_issue_table(tracks, errors):
    assert _errors(_board(*tracks)) == errors


def test_split_on_edge_reads_within_own_pad_on_both_pieces():
    """The outside piece reads ``within_own_pad`` exactly like the single entry."""
    single, one = _at_u1(_board(((7, Y), (10, Y))))
    split, two = _at_u1(_board(((7, Y), (9, Y)), ((9, Y), (10, Y))))
    for piece in two.values():
        assert piece.disposition == "within_own_pad"
        assert piece.within_own_pad_failed_condition is None
        assert piece.beside_owner_excess_mm2 == pytest.approx(0.0, abs=1e-9)
        assert _severity(split, piece.conductor_uuid) == "info"
    assert one["t0"].disposition == two["t0"].disposition == two["t1"].disposition
    assert split.passed and single.passed
    # Each piece is still measured on its own copper: about the issue's
    # 0.0157 mm² cap for the outside piece, none of it beyond the pad.
    assert two["t0"].exposed_area_mm2 == pytest.approx(0.0157, abs=1e-5)
    assert two["t0"].exposed_beyond_owner_mm2 == pytest.approx(0.0, abs=1e-9)
    assert two["t1"].exposed_area_mm2 == pytest.approx(0.2157, abs=1e-5)


def test_edge_ending_control_keeps_condition_4():
    """The issue's control: no authored continuation, so no allowance."""
    result, measured = _at_u1(_board(((7, Y), (9, Y))))
    assert measured["t0"].disposition == "violation"
    assert measured["t0"].within_own_pad_failed_condition == 4
    assert measured["t0"].beside_owner_excess_mm2 == pytest.approx(0.018, abs=1e-4)
    assert _severity(result, "t0") == "error"
    assert not result.passed


# --- the negatives keep their verdict ---------------------------------------------------


@pytest.mark.parametrize(
    "tracks,errors",
    [
        ((((7, Y), (9, Y)), ((9, Y), (10, 11))), ["t0", "t1"]),  # continuation bends away
        ((((7, Y), (9, Y)), ((9, Y), (10, Y), {"width": 0.25})), ["t0"]),  # width differs
        ((((7, Y), (9, Y)), ((9, Y), (10, Y), {"side": "B"})), ["t0"]),  # layer differs
        ((((7, Y), (9, Y)), ((9, Y), (10, Y)), ((9, Y), (9, 12))), ["t0", "t2"]),  # branch
        ((((6, Y), (9, Y)), ((9, Y), (12, Y))), ["t0", "t1"]),  # pass-through, split
        ((((7, Y), (9, Y)), ((9, Y), (10, Y), {"net": "N2"})), ["t0", "t1"]),  # other net
    ],
    ids=["bend", "width", "layer", "branch", "pass-through-split", "net"],
)
def test_no_continuation_means_no_allowance(tracks, errors):
    assert _errors(_board(*tracks)) == errors


def test_straight_pass_through_stays_an_error():
    assert _errors(_board(((6, Y), (12, Y)))) == ["t0"]


def test_pass_through_split_on_the_edge_fails_both_pieces():
    _, measured = _at_u1(_board(((6, Y), (9, Y)), ((9, Y), (12, Y))))
    assert [m.disposition for m in measured.values()] == ["violation", "violation"]
    assert [m.within_own_pad_failed_condition for m in measured.values()] == [4, 4]


def test_branched_joint_is_not_a_continuation():
    """A third same-net segment at the split point: the joint is not unbranched."""
    _, measured = _at_u1(_board(((7, Y), (9, Y)), ((9, Y), (10, Y)), ((9, Y), (9, 12))))
    assert measured["t0"].disposition == "violation"
    assert measured["t0"].within_own_pad_failed_condition == 4
    # The inner piece ends inside the pad on its own account and keeps its class.
    assert measured["t1"].disposition == "within_own_pad"


def test_continuation_never_excuses_copper_exposed_beyond_the_pad():
    """A positive expansion still fails condition 3 on the piece's own copper."""
    _, measured = _at_u1(
        _board(((7, Y), (9, Y)), ((9, Y), (10, Y)), margin=0.05),
    )
    assert measured["t0"].disposition == "violation"
    assert measured["t0"].within_own_pad_failed_condition == 3
    assert measured["t0"].exposed_beyond_owner_mm2 > 0


# --- longer chains, tolerance and termination ------------------------------------------


def test_chain_of_three_collinear_segments_qualifies():
    """Both outside pieces reach the pad through the chain, one of them in two steps."""
    result, measured = _at_u1(_board(((8.9, Y), (8.95, Y)), ((8.95, Y), (9, Y)), ((9, Y), (10, Y))))
    assert set(measured) == {"t0", "t1", "t2"}
    assert {m.disposition for m in measured.values()} == {"within_own_pad"}
    assert result.passed


@pytest.mark.parametrize("where", ["joint", "far end"])
@pytest.mark.parametrize("offset,errors", [(0.005, []), (0.02, ["t0"])])
def test_joint_and_collinearity_tolerate_the_construction_budget_only(where, offset, errors):
    """Within the budget (10 um at max_error 0.005) is the same straight entry; past it is not."""
    near = (9 + offset, Y + offset) if where == "joint" else (9, Y)
    far = (10, Y + offset) if where == "far end" else (10, Y)
    assert _errors(_board(((7, Y), (9, Y)), (near, far), max_error=0.005)) == errors


def test_chain_walk_terminates_when_two_pieces_continue_each_other():
    """Pieces shorter than the joint tolerance each rejoin the other: no allowance, no loop.

    Neither piece reaches the pad's core (10 um inside its outline at
    max_error 0.005), so the walk must give up rather than circle.
    """
    board = _board(((7, Y), (9, Y)), ((9, Y), (9.005, Y)), max_error=0.005)
    assert _errors(board) == ["t0", "t1"]
