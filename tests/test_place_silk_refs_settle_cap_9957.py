"""The ``settle`` loop of ``SilkRefPlacer.plan`` is bounded (Issue #9957).

``plan(settle=True)`` retries low-association and failed references once
their neighbours have moved; ``legalize_silk`` runs it on every board it
writes.  The loop had no round counter, and the issue suspected two
low-association references could displace each other forever.

They cannot.  Every candidate is tested against the other references'
*current* boxes, so a move never lands on a position that already clears;
a placed reference then moves only to a strictly larger courtyard margin,
over a fixed candidate set.  The regression test below drives the suspected
layout and asserts the call returns with neither reference displaced.  The
cap is kept for the residual case spelled out at ``_SETTLE_MAX_ROUNDS``.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.silkscreen import place_refs
from kicad_tools.silkscreen.place_refs import PlaceSilkRefsResult, SilkRefPlacer

pytest.importorskip("shapely")

HEADER = """(kicad_pcb
  (version 20240108) (generator "test") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_line (start 0 0) (end 60 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_line (start 60 0) (end 60 40) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_line (start 60 40) (end 0 40) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_line (start 0 40) (end 0 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
"""


def _fp(ref: str, pos: tuple[float, float], ref_local: tuple[float, float]) -> str:
    return (
        f'(footprint "FP" (layer "F.Cu") (at {pos[0]} {pos[1]}) '
        f'(fp_text reference "{ref}" (at {ref_local[0]} {ref_local[1]}) (layer "F.SilkS") '
        "(effects (font (size 1 1) (thickness 0.15)))) "
        "(fp_rect (start -0.5 -0.3) (end 0.5 0.3) (stroke (width 0.05) (type solid)) "
        '(layer "F.CrtYd") (fill none)))'
    )


def _write(tmp_path: Path, *footprints: str) -> Path:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(HEADER + "\n".join(footprints) + ")\n")
    return path


def _positions(result: PlaceSilkRefsResult) -> dict[str, tuple[float, float]]:
    return {p.footprint_ref: p.new_position for p in result.placements}


def _blocking_pair() -> list[str]:
    """A 1.3 mm-pitch row whose two inner labels start over each other.

    B and C each have to move in the first round, and nothing they can reach
    is 0.4 mm nearer its own courtyard than a neighbour's, so both stay
    low-association and both go on the retry list.
    """
    return [
        _fp("A", (20.0, 20.0), (0.0, -1.0)),
        _fp("B", (21.3, 20.0), (0.6, -1.0)),
        _fp("C", (22.6, 20.0), (-0.6, -1.0)),
        _fp("D", (23.9, 20.0), (0.0, -1.0)),
    ]


def _lattice() -> list[str]:
    """A 4x4 lattice whose labels compete for the same gaps.

    One pass is not enough: a reference only sees the better candidate once
    its neighbour has moved, so a second round still moves references.
    """
    footprints = []
    for row in range(4):
        for col in range(4):
            angle = math.degrees(math.atan2(2 - row, 2 - col))
            dx = 0.6 * math.cos(math.radians(angle))
            dy = -0.6 * math.sin(math.radians(angle))
            footprints.append(_fp(f"G{row}{col}", (20.0 + col * 2.0, 20.0 + row * 2.0), (dx, dy)))
    return footprints


def test_unsettled_refs_defaults_to_an_independent_empty_list():
    first = PlaceSilkRefsResult(clearance_mm=0.15)
    second = PlaceSilkRefsResult(clearance_mm=0.15)
    assert first.unsettled_refs == []
    first.unsettled_refs.append("R1")
    assert second.unsettled_refs == []


@pytest.mark.timeout(120)
def test_mutually_blocking_low_association_pair_settles(tmp_path: Path):
    placer = SilkRefPlacer(_write(tmp_path, *_blocking_pair()))

    result = placer.plan(settle=True, only={"B", "C"})

    by_ref = {p.footprint_ref: p for p in result.placements}
    # The fixture reproduces the issue's precondition: both labels moved in
    # the first round and remain poorly associated, so both are retried.
    assert {ref: p.status for ref, p in by_ref.items()} == {"B": "moved", "C": "moved"}
    assert by_ref["B"].low_association and by_ref["C"].low_association
    # The retry round displaces neither: it is the same plan as one pass.
    assert result.unsettled_refs == []
    single = placer.plan(settle=False, only={"B", "C"})
    assert _positions(result) == _positions(single)


def test_settling_never_lowers_a_placed_reference(tmp_path: Path):
    """A reference that already clears only ever moves to a larger margin."""
    for build in (_blocking_pair, _lattice):
        placer = SilkRefPlacer(_write(tmp_path, *build()))
        single = {p.footprint_ref: p for p in placer.plan(settle=False).placements}
        settled = {p.footprint_ref: p for p in placer.plan(settle=True).placements}
        assert settled.keys() == single.keys()
        for ref, before in single.items():
            after = settled[ref]
            if before.status in {"moved", "unchanged"}:
                assert after.status in {"moved", "unchanged"}, (build, ref)
            if before.association_margin_mm is not None:
                assert after.association_margin_mm is not None
                assert after.association_margin_mm >= before.association_margin_mm - 1e-9, (
                    build,
                    ref,
                )


def test_settled_lattice_converges_and_needs_a_second_moving_round(tmp_path: Path):
    placer = SilkRefPlacer(_write(tmp_path, *_lattice()))
    full = placer.plan(settle=True)
    single = placer.plan(settle=False)
    assert full.unsettled_refs == []
    assert len(full.placements) == 16
    assert _positions(full) != _positions(single)


def test_settle_off_never_reports_unsettled_references(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(place_refs, "_SETTLE_MAX_ROUNDS", 1)
    result = SilkRefPlacer(_write(tmp_path, *_lattice())).plan(settle=False)
    assert any(p.low_association for p in result.placements), "retry list would not be empty"
    assert result.unsettled_refs == []


def test_cap_reports_the_references_still_retrying(tmp_path: Path, monkeypatch):
    path = _write(tmp_path, *_lattice())
    full = SilkRefPlacer(path).plan(settle=True)

    monkeypatch.setattr(place_refs, "_SETTLE_MAX_ROUNDS", 1)
    capped = SilkRefPlacer(path).plan(settle=True)

    assert capped.unsettled_refs, "one round is not enough for this lattice"
    assert len(capped.placements) == len(full.placements)
    assert set(capped.unsettled_refs) <= set(_positions(capped))
    # Stopped after the first round: the second round's improvements are missing.
    assert _positions(capped) != _positions(full)
    assert sorted(capped.unsettled_refs) == sorted(
        p.footprint_ref
        for p in capped.placements
        if p.low_association or p.status in {"unplaceable", "under_component_fallback"}
    )


def test_cap_on_the_last_moving_round_keeps_the_plan_but_still_reports(tmp_path: Path, monkeypatch):
    path = _write(tmp_path, *_lattice())
    full = SilkRefPlacer(path).plan(settle=True)

    monkeypatch.setattr(place_refs, "_SETTLE_MAX_ROUNDS", 2)
    capped = SilkRefPlacer(path).plan(settle=True)

    # Round 2 held the last moves, so the plan matches the converged one; the
    # planner cannot know round 3 would have been still, so it reports.
    assert _positions(capped) == _positions(full)
    assert capped.unsettled_refs


def test_cap_with_a_single_pending_reference(tmp_path: Path, monkeypatch):
    path = _write(tmp_path, *_blocking_pair())

    monkeypatch.setattr(place_refs, "_SETTLE_MAX_ROUNDS", 1)
    capped = SilkRefPlacer(path).plan(settle=True, only={"B"})
    (placement,) = capped.placements
    assert placement.footprint_ref == "B"
    assert placement.low_association, "fixture must keep B on the retry list"
    assert capped.unsettled_refs == ["B"]

    monkeypatch.undo()
    settled = SilkRefPlacer(path).plan(settle=True, only={"B"})
    assert _positions(settled) == _positions(capped)
    assert settled.unsettled_refs == []


def test_cap_with_no_pending_references(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(place_refs, "_SETTLE_MAX_ROUNDS", 1)
    result = SilkRefPlacer(_write(tmp_path)).plan(settle=True)
    assert result.placements == []
    assert result.unsettled_refs == []
