"""Wire-list sidecar for ``kct pcb reinforce`` (Issue #8744)."""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli import main
from kicad_tools.cli.commands.pcb import run_pcb_command
from kicad_tools.pcb.reinforcement_sidecar import (
    check_wire_records,
    drop_wires,
    is_parallel,
    load_wire_records,
    load_wires,
    load_wires_if_present,
    merge_wires,
    sidecar_path,
    wire_key,
    write_sidecar,
)
from kicad_tools.physics.wire_gauge import bare_copper_diameter_mm
from kicad_tools.schema.pcb import PCB


def _two_net_pcb(length: float = 100.0) -> PCB:
    pcb = PCB.create(width=200, height=100, center=False)
    for net, y in (("NET_A", 30.0), ("NET_B", 70.0)):
        pcb.add_trace((20.0, y), (20.0 + length, y), width=2.0, layer="F.Cu", net=net)
    return pcb


def _args(pcb_path: Path, net: str | None = "NET_A", **kw) -> argparse.Namespace:
    base = {
        "pcb_command": "reinforce",
        "pcb": str(pcb_path),
        "net": net,
        "wire_gauge": 16,
        "layer": None,
        "output": None,
        "format": "text",
        "dry_run": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _board(tmp_path: Path) -> Path:
    p = tmp_path / "board.kicad_pcb"
    _two_net_pcb().save(p)
    return p


def test_straight_run_one_wire(tmp_path):
    p = _board(tmp_path)
    assert run_pcb_command(_args(p)) == 0
    wires = load_wires(sidecar_path(p))
    assert len(wires) == 1
    w = wires[0]
    assert w["net"] == "NET_A" and w["layer"] == "F.Cu" and w["status"] == "full"
    assert len(w["anchors"]) == 2
    assert all(abs(a["y"] - 30.0) < 1e-3 for a in w["anchors"])
    span = math.hypot(
        w["anchors"][1]["x"] - w["anchors"][0]["x"], w["anchors"][1]["y"] - w["anchors"][0]["y"]
    )
    assert abs(w["straight_line_mm"] - span) < 1e-3
    assert 90.0 <= w["length_mm"] <= 100.0
    assert w["wire_gauge_awg"] == 16
    assert w["bare_diameter_mm"] == round(bare_copper_diameter_mm(16), 4)
    assert w["mask_opening"] is None


def test_two_nets_no_duplicates_on_rerun(tmp_path):
    p = _board(tmp_path)
    assert run_pcb_command(_args(p, "NET_A")) == 0
    assert run_pcb_command(_args(p, "NET_B")) == 0
    assert {w["net"] for w in load_wires(sidecar_path(p))} == {"NET_A", "NET_B"}
    assert run_pcb_command(_args(p, "NET_A")) == 0
    nets = [w["net"] for w in load_wires(sidecar_path(p))]
    assert sorted(nets) == ["NET_A", "NET_B"]


def test_dry_run_writes_no_sidecar(tmp_path):
    p = _board(tmp_path)
    assert run_pcb_command(_args(p, dry_run=True)) == 0
    assert not sidecar_path(p).exists()


def test_deterministic_bytes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    pa, pb = _board(a), _board(b)
    run_pcb_command(_args(pa))
    run_pcb_command(_args(pb))
    assert sidecar_path(pa).read_bytes() == sidecar_path(pb).read_bytes()


def test_merge_replaces_same_key_and_sorts():
    def w(net, x, status):
        return {
            "net": net,
            "layer": "F.Cu",
            "leg_start": {"x": x, "y": 0.0},
            "leg_end": {"x": x + 5, "y": 0.0},
            "status": status,
        }

    merged = merge_wires([w("B", 1, "old"), w("A", 1, "old")], [w("B", 1, "new")])
    assert [(m["net"], m["status"]) for m in merged] == [("A", "old"), ("B", "new")]


def test_list_wires_read_only(tmp_path, capsys):
    p = _board(tmp_path)
    run_pcb_command(_args(p))
    before = sidecar_path(p).read_bytes()
    pcb_before = p.read_bytes()
    capsys.readouterr()
    assert run_pcb_command(_args(p, net=None, list_wires=True, format="json")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["wires"]) == 1
    assert sidecar_path(p).read_bytes() == before and p.read_bytes() == pcb_before


def test_list_wires_missing_sidecar(tmp_path):
    p = _board(tmp_path)
    assert run_pcb_command(_args(p, net=None, list_wires=True)) == 1


def test_json_embeds_wires(tmp_path, capsys):
    p = _board(tmp_path)
    assert run_pcb_command(_args(p, format="json")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["wires"] == load_wires(sidecar_path(p))


# --------------------------------------------------------------------------
# Issue #8915: a leg below the minimum wire length is not on the cut list
# --------------------------------------------------------------------------


def _short_board(tmp_path: Path, length: float) -> Path:
    p = tmp_path / "short.kicad_pcb"
    _two_net_pcb(length=length).save(p)
    return p


def test_leg_below_min_wire_length_writes_no_record(tmp_path):
    """The owner does not want a wire on a sub-15 mm leg, so it is absent from
    the cut-and-bend list -- not listed with an empty ``anchors``."""
    p = _short_board(tmp_path, 12.0)
    assert run_pcb_command(_args(p)) == 0
    # No wire at all on the net -> nothing to write, so no sidecar is created.
    assert not sidecar_path(p).exists()


def test_min_wire_length_zero_still_writes_the_record(tmp_path):
    """Control for the test above: the record's absence is the minimum's doing,
    not something else about a 12 mm leg."""
    p = _short_board(tmp_path, 12.0)
    assert run_pcb_command(_args(p, min_wire_length=0.0)) == 0
    wires = load_wires(sidecar_path(p))
    assert [(w["net"], w["status"], len(w["anchors"])) for w in wires] == [("NET_A", "full", 2)]


def test_short_leg_does_not_displace_another_net_s_wire(tmp_path):
    """A skipped leg must not be written, and must not disturb the entries a
    previous pass wrote for a different net."""
    p = tmp_path / "mixed.kicad_pcb"
    pcb = PCB.create(width=200, height=100, center=False)
    # NET_A is long enough for a wire; NET_B is below the minimum.
    pcb.add_trace((20.0, 30.0), (120.0, 30.0), width=2.0, layer="F.Cu", net="NET_A")
    pcb.add_trace((20.0, 70.0), (32.0, 70.0), width=2.0, layer="F.Cu", net="NET_B")
    pcb.save(p)

    assert run_pcb_command(_args(p, net="NET_A")) == 0
    after_a = load_wires(sidecar_path(p))
    assert [w["net"] for w in after_a] == ["NET_A"]

    assert run_pcb_command(_args(p, net="NET_B")) == 0
    after_b = load_wires(sidecar_path(p))
    assert after_b == after_a, "NET_B has no wire; NET_A's entry must be untouched"


def test_unanchored_leg_that_still_wants_a_wire_keeps_its_record(tmp_path):
    """Only the length filter removes a record. A leg left unanchored for any
    other reason still names copper that WANTS a wire, so it stays on the list
    with its reason -- the pre-#8915 behaviour."""
    p = tmp_path / "unselected.kicad_pcb"
    pcb = PCB.create(width=200, height=100, center=False)
    # Two legs on one net, both over the minimum; without --all-runs only the
    # longest is anchored and the other is reported ``unanchored``.
    pcb.add_trace((20.0, 30.0), (120.0, 30.0), width=2.0, layer="F.Cu", net="NET_A")
    pcb.add_trace((20.0, 70.0), (60.0, 70.0), width=2.0, layer="F.Cu", net="NET_A")
    pcb.save(p)

    assert run_pcb_command(_args(p, net="NET_A")) == 0
    wires = load_wires(sidecar_path(p))
    statuses = sorted(w["status"] for w in wires)
    assert statuses == ["full", "unanchored"], wires
    unanchored = next(w for w in wires if w["status"] == "unanchored")
    assert unanchored["anchors"] == []
    assert "only the longest leg" in unanchored["reason"]


# --------------------------------------------------------------------------
# Issue #8916: parallel-copy records
# --------------------------------------------------------------------------


def test_absent_strategy_reads_as_wire():
    """Pre-#8916 records carry no ``strategy``; they stay wires (no version bump)."""
    from kicad_tools.pcb.reinforcement_sidecar import SIDECAR_VERSION, is_parallel

    assert SIDECAR_VERSION == 1
    assert not is_parallel({"net": "N", "layer": "F.Cu"})
    assert is_parallel({"net": "N", "layer": "F.Cu", "strategy": "parallel"})


def test_parallel_record_key_ignores_which_side_it_was_met_from():
    """A re-run that meets the leg from its copy's side replaces the record."""
    from kicad_tools.pcb.reinforcement_sidecar import wire_key

    def w(layer, copy_layer):
        return {
            "net": "N",
            "layer": layer,
            "copy_layer": copy_layer,
            "strategy": "parallel",
            "leg_start": {"x": 0.0, "y": 0.0},
            "leg_end": {"x": 12.0, "y": 0.0},
        }

    assert wire_key(w("B.Cu", "F.Cu")) == wire_key(w("F.Cu", "B.Cu"))
    assert len(merge_wires([w("B.Cu", "F.Cu")], [w("F.Cu", "B.Cu")])) == 1
    wire = {k: v for k, v in w("B.Cu", "F.Cu").items() if k not in ("strategy", "copy_layer")}
    assert wire_key(wire) != wire_key(w("B.Cu", "F.Cu"))


def test_parallel_short_leg_is_recorded_and_cleared(tmp_path):
    """A 12 mm leg below the #8915 minimum: a wire gets no record, a copy does,
    and --clear removes the copy's copper and its record."""
    p = _short_board(tmp_path, 12.0)
    assert run_pcb_command(_args(p, strategy="parallel")) == 0
    wires = load_wires(sidecar_path(p))
    assert [(w["net"], w["strategy"], w["copy_layer"]) for w in wires] == [
        ("NET_A", "parallel", "B.Cu")
    ]
    assert any(s.layer == "B.Cu" for s in PCB.load(p).segments)
    assert run_pcb_command(_args(p, clear=True)) == 0
    assert not any(s.layer == "B.Cu" for s in PCB.load(p).segments)
    assert load_wires(sidecar_path(p)) == []


# ----------------------------- an entry that is not an object (Issue #10401)


def _reinforced(tmp_path: Path, *nets: str) -> tuple[Path, list[dict]]:
    """The two-net board after reinforcing each of ``nets``: ``(board, its records)``."""
    p = _board(tmp_path)
    for net in nets:
        assert run_pcb_command(_args(p, net)) == 0
    return p, load_wires(sidecar_path(p))


def _rewrite_sidecar(board: Path, entries: list) -> Path:
    """Put a version-1 sidecar of raw ``entries`` beside ``board``; its path."""
    sc = sidecar_path(board)
    sc.write_text(json.dumps({"version": 1, "wires": entries}, indent=2) + "\n", encoding="utf-8")
    return sc


def _snapshot(*paths: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in paths}


def _assert_refused(err: str, sidecar: Path, index: int) -> None:
    """The diagnostic names the sidecar, the first offending entry and what it should be."""
    assert str(sidecar) in err
    assert f"wires[{index}]" in err
    assert "JSON object" in err


_NON_OBJECTS = [
    pytest.param(None, id="null"),
    pytest.param(1.5, id="number"),
    pytest.param("oops", id="string"),
    pytest.param([{"net": "NET_B"}], id="array"),
    pytest.param(False, id="boolean"),
]


@pytest.mark.parametrize("bad", _NON_OBJECTS)
def test_reinforce_refuses_a_non_object_entry_after_another_nets_record(tmp_path, capsys, bad):
    """Add reads the sidecar again to merge its record; the merge used to raise."""
    p, (_net_a, net_b) = _reinforced(tmp_path, "NET_A", "NET_B")
    sidecar = _rewrite_sidecar(p, [net_b, bad])
    before = _snapshot(p, sidecar)
    capsys.readouterr()
    assert run_pcb_command(_args(p, "NET_A")) == 1
    captured = capsys.readouterr()
    _assert_refused(captured.err, sidecar, 1)
    assert captured.out == ""
    assert _snapshot(p, sidecar) == before


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("bad", _NON_OBJECTS)
def test_clear_refuses_a_non_object_entry_instead_of_falling_back_to_the_size_rule(
    tmp_path, capsys, bad, dry_run
):
    """The list names the anchors to remove, so it is never skipped over as unreadable
    -- under --dry-run too, which reads it just the same."""
    p, (net_a, net_b) = _reinforced(tmp_path, "NET_A", "NET_B")
    sidecar = _rewrite_sidecar(p, [net_b, net_a, bad])
    before = _snapshot(p, sidecar)
    for net in ("NET_A", "NET_B"):
        capsys.readouterr()
        assert run_pcb_command(_args(p, net, clear=True, dry_run=dry_run)) == 1
        captured = capsys.readouterr()
        _assert_refused(captured.err, sidecar, 2)
        assert captured.out == ""
    assert _snapshot(p, sidecar) == before


@pytest.mark.parametrize("bad", _NON_OBJECTS)
def test_replace_refuses_a_non_object_entry(tmp_path, capsys, bad):
    p, (net_a, net_b) = _reinforced(tmp_path, "NET_A", "NET_B")
    sidecar = _rewrite_sidecar(p, [net_b, bad, net_a])
    before = _snapshot(p, sidecar)
    capsys.readouterr()
    assert run_pcb_command(_args(p, "NET_B", replace=True)) == 1
    captured = capsys.readouterr()
    _assert_refused(captured.err, sidecar, 1)
    assert captured.out == ""
    assert _snapshot(p, sidecar) == before


@pytest.mark.parametrize("fmt", ["text", "json"])
@pytest.mark.parametrize("bad", _NON_OBJECTS)
def test_adopt_refuses_a_non_object_entry_even_after_a_recorded_match(tmp_path, capsys, bad, fmt):
    """An entry before the matching record used to raise; one after it went unread (exit 0)."""
    p, (record,) = _reinforced(tmp_path, "NET_A")
    for entries, index in (([bad, record], 0), ([record, bad], 1)):
        sidecar = _rewrite_sidecar(p, entries)
        before = _snapshot(p, sidecar)
        capsys.readouterr()
        assert run_pcb_command(_args(p, net=None, adopt=True, format=fmt)) == 1
        captured = capsys.readouterr()
        _assert_refused(captured.err, sidecar, index)
        assert captured.out == ""
        assert _snapshot(p, sidecar) == before


@pytest.mark.parametrize("net", [None, "NET_A", "NET_B"])
@pytest.mark.parametrize("bad", _NON_OBJECTS)
def test_list_wires_text_refuses_a_non_object_entry(tmp_path, capsys, bad, net):
    """With or without --net, the text listing has to read every entry to print it."""
    p, (net_a, net_b) = _reinforced(tmp_path, "NET_A", "NET_B")
    sidecar = _rewrite_sidecar(p, [net_a, bad, net_b])
    before = sidecar.read_bytes()
    capsys.readouterr()
    assert run_pcb_command(_args(p, net, list_wires=True)) == 1
    captured = capsys.readouterr()
    _assert_refused(captured.err, sidecar, 1)
    assert captured.out == ""
    assert sidecar.read_bytes() == before


def test_list_wires_json_refuses_a_net_filter_but_dumps_the_file_unfiltered(tmp_path, capsys):
    """The list from the issue: the dump prints every entry in file order; filtering
    needs records, so --net refuses and names the first offender."""
    p, (net_a, net_b) = _reinforced(tmp_path, "NET_A", "NET_B")
    entries = [None, 1.5, net_a, "oops", [net_b], False, net_b]
    sidecar = _rewrite_sidecar(p, entries)
    before = sidecar.read_bytes()
    for net in ("NET_A", "NET_B"):
        capsys.readouterr()
        assert run_pcb_command(_args(p, net, list_wires=True, format="json")) == 1
        captured = capsys.readouterr()
        _assert_refused(captured.err, sidecar, 0)
        assert captured.out == ""
    capsys.readouterr()
    assert run_pcb_command(_args(p, net=None, list_wires=True, format="json")) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"sidecar": str(sidecar), "wires": entries}
    assert captured.err == ""
    assert sidecar.read_bytes() == before


@pytest.mark.parametrize("existing_output", [False, True])
@pytest.mark.parametrize("fmt", ["text", "json"])
def test_clear_to_an_output_with_a_bad_destination_sidecar_writes_nothing(
    tmp_path, capsys, fmt, existing_output
):
    """The output board used to be saved before ``drop_wires`` reached the sidecar
    beside it, which is not the valid one beside the input."""
    p, (net_a,) = _reinforced(tmp_path, "NET_A")
    out = tmp_path / "out.kicad_pcb"
    if existing_output:
        out.write_bytes(p.read_bytes())
    bad = _rewrite_sidecar(out, [net_a, None])
    watched = [p, sidecar_path(p), bad] + ([out] if existing_output else [])
    before = _snapshot(*watched)
    capsys.readouterr()
    assert run_pcb_command(_args(p, "NET_A", clear=True, output=str(out), format=fmt)) == 1
    captured = capsys.readouterr()
    _assert_refused(captured.err, bad, 1)
    assert captured.out == ""
    assert out.exists() is existing_output
    assert _snapshot(*watched) == before


@pytest.mark.parametrize(
    ("reinforced", "mode"),
    [("NET_B", {}), ("NET_B", {"replace": True}), ("NET_A", {"replace": True})],
    ids=["add", "replace", "replace-after-clear"],
)
def test_reinforce_to_an_output_with_a_bad_destination_sidecar_writes_nothing(
    tmp_path, capsys, reinforced, mode
):
    """The sidecar beside a ``-o`` board is merged into, or dropped from and then
    merged into; either way it refuses the command before the board is saved."""
    p, (net,) = _reinforced(tmp_path, reinforced)
    out = tmp_path / "out.kicad_pcb"
    bad = _rewrite_sidecar(out, [net, "oops"])
    before = _snapshot(p, sidecar_path(p), bad)
    capsys.readouterr()
    assert run_pcb_command(_args(p, "NET_A", output=str(out), **mode)) == 1
    captured = capsys.readouterr()
    _assert_refused(captured.err, bad, 1)
    assert captured.out == ""
    assert not out.exists()
    assert _snapshot(p, sidecar_path(p), bad) == before


def test_writers_refuse_a_non_object_entry_and_leave_the_file_alone(tmp_path):
    """``write_sidecar`` and ``drop_wires`` rewrite the file from what they read, so
    they refuse the list instead of raising mid-merge or writing around the entry."""
    from kicad_tools.pcb.reinforcement_sidecar import (
        check_wire_records,
        drop_wires,
        load_wire_records,
        write_sidecar,
    )

    p, (net_a, net_b) = _reinforced(tmp_path, "NET_A", "NET_B")
    sidecar = _rewrite_sidecar(p, [net_b, None])
    before = sidecar.read_bytes()
    with pytest.raises(ValueError, match=r"wires\[1\] is null, not a JSON object"):
        load_wire_records(sidecar)
    with pytest.raises(ValueError, match=r"wires\[1\]"):
        write_sidecar(p, [net_a])
    with pytest.raises(ValueError, match=r"wires\[1\]"):
        drop_wires(sidecar, "NET_B", [(a["x"], a["y"]) for a in net_b["anchors"]])
    assert sidecar.read_bytes() == before
    assert check_wire_records(sidecar, [net_b, net_a]) is None
    assert check_wire_records(sidecar, []) is None


def test_empty_list_and_legacy_object_records_keep_their_behaviour(tmp_path, capsys):
    """Both are valid: every consumer reads them as before."""
    legacy = {
        "net": "NET_B",
        "layer": "F.Cu",
        "leg_start": {"x": 20.0, "y": 70.0},
        "leg_end": {"x": 120.0, "y": 70.0},
        "anchors": [{"x": 20.0, "y": 70.0}, {"x": 120.0, "y": 70.0}],
        "length_mm": 100.0,
        "wire_gauge_awg": 16,
        "status": "full",
    }
    p = _board(tmp_path)
    sidecar = _rewrite_sidecar(p, [])
    capsys.readouterr()
    assert run_pcb_command(_args(p, net=None, list_wires=True)) == 0
    assert "No wires recorded." in capsys.readouterr().out
    for net in (None, "NET_A"):
        assert run_pcb_command(_args(p, net=net, list_wires=True, format="json")) == 0
        assert json.loads(capsys.readouterr().out)["wires"] == []
    assert run_pcb_command(_args(p, net=None, adopt=True)) == 0
    assert run_pcb_command(_args(p, "NET_A", clear=True)) == 0
    assert load_wires(sidecar) == []

    _rewrite_sidecar(p, [legacy])
    assert run_pcb_command(_args(p, net=None, list_wires=True)) == 0
    out = capsys.readouterr().out
    assert "1 wire(s):" in out and "NET_B F.Cu 100.0 mm 16 AWG [full]" in out
    assert run_pcb_command(_args(p, net="NET_B", list_wires=True, format="json")) == 0
    assert json.loads(capsys.readouterr().out)["wires"] == [legacy]
    assert run_pcb_command(_args(p, net=None, adopt=True)) == 0
    assert run_pcb_command(_args(p)) == 0
    assert [w["net"] for w in load_wires(sidecar)] == ["NET_A", "NET_B"]
    assert next(w for w in load_wires(sidecar) if w["net"] == "NET_B") == legacy
    assert run_pcb_command(_args(p, "NET_A", clear=True)) == 0
    assert load_wires(sidecar) == [legacy]


# ------------------------------------------------------- --adopt (Issue #9419)


def _reinforced_board_without_sidecar(tmp_path: Path):
    """A board ``pcb reinforce`` has already wired (strips + anchors), sidecar deleted."""
    p = _board(tmp_path)
    assert run_pcb_command(_args(p, "NET_A")) == 0
    original = load_wires(sidecar_path(p))
    sidecar_path(p).unlink()
    return p, original


def _board_in(directory: Path) -> Path:
    directory.mkdir()
    q, _ = _reinforced_board_without_sidecar(directory)
    return q


def test_adopt_records_wires_already_on_the_board(tmp_path, capsys):
    p, original = _reinforced_board_without_sidecar(tmp_path)
    pcb_before = p.read_bytes()
    capsys.readouterr()
    assert run_pcb_command(_args(p, net=None, adopt=True)) == 0
    # Nothing on the board moves, and the board file is not rewritten.
    assert p.read_bytes() == pcb_before
    wires = load_wires(sidecar_path(p))
    assert len(wires) == len(original) == 1
    w, o = wires[0], original[0]
    assert (w["net"], w["layer"], w["wire_gauge_awg"]) == (o["net"], o["layer"], 16)
    assert w["status"] == "adopted"
    assert sorted((a["x"], a["y"]) for a in w["anchors"]) == sorted(
        (a["x"], a["y"]) for a in o["anchors"]
    )
    assert abs(w["straight_line_mm"] - o["straight_line_mm"]) < 1e-3
    assert w["bare_diameter_mm"] == round(bare_copper_diameter_mm(16), 4)
    # --list-wires prints it.
    capsys.readouterr()
    assert run_pcb_command(_args(p, net=None, list_wires=True)) == 0
    assert "NET_A" in capsys.readouterr().out


def test_adopt_is_idempotent_and_keeps_existing_records(tmp_path):
    p = _board(tmp_path)
    assert run_pcb_command(_args(p, "NET_A")) == 0
    before = sidecar_path(p).read_bytes()
    # The richer reinforce record (path name, leg ends) is not shadowed.
    assert run_pcb_command(_args(p, net=None, adopt=True)) == 0
    assert sidecar_path(p).read_bytes() == before
    q = _board_in(tmp_path / "again")
    assert run_pcb_command(_args(q, net=None, adopt=True)) == 0
    first = sidecar_path(q).read_bytes()
    assert run_pcb_command(_args(q, net=None, adopt=True)) == 0
    assert sidecar_path(q).read_bytes() == first


def test_center_exports_legacy_adoption_and_both_names_follow_the_next_move(tmp_path, capsys):
    from kicad_tools.cli import main
    from kicad_tools.sexp import parse_file

    board = _board(tmp_path)
    for net in ("NET_A", "NET_B"):
        assert run_pcb_command(_args(board, net)) == 0
    canonical = sidecar_path(board)
    canonical.unlink()
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    alias = review_dir / "review.kicad_pcb"
    alias.symlink_to(board)
    # Older --adopt wrote beside the supplied board name. An existing empty
    # inventory selects that same location when replaying adoption today.
    legacy = alias.with_name(alias.name + ".reinforcement.json")
    legacy.write_text('{"version": 1, "wires": []}')
    assert run_pcb_command(_args(alias, net=None, adopt=True)) == 0
    assert len(load_wires(legacy)) == 2 and not canonical.exists()

    source = tmp_path / "source.kicad_pcb"
    shutil.copyfile(board, source)
    shutil.copyfile(legacy, sidecar_path(source))
    source_pair = (source.read_bytes(), sidecar_path(source).read_bytes())
    capsys.readouterr()
    for args in ([str(source), "--paper", "keep", "-o", str(alias)], [str(board), "--paper", "A3"]):
        assert main(["pcb", "center-on-sheet", *args, "--format", "json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["dx_mm"] or report["dy_mm"]
        assert Path(report["sidecar"]).samefile(canonical)
        vias = sorted(
            tuple(v.find_child("at").get_atoms()[:2])
            for v in parse_file(board).find_children("via")
        )
        assert len(vias) == 4
        for name in (board, alias):
            assert main(["pcb", "reinforce", str(name), "--list-wires", "--format", "json"]) == 0
            wires = json.loads(capsys.readouterr().out)["wires"]
            anchors = sorted((a["x"], a["y"]) for w in wires for a in w["anchors"])
            assert len(anchors) == len(vias)
            assert all(math.dist(a, v) <= 0.0001 for a, v in zip(anchors, vias, strict=True))
        assert (source.read_bytes(), sidecar_path(source).read_bytes()) == source_pair


def test_adopt_dry_run_and_net_filter_write_nothing_extra(tmp_path):
    p, _ = _reinforced_board_without_sidecar(tmp_path)
    assert run_pcb_command(_args(p, net=None, adopt=True, dry_run=True)) == 0
    assert not sidecar_path(p).exists()
    assert run_pcb_command(_args(p, net="NET_B", adopt=True)) == 0
    assert not sidecar_path(p).exists()
    assert run_pcb_command(_args(p, net="NO_SUCH", adopt=True)) == 1


def test_adopt_reports_a_strip_without_anchor_holes(tmp_path, capsys):
    p = _board(tmp_path)
    pcb = PCB.load(str(p))
    # A bare mask strip along NET_B's trace with no anchor holes at its ends.
    pcb.add_graphic_line((30.0, 70.0), (90.0, 70.0), "F.Mask", width=2.0)
    pcb.save(p)
    capsys.readouterr()
    assert run_pcb_command(_args(p, net=None, adopt=True)) == 0
    out = capsys.readouterr().out
    assert "Adopted 0 wire(s)" in out and "Not adopted" in out
    assert not sidecar_path(p).exists()


def test_reinforce_through_board_symlink_pairs_record_with_saved_pcb(tmp_path):
    board = _board(tmp_path)
    alias = tmp_path / "review.kicad_pcb"
    alias.symlink_to(board)
    assert run_pcb_command(_args(alias, "NET_A")) == 0
    # The save detached the alias; its record sits beside it, main is untouched.
    assert not alias.is_symlink()
    assert not sidecar_path(board).exists()
    assert len(load_wires(sidecar_path(alias))) == 1


def test_replace_and_clear_through_symlink_keep_other_nets(tmp_path):
    board = _board(tmp_path)
    for net in ("NET_A", "NET_B"):
        assert run_pcb_command(_args(board, net)) == 0
    main_record = sidecar_path(board).read_bytes()
    alias = tmp_path / "review.kicad_pcb"
    alias.symlink_to(board)
    assert run_pcb_command(_args(alias, "NET_A", replace=True)) == 0
    assert sidecar_path(board).read_bytes() == main_record
    assert {w["net"] for w in load_wires(sidecar_path(alias))} == {"NET_A", "NET_B"}

    alias2 = tmp_path / "second.kicad_pcb"
    alias2.symlink_to(board)
    assert run_pcb_command(_args(alias2, "NET_A", clear=True)) == 0
    assert sidecar_path(board).read_bytes() == main_record
    assert {w["net"] for w in load_wires(sidecar_path(alias2))} == {"NET_B"}


def _centered_alias_pair(tmp_path):
    """A board symlink whose record, after in-place centering, links to the original's."""
    from kicad_tools.cli import main

    board = _board(tmp_path)
    for net in ("NET_A", "NET_B"):
        assert run_pcb_command(_args(board, net=net)) == 0
    canonical = sidecar_path(board)
    legacy_bytes = canonical.read_bytes()
    canonical.unlink()
    alias = tmp_path / "review.kicad_pcb"
    alias.symlink_to(board)
    alias.with_name(alias.name + ".reinforcement.json").write_bytes(legacy_bytes)
    assert main(["pcb", "center-on-sheet", str(alias), "--paper", "A3"]) == 0
    assert sidecar_path(alias).is_symlink()  # linked to the original board's inventory
    return board, alias


@pytest.mark.parametrize("op", ["add", "replace", "clear"])
def test_edit_through_centered_alias_detaches_its_inventory(tmp_path, op):
    board, alias = _centered_alias_pair(tmp_path)
    board_record = sidecar_path(board)
    before_pcb, before_record = board.read_bytes(), board_record.read_bytes()
    kw = {"replace": {"replace": True}, "clear": {"clear": True}}.get(op, {})
    assert run_pcb_command(_args(alias, "NET_A", **kw)) == 0
    assert not alias.is_symlink()
    assert not sidecar_path(alias).is_symlink()
    assert board.read_bytes() == before_pcb
    assert board_record.read_bytes() == before_record
    assert {w["net"] for w in load_wires(sidecar_path(alias))} >= {"NET_B"}


@pytest.mark.parametrize("op", ["add", "replace", "clear"])
def test_distinct_output_symlink_does_not_inherit_the_targets_inventory(tmp_path, op):
    src = tmp_path / "src.kicad_pcb"
    _two_net_pcb().save(src)
    if op != "add":
        assert run_pcb_command(_args(src, "NET_A")) == 0
    other = tmp_path / "other.kicad_pcb"
    _two_net_pcb().save(other)
    assert run_pcb_command(_args(other, "NET_B")) == 0
    other_record = sidecar_path(other).read_bytes()
    out = tmp_path / "out.kicad_pcb"
    out.symlink_to(other)
    kw = {"replace": {"replace": True}, "clear": {"clear": True}}.get(op, {})
    assert run_pcb_command(_args(src, "NET_A", output=str(out), **kw)) == 0
    assert not out.is_symlink()
    assert sidecar_path(other).read_bytes() == other_record
    nets = (
        {w["net"] for w in load_wires(sidecar_path(out))} if sidecar_path(out).is_file() else set()
    )
    assert "NET_B" not in nets
    assert nets == (set() if op == "clear" else {"NET_A"})


def test_spaced_edit_through_centered_alias_still_detaches_inventory(tmp_path):
    board, alias = _centered_alias_pair(tmp_path)
    board_record = sidecar_path(board)
    before = board_record.read_bytes()
    assert run_pcb_command(_args(alias, "NET_B", anchor_mode="spaced")) == 0
    assert not alias.is_symlink()
    assert not sidecar_path(alias).is_symlink()
    assert board_record.read_bytes() == before
    assert sidecar_path(alias).read_bytes() == before


def test_new_detached_record_never_widens_the_creation_mask(tmp_path):
    import os

    board = _board(tmp_path)
    assert run_pcb_command(_args(board, "NET_A")) == 0
    sidecar_path(board).chmod(0o644)
    alias = tmp_path / "review.kicad_pcb"
    alias.symlink_to(board)
    old = os.umask(0o077)
    try:
        assert run_pcb_command(_args(alias, "NET_B")) == 0
    finally:
        os.umask(old)
    assert sidecar_path(alias).stat().st_mode & 0o777 == 0o600


def _loop_pcb(net: str) -> PCB:
    """A closed rectangular loop: reinforce places no anchor, one wire is unhandled."""
    pcb = PCB.create(width=200, height=100, center=False)
    pts = [(20.0, 20.0), (80.0, 20.0), (80.0, 60.0), (20.0, 60.0), (20.0, 20.0)]
    for a, b in zip(pts, pts[1:], strict=False):
        pcb.add_trace(a, b, width=2.0, layer="F.Cu", net=net)
    return pcb


def test_zero_anchor_export_to_foreign_symlink_is_refused_untouched(tmp_path, capsys):
    """Issue #10250 review: no wire record may reach another board's list."""
    src = tmp_path / "src.kicad_pcb"
    _loop_pcb("FRESH").save(src)
    other = tmp_path / "other.kicad_pcb"
    _two_net_pcb().save(other)
    assert run_pcb_command(_args(other, "NET_B")) == 0
    other_pcb, other_record = other.read_bytes(), sidecar_path(other).read_bytes()
    out = tmp_path / "out.kicad_pcb"
    out.symlink_to(other)
    rc = run_pcb_command(_args(src, "FRESH", output=str(out), all_runs=True, format="json"))
    assert rc == 1
    assert out.is_symlink()
    assert other.read_bytes() == other_pcb
    assert sidecar_path(other).read_bytes() == other_record
    assert not out.with_name(out.name + ".reinforcement.json").exists()
    assert "symlink" in capsys.readouterr().err


@pytest.mark.parametrize("op", ["add", "replace", "clear"])
def test_conflicting_record_beside_alias_is_refused_before_any_write(tmp_path, op):
    src = tmp_path / "src.kicad_pcb"
    _two_net_pcb().save(src)
    assert run_pcb_command(_args(src, "NET_B")) == 0
    other = tmp_path / "other.kicad_pcb"
    _two_net_pcb().save(other)
    assert run_pcb_command(_args(other, "NET_A")) == 0
    legacy = sidecar_path(other).read_bytes()
    out = tmp_path / "out.kicad_pcb"
    out.symlink_to(other)
    beside = out.with_name(out.name + ".reinforcement.json")
    beside.write_bytes(legacy)
    snap = (src.read_bytes(), other.read_bytes(), sidecar_path(src).read_bytes())
    kw = {"replace": {"replace": True}, "clear": {"clear": True}}.get(op, {})
    assert run_pcb_command(_args(src, "NET_B", output=str(out), **kw)) == 1
    assert out.is_symlink()
    assert beside.read_bytes() == legacy
    assert (src.read_bytes(), other.read_bytes(), sidecar_path(src).read_bytes()) == snap


def test_distinct_output_symlink_seeds_from_the_edited_inputs_inventory(tmp_path):
    """An unrelated input wire is preserved; the alias target's wire is not."""
    src = tmp_path / "src.kicad_pcb"
    _two_net_pcb().save(src)
    assert run_pcb_command(_args(src, "NET_B")) == 0
    other = tmp_path / "other.kicad_pcb"
    _two_net_pcb().save(other)
    assert run_pcb_command(_args(other, "NET_A")) == 0
    out = tmp_path / "out.kicad_pcb"
    out.symlink_to(other)
    assert run_pcb_command(_args(src, "NET_A", output=str(out))) == 0
    nets = {w["net"] for w in load_wires(sidecar_path(out))}
    assert nets == {"NET_A", "NET_B"}
    assert len(load_wires(sidecar_path(out))) == 2


def _list_wires_json(board: Path, capsys) -> list[dict]:
    capsys.readouterr()
    assert run_pcb_command(_args(board, net=None, list_wires=True, format="json")) == 0
    return json.loads(capsys.readouterr().out)["wires"]


@pytest.mark.parametrize("failure", ["save", "rules"])
@pytest.mark.parametrize("op", ["add", "replace", "clear", "spaced"])
def test_save_failure_through_output_symlink_preserves_pair_and_control_succeeds(
    tmp_path, monkeypatch, capsys, op, failure
):
    """PR #10253 hold: a failed PCB save must preserve the symlink, PCB, and wire inventory."""
    src = tmp_path / "src.kicad_pcb"
    _two_net_pcb().save(src)
    assert run_pcb_command(_args(src, "NET_B")) == 0
    if op in ("replace", "clear"):
        assert run_pcb_command(_args(src, "NET_A")) == 0
    other = tmp_path / "other.kicad_pcb"
    _two_net_pcb().save(other)
    assert run_pcb_command(_args(other, "NET_A")) == 0
    out = tmp_path / "out.kicad_pcb"
    out.symlink_to(other)

    other_pcb_before = other.read_bytes()
    other_record_before = sidecar_path(other).read_bytes()
    out_target_before = out.readlink()
    out_wires_before = _list_wires_json(out, capsys)
    other_wires_before = _list_wires_json(other, capsys)
    assert out_wires_before == other_wires_before
    assert {w["net"] for w in out_wires_before} == {"NET_A"}

    kw = {
        "add": {},
        "replace": {"replace": True},
        "clear": {"clear": True},
        "spaced": {"anchor_mode": "spaced"},
    }[op]

    def fail_save(self, path=None):
        raise OSError("forced save failure")

    with monkeypatch.context() as mp:
        if failure == "save":
            mp.setattr(PCB, "save", fail_save)
        else:
            src.with_suffix(".kicad_pro").write_text("{}")
            src.with_suffix(".kicad_dru").write_text("(version 1)")
            rules = out.with_suffix(".kicad_dru")
            rules.write_text("existing")
            read = Path.read_bytes

            def read_rules(path):
                if path == rules:
                    raise PermissionError("forced project-rule read failure")
                return read(path)

            mp.setattr(Path, "read_bytes", read_rules)
        capsys.readouterr()
        assert run_pcb_command(_args(src, "NET_A", output=str(out), **kw)) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Error saving PCB" in captured.err

    assert not out.with_suffix(".kicad_pro").exists()
    assert out.is_symlink()
    assert out.readlink() == out_target_before
    assert not out.with_name(out.name + ".reinforcement.json").exists()
    assert other.read_bytes() == other_pcb_before
    assert sidecar_path(other).read_bytes() == other_record_before
    assert _list_wires_json(out, capsys) == out_wires_before
    assert _list_wires_json(other, capsys) == other_wires_before

    # Control: without the save failure, the export detaches cleanly and leaves target intact.
    assert run_pcb_command(_args(src, "NET_A", output=str(out), **kw)) == 0
    assert not out.is_symlink()
    assert other.read_bytes() == other_pcb_before
    assert sidecar_path(other).read_bytes() == other_record_before
    assert _list_wires_json(other, capsys) == other_wires_before
    expected_out_nets = {"NET_A", "NET_B"} if op in ("add", "replace") else {"NET_B"}
    assert {w["net"] for w in _list_wires_json(out, capsys)} == expected_out_nets


@pytest.mark.parametrize("op", ["add", "replace", "clear", "spaced"])
def test_save_failure_on_centered_alias_preserves_symlink_pair(tmp_path, monkeypatch, capsys, op):
    """A failed PCB save through a centered alias must not detach its sidecar symlink."""
    board, alias = _centered_alias_pair(tmp_path)
    alias_record = alias.with_name(alias.name + ".reinforcement.json")
    assert alias.is_symlink()
    assert alias_record.is_symlink()
    board_pcb_before = board.read_bytes()
    board_record_before = sidecar_path(board).read_bytes()
    alias_wires_before = _list_wires_json(alias, capsys)
    board_wires_before = _list_wires_json(board, capsys)
    assert alias_wires_before == board_wires_before

    net = "NET_B" if op in ("add", "spaced") else "NET_A"
    kw = {
        "add": {},
        "replace": {"replace": True},
        "clear": {"clear": True},
        "spaced": {"anchor_mode": "spaced"},
    }[op]

    def fail_save(self, path=None):
        raise OSError("forced save failure")

    monkeypatch.setattr(PCB, "save", fail_save)
    capsys.readouterr()
    assert run_pcb_command(_args(alias, net, **kw)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error saving PCB" in captured.err
    assert alias.is_symlink()
    assert alias_record.is_symlink()
    assert board.read_bytes() == board_pcb_before
    assert sidecar_path(board).read_bytes() == board_record_before
    assert _list_wires_json(alias, capsys) == alias_wires_before
    assert _list_wires_json(board, capsys) == board_wires_before


def test_sidecar_commit_failure_rolls_back_pcb_and_symlink(tmp_path, monkeypatch, capsys):
    """If publishing the sidecar fails after PCB.save, the output symlink is restored."""
    import os

    from kicad_tools.pcb import reinforcement_sidecar as sidecar_mod

    src = tmp_path / "src.kicad_pcb"
    _two_net_pcb().save(src)
    assert run_pcb_command(_args(src, "NET_B")) == 0
    other = tmp_path / "other.kicad_pcb"
    _two_net_pcb().save(other)
    assert run_pcb_command(_args(other, "NET_A")) == 0
    out = tmp_path / "out.kicad_pcb"
    out.symlink_to(other)

    other_pcb_before = other.read_bytes()
    other_record_before = sidecar_path(other).read_bytes()
    out_target_before = out.readlink()
    out_wires_before = _list_wires_json(out, capsys)
    other_wires_before = _list_wires_json(other, capsys)
    expected_sidecar = out.with_name(out.name + ".reinforcement.json")

    real_replace = os.replace

    def fail_sidecar_replace(src_path, dst_path):
        if Path(dst_path) == expected_sidecar:
            raise OSError("forced sidecar replace failure")
        return real_replace(src_path, dst_path)

    monkeypatch.setattr(sidecar_mod.os, "replace", fail_sidecar_replace)
    capsys.readouterr()
    assert run_pcb_command(_args(src, "NET_A", output=str(out))) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error updating reinforcement sidecar" in captured.err
    assert out.is_symlink()
    assert out.readlink() == out_target_before
    assert not expected_sidecar.exists()
    assert other.read_bytes() == other_pcb_before
    assert sidecar_path(other).read_bytes() == other_record_before
    assert _list_wires_json(out, capsys) == out_wires_before
    assert _list_wires_json(other, capsys) == other_wires_before


# --------------------------------------------------------------------------
# Issue #10419: --clear refuses before any write when the destination
# reinforcement sidecar cannot be read
# --------------------------------------------------------------------------

_BAD_VERSION = '{"version": 2, "wires": []}\n'
_MODES = ["inplace", "fresh", "existing"]


def _unreadable_sidecars() -> list:
    """``(file text, reason)`` for three sidecars no version-1 reader accepts."""
    with pytest.raises(ValueError) as info:
        json.loads("{not json\n")
    return [
        pytest.param(_BAD_VERSION, "not a version 1 reinforcement sidecar", id="wrong_version"),
        pytest.param("{not json\n", str(info.value), id="malformed_json"),
        pytest.param('{"version": 1}\n', "missing 'wires' list", id="no_wire_list"),
    ]


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` by relative path, so an absent file shows up too."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _reinforced_source(tmp_path: Path, **reinforce) -> Path:
    """The two-net board reinforced on NET_A (2 anchors, 1 mask opening), in ``tmp_path/src``.

    A ``.kicad_pro`` sits beside it, so a project-sidecar copy to a refused
    ``-o`` output would be noticed.
    """
    src = tmp_path / "src"
    src.mkdir()
    board = _board(src)
    assert run_pcb_command(_args(board, **reinforce)) == 0
    (src / "board.kicad_pro").write_text('{"board": {}}\n')
    return board


def _output_arg(tmp_path: Path, mode: str) -> str | None:
    """The ``-o`` of a clear: in place, a fresh output, or an output holding another board."""
    if mode == "inplace":
        return None
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = out_dir / "out.kicad_pcb"
    if mode == "existing":
        _two_net_pcb(length=80.0).save(out)
    return str(out)


def _destination(board: Path, output: str | None) -> Path:
    return Path(output) if output else board


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("text, reason", _unreadable_sidecars())
def test_clear_refuses_before_writing_on_unreadable_destination(
    tmp_path, capsys, mode, text, reason
):
    board, output = _reinforced_source(tmp_path), _output_arg(tmp_path, mode)
    bad = sidecar_path(_destination(board, output))
    bad.write_text(text)
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    rc = run_pcb_command(_args(board, clear=True, output=output))

    captured = capsys.readouterr()
    assert rc == 1
    assert str(bad) in captured.err and reason in captured.err
    assert _tree_bytes(tmp_path) == before
    assert captured.out == ""


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("kind", ["dangling", "directory"])
@pytest.mark.parametrize(
    "flags", [["--clear"], ["--replace"], ["--replace", "--anchor-mode", "spaced"]]
)
def test_refuses_unreadable_destination_entry(tmp_path, capsys, mode, kind, flags):
    board = _reinforced_source(tmp_path)
    output = _output_arg(tmp_path, mode)
    destination = sidecar_path(_destination(board, output))
    destination.unlink(missing_ok=True)
    target = tmp_path / "missing.json"
    if kind == "dangling":
        destination.symlink_to(target)
    else:
        destination.mkdir()
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    argv = ["pcb", "reinforce", str(board), "--net", "NET_A", *flags]
    if output:
        argv.extend(["-o", output])
    assert main(argv) == 1

    captured = capsys.readouterr()
    assert str(destination) in captured.err and captured.out == ""
    assert _tree_bytes(tmp_path) == before
    if kind == "dangling":
        assert destination.readlink() == target and not target.exists()
    else:
        assert destination.is_dir()


@pytest.mark.parametrize("flag", ["--clear", "--replace"])
def test_readable_destination_symlink_is_preserved(tmp_path, flag):
    board = _reinforced_source(tmp_path)
    destination = sidecar_path(board)
    target = destination.rename(tmp_path / "records.json")
    destination.symlink_to(target)

    assert main(["pcb", "reinforce", str(board), "--net", "NET_A", flag]) == 0
    assert destination.readlink() == target
    assert len(load_wires(target)) == (0 if flag == "--clear" else 1)


def test_clear_refusal_leaves_the_output_directory_as_it_was(tmp_path, capsys):
    """A refused ``-o`` clear copies no project sidecar either."""
    board = _reinforced_source(tmp_path)
    output = _output_arg(tmp_path, "fresh")
    sidecar_path(Path(output)).write_text(_BAD_VERSION)

    capsys.readouterr()
    assert run_pcb_command(_args(board, clear=True, output=output)) == 1

    assert [p.name for p in (tmp_path / "out").iterdir()] == ["out.kicad_pcb.reinforcement.json"]


def test_clear_refusal_prints_no_json_document(tmp_path, capsys):
    board = _reinforced_source(tmp_path)
    bad = sidecar_path(board)
    bad.write_text(_BAD_VERSION)

    capsys.readouterr()
    assert run_pcb_command(_args(board, clear=True, format="json")) == 1

    captured = capsys.readouterr()
    assert captured.out == "" and str(bad) in captured.err


def test_clear_of_a_parallel_copy_refuses_on_unreadable_destination(tmp_path, capsys):
    """The source records identify the copy; the destination still has to be readable."""
    src = tmp_path / "src"
    src.mkdir()
    board = src / "short.kicad_pcb"
    _two_net_pcb(length=12.0).save(board)
    assert run_pcb_command(_args(board, strategy="parallel")) == 0
    output = _output_arg(tmp_path, "fresh")
    bad = sidecar_path(Path(output))
    bad.write_text(_BAD_VERSION)
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    assert run_pcb_command(_args(board, clear=True, output=output)) == 1

    captured = capsys.readouterr()
    assert str(bad) in captured.err and captured.out == ""
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("mode", _MODES)
def test_replace_with_unreadable_destination_writes_nothing(tmp_path, capsys, mode):
    """Regression control: ``--replace`` drops the cleared wires before its board save."""
    board, output = _reinforced_source(tmp_path), _output_arg(tmp_path, mode)
    bad = sidecar_path(_destination(board, output))
    bad.write_text(_BAD_VERSION)
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    assert run_pcb_command(_args(board, replace=True, output=output)) == 1

    captured = capsys.readouterr()
    assert str(bad) in captured.err and captured.out == ""
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("text, reason", _unreadable_sidecars())
@pytest.mark.parametrize("clear", [True, False], ids=["clear", "replace"])
def test_unreadable_source_falls_back_to_size_toward_a_fresh_destination(
    tmp_path, capsys, text, reason, clear
):
    board = _reinforced_source(tmp_path)
    sidecar_path(board).write_text(text)
    output = _output_arg(tmp_path, "fresh")
    source_before = _tree_bytes(tmp_path / "src")

    capsys.readouterr()
    rc = run_pcb_command(_args(board, clear=clear, replace=not clear, output=output, format="json"))

    captured = capsys.readouterr()
    assert rc == 0
    assert f"Warning: ignoring unreadable {sidecar_path(board)}" in captured.err
    assert json.loads(captured.out)["identified_by"] == "size"
    assert len(PCB.load(Path(output)).vias) == (0 if clear else 2)
    written = sidecar_path(Path(output))
    if clear:
        assert not written.exists()
    else:
        assert len(load_wires(written)) == 1
    assert _tree_bytes(tmp_path / "src") == source_before


@pytest.mark.parametrize("clear", [True, False], ids=["clear", "replace"])
@pytest.mark.parametrize("mode", _MODES)
def test_dry_run_reports_an_unreadable_destination_without_failing(tmp_path, capsys, mode, clear):
    board, output = _reinforced_source(tmp_path), _output_arg(tmp_path, mode)
    sidecar_path(_destination(board, output)).write_text(_BAD_VERSION)
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    rc = run_pcb_command(
        _args(board, clear=clear, replace=not clear, output=output, dry_run=True, format="json")
    )

    captured = capsys.readouterr()
    assert rc == 0
    doc = json.loads(captured.out)
    assert doc["dry_run"] is True and doc["anchors_cleared"] == 2
    assert doc["identified_by"] == ("size" if mode == "inplace" else "sidecar")
    assert "Error" not in captured.err
    assert _tree_bytes(tmp_path) == before


def test_clear_without_a_sidecar_still_saves(tmp_path):
    board = _reinforced_source(tmp_path)
    sidecar_path(board).unlink()

    assert run_pcb_command(_args(board, clear=True)) == 0

    assert PCB.load(board).vias == []
    assert not sidecar_path(board).exists()


def test_clear_with_nothing_to_remove_ignores_an_unreadable_sidecar(tmp_path, capsys):
    board = _board(tmp_path)
    sidecar_path(board).write_text(_BAD_VERSION)
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    assert run_pcb_command(_args(board, clear=True)) == 0

    captured = capsys.readouterr()
    assert "Nothing to clear" in captured.out
    assert "Error" not in captured.err
    assert _tree_bytes(tmp_path) == before


# ------------------------------------------- read-only loader (Issue #10398)


def _wire_record(net: str, x: float, **extra) -> dict:
    return {
        "net": net,
        "layer": "F.Cu",
        "kind": "run",
        "path": None,
        "paths": [],
        "leg_start": {"x": x, "y": 0.0},
        "leg_end": {"x": x + 10.0, "y": 0.0},
        "anchors": [{"x": x, "y": 0.0}, {"x": x + 10.0, "y": 0.0}],
        "length_mm": 10.0,
        "straight_line_mm": 10.0,
        "wire_gauge_awg": 16,
        "max_end_shift_mm": None,
        "bare_diameter_mm": round(bare_copper_diameter_mm(16), 4),
        "mask_opening": None,
        "status": "full",
        "reason": "reinforced",
        **extra,
    }


def _parallel_record(net: str, x: float) -> dict:
    return _wire_record(
        net,
        x,
        strategy="parallel",
        copy_status="placed",
        copy_layer="B.Cu",
        polyline=[{"x": x, "y": 0.0}, {"x": x + 10.0, "y": 0.0}],
        copy_segments=[
            {"start": {"x": x, "y": 0.0}, "end": {"x": x + 10.0, "y": 0.0}, "width_mm": 2.0}
        ],
        stitch_vias=[{"x": x + 5.0, "y": 0.0}],
        stitch_via_size_mm=0.8,
        stitch_via_drill_mm=0.4,
    )


def _sidecar_with(tmp_path: Path, entries: list) -> tuple[Path, Path]:
    board = tmp_path / "board.kicad_pcb"
    board.touch()
    sidecar = sidecar_path(board)
    sidecar.write_text(
        json.dumps({"version": 1, "wires": entries}, indent=2) + "\n", encoding="utf-8"
    )
    return board, sidecar


def test_read_only_loader_returns_the_objects_in_file_order(tmp_path):
    """Objects survive whole and in order around entries that are not objects,
    and reading leaves the file's bytes alone."""
    legacy = _wire_record("NET_A", 0.0)
    parallel = _parallel_record("NET_B", 20.0)
    board, sidecar = _sidecar_with(
        tmp_path, [None, legacy, "not a record", 7, [legacy], parallel, False]
    )
    before = sidecar.read_bytes()
    assert load_wires_if_present(board) == [legacy, parallel]
    assert sidecar.read_bytes() == before


def test_read_only_loader_keeps_legacy_wire_and_parallel_copy_meaning(tmp_path):
    legacy = _wire_record("NET_A", 0.0)
    parallel = _parallel_record("NET_B", 20.0)
    board, _ = _sidecar_with(tmp_path, [parallel, None, legacy])
    loaded = load_wires_if_present(board)
    assert loaded == [parallel, legacy]
    assert [is_parallel(w) for w in loaded] == [True, False]
    assert [wire_key(w) for w in loaded] == [wire_key(parallel), wire_key(legacy)]


@pytest.mark.parametrize(
    "entries",
    [[], [None], ["not a record"], [1, 2.5, True, [1, {"net": "NET_A"}], None]],
)
def test_read_only_loader_with_no_object_is_empty(tmp_path, entries):
    board, sidecar = _sidecar_with(tmp_path, entries)
    before = sidecar.read_bytes()
    assert load_wires_if_present(board) == []
    assert sidecar.read_bytes() == before


def test_read_only_loader_absent_sidecar_is_empty(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    board.touch()
    assert load_wires_if_present(board) == []


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        json.dumps([_wire_record("NET_A", 0.0)]),
        json.dumps({"version": 2, "wires": [_wire_record("NET_A", 0.0)]}),
        json.dumps({"version": 1, "wires": {"NET_A": _wire_record("NET_A", 0.0)}}),
        json.dumps({"version": 1}),
    ],
)
def test_read_only_loader_unreadable_envelope_is_empty(tmp_path, text):
    board = tmp_path / "board.kicad_pcb"
    board.touch()
    sidecar = sidecar_path(board)
    sidecar.write_text(text, encoding="utf-8")
    before = sidecar.read_bytes()
    assert load_wires_if_present(board) == []
    assert sidecar.read_bytes() == before


def test_low_level_loader_still_reads_every_entry(tmp_path):
    """``load_wires`` feeds ``write_sidecar`` and ``drop_wires``: dropping an
    entry there would silently delete it from the owner's file."""
    legacy = _wire_record("NET_A", 0.0)
    board, sidecar = _sidecar_with(tmp_path, [None, legacy, "not a record"])
    assert load_wires(sidecar) == [None, legacy, "not a record"]


def test_center_between_reinforcement_board_and_record_writes(tmp_path, monkeypatch):
    import subprocess
    import sys

    from kicad_tools.pcb.center_sheet import center_on_sheet

    board = _board(tmp_path)
    assert run_pcb_command(_args(board, "NET_B")) == 0
    save = PCB.save
    attempts = []

    def save_then_center(pcb, path):
        save(pcb, path)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "pcb",
                "center-on-sheet",
                str(board),
                "--paper",
                "keep",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        attempts.append((path, result.returncode, result.stderr))

    monkeypatch.setattr(PCB, "save", save_then_center)
    assert run_pcb_command(_args(board, "NET_A")) == 0
    [(path, code, error)] = attempts
    assert path == board and code == 1
    assert "publishing the input" in error
    assert center_on_sheet(board, paper="keep").changed
    pcb = PCB.load(board)
    ox, oy = pcb.board_origin or (0, 0)
    vias = {(round(v.position[0] + ox, 4), round(v.position[1] + oy, 4)) for v in pcb.vias}
    anchors = {(a["x"], a["y"]) for w in load_wires(sidecar_path(board)) for a in w["anchors"]}
    assert len(anchors) == 4
    assert anchors == vias


# --------------------- malformed coordinate data in a record (Issue #10559)

_ABSENT = object()
_POINT = "an object with numeric 'x' and 'y'"
_SEGMENT = "an object with 'start' and 'end' points"

_BAD_POINTS = [
    pytest.param(
        ("anchors", "not-a-point"),
        ".anchors is a string, expected an array of points",
        id="anchors-string",
    ),
    pytest.param(
        ("anchors", {"x": 20.0, "y": 70.0}),
        ".anchors is an object, expected an array of points",
        id="anchors-object",
    ),
    pytest.param(
        ("anchors", None), ".anchors is null, expected an array of points", id="anchors-null"
    ),
    pytest.param(
        ("anchors", _ABSENT), " has no 'anchors', expected an array of points", id="no-anchors"
    ),
    pytest.param(
        ("anchors.0", "not-a-point"),
        f".anchors[0] is a string, expected {_POINT}",
        id="anchor-string",
    ),
    pytest.param(
        ("anchors.1", [20.0, 70.0]),
        f".anchors[1] is an array, expected {_POINT}",
        id="anchor-pair-array",
    ),
    pytest.param(
        ("anchors.1", None),
        f".anchors[1] is null, expected {_POINT}",
        id="anchor-null",
    ),
    pytest.param(
        ("anchors.0.x", "20.0"), ".anchors[0].x is a string, expected a number", id="x-string"
    ),
    pytest.param(
        ("anchors.0.x", True), ".anchors[0].x is a boolean, expected a number", id="x-boolean"
    ),
    pytest.param(("anchors.0.y", _ABSENT), ".anchors[0] has no 'y', expected a number", id="no-y"),
    pytest.param(
        ("anchors.0.y", {"x": 1.0}),
        ".anchors[0].y is an object, expected a number",
        id="y-object",
    ),
    pytest.param(
        ("leg_start", [20.0, 70.0]),
        f".leg_start is an array, expected {_POINT}",
        id="leg-start-array",
    ),
    pytest.param(
        ("leg_start.x", "oops"), ".leg_start.x is a string, expected a number", id="leg-x-string"
    ),
    pytest.param(
        ("leg_start.y", _ABSENT), ".leg_start has no 'y', expected a number", id="leg-no-y"
    ),
    pytest.param(
        ("leg_end", _ABSENT),
        f" has no 'leg_end', expected {_POINT}",
        id="no-leg-end",
    ),
    pytest.param(
        ("leg_end.y", False), ".leg_end.y is a boolean, expected a number", id="leg-y-boolean"
    ),
]

_BAD_COPY_POINTS = [
    pytest.param(
        ("polyline", "oops"),
        ".polyline is a string, expected an array of points",
        id="polyline-string",
    ),
    pytest.param(
        ("polyline", {"x": 1.0}),
        ".polyline is an object, expected an array of points",
        id="polyline-object",
    ),
    pytest.param(
        ("polyline.1", {"x": 1.0}), ".polyline[1] has no 'y', expected a number", id="polyline-no-y"
    ),
    pytest.param(
        ("polyline.0.x", [1.0]),
        ".polyline[0].x is an array, expected a number",
        id="polyline-x-array",
    ),
    pytest.param(
        ("stitch_vias", 3),
        ".stitch_vias is a number, expected an array of points",
        id="stitches-number",
    ),
    pytest.param(
        ("stitch_vias.0", None),
        f".stitch_vias[0] is null, expected {_POINT}",
        id="stitch-null",
    ),
    pytest.param(
        ("stitch_vias.0.x", "5"),
        ".stitch_vias[0].x is a string, expected a number",
        id="stitch-x-string",
    ),
    pytest.param(
        ("copy_segments", "x"),
        ".copy_segments is a string, expected an array of segments with 'start' and 'end' points",
        id="segments-string",
    ),
    pytest.param(
        ("copy_segments.0", 7),
        f".copy_segments[0] is a number, expected {_SEGMENT}",
        id="segment-number",
    ),
    pytest.param(
        ("copy_segments.0", {"start": {"x": 1.0, "y": 2.0}}),
        f".copy_segments[0] has no 'end', expected {_POINT}",
        id="segment-no-end",
    ),
    pytest.param(
        ("copy_segments.0.start", "nope"),
        f".copy_segments[0].start is a string, expected {_POINT}",
        id="segment-start-string",
    ),
    pytest.param(
        ("copy_segments.0.end", "nope"),
        f".copy_segments[0].end is a string, expected {_POINT}",
        id="segment-end-string",
    ),
    pytest.param(
        ("copy_segments.0.start.x", _ABSENT),
        ".copy_segments[0].start has no 'x', expected a number",
        id="segment-start-no-x",
    ),
    pytest.param(
        ("copy_segments.0.end.x", False),
        ".copy_segments[0].end.x is a boolean, expected a number",
        id="segment-end-x-boolean",
    ),
]

_BAD_COORDINATES = _BAD_POINTS + _BAD_COPY_POINTS


def _put(record: dict, path: str, value) -> None:
    """Damage the value of ``record`` at dotted ``path`` (``copy_segments.0.end.y``);
    ``_ABSENT`` removes it."""
    keys = [int(key) if key.isdigit() else key for key in path.split(".")]
    node = record
    for key in keys[:-1]:
        node = node[key]
    if value is _ABSENT:
        del node[keys[-1]]
    else:
        node[keys[-1]] = value


def _damaged(tmp_path: Path, net: str, path: str, value, *others: str):
    """``(board, sidecar, pristine records)``: the board after reinforcing ``net`` then
    ``others``, with ``net``'s record damaged at ``path``. File order is kept, so
    ``NET_B`` is ``wires[1]`` beside ``NET_A``."""
    p, records = _reinforced(tmp_path, net, *others)
    pristine = json.loads(json.dumps(records))
    _put(next(w for w in records if w["net"] == net), path, value)
    return p, _rewrite_sidecar(p, records), pristine


@pytest.mark.parametrize(("damage", "fragment"), _BAD_COORDINATES)
def test_record_guard_names_the_file_index_and_the_offending_field(tmp_path, damage, fragment):
    """The diagnostic names the sidecar, the entry's position in the file -- 1,
    where sorting would move NET_A to 0 -- and the exact field or element."""
    bad = _parallel_record("NET_A", 0.0)
    _put(bad, *damage)
    entries = [_wire_record("NET_B", 20.0), bad]
    _board, sidecar = _sidecar_with(tmp_path, entries)
    before = sidecar.read_bytes()
    with pytest.raises(ValueError) as refused:
        load_wire_records(sidecar)
    assert str(refused.value) == f"{sidecar}: wires[1]{fragment}"
    assert load_wires(sidecar) == entries and sidecar.read_bytes() == before


def test_record_guard_reports_the_first_offender_in_file_order(tmp_path):
    """An entry that is not an object keeps its #10401 message and is reported before
    any malformed point, wherever it sits; without one, the first malformed point in
    file order is named."""
    bad = _parallel_record("NET_A", 0.0)
    _put(bad, "anchors.0", "not-a-point")
    later = _parallel_record("NET_B", 40.0)
    _put(later, "leg_end.y", True)
    board, sidecar = _sidecar_with(tmp_path, [bad, later])
    with pytest.raises(ValueError, match=r"wires\[0\]\.anchors\[0\] is a string"):
        load_wire_records(sidecar)
    _rewrite_sidecar(board, [_wire_record("NET_C", 60.0), later, bad])
    with pytest.raises(ValueError, match=r"wires\[1\]\.leg_end\.y is a boolean"):
        load_wire_records(sidecar)
    for entries in ([bad, later, None], [None, bad, later]):
        _rewrite_sidecar(board, entries)
        with pytest.raises(ValueError, match=r"wires\[(0|2)\] is null, not a JSON object"):
            load_wire_records(sidecar)


def test_record_guard_accepts_the_shapes_writers_use(tmp_path, capsys):
    """Integers and floats, empty point lists, and records without the optional
    parallel-copy fields: a legacy wire, and a parallel copy with empty lists."""
    entries = [
        _wire_record("NET_B", 0.0),
        _wire_record(
            "NET_C",
            0.0,
            leg_start={"x": 20, "y": 70},
            leg_end={"x": 120, "y": 70},
            anchors=[{"x": 20, "y": 70}, {"x": 120, "y": 70}],
        ),
        _wire_record("NET_D", 0.0, anchors=[]),
        _parallel_record("NET_E", 0.0),
        {
            **_parallel_record("NET_F", 0.0),
            "polyline": [],
            "copy_segments": [],
            "stitch_vias": [],
        },
        _wire_record("NET_A", 0.0),
    ]
    p = _board(tmp_path)
    sidecar = _rewrite_sidecar(p, entries)
    before = sidecar.read_bytes()
    assert load_wire_records(sidecar) == entries
    assert check_wire_records(sidecar, entries) is None
    assert check_wire_records(sidecar, []) is None
    capsys.readouterr()
    assert run_pcb_command(_args(p, net=None, list_wires=True)) == 0
    out = capsys.readouterr().out
    assert f"{len(entries)} wire(s):" in out and all(w["net"] in out for w in entries)
    assert sidecar.read_bytes() == before


def test_object_check_alone_passes_malformed_coordinates(tmp_path):
    """The shallow check stays available to a reader that only filters or dumps."""
    from kicad_tools.pcb.reinforcement_sidecar import check_wire_objects

    bad = _parallel_record("NET_A", 0.0)
    _put(bad, "anchors.0", "not-a-point")
    _put(bad, "copy_segments.0.end", 7)
    _board, sidecar = _sidecar_with(tmp_path, [_wire_record("NET_B", 20.0), bad])
    assert check_wire_objects(sidecar, [_wire_record("NET_B", 20.0), bad]) is None
    with pytest.raises(ValueError, match=r"wires\[1\] is null, not a JSON object"):
        check_wire_objects(sidecar, [_wire_record("NET_B", 20.0), None])


def test_lenient_loaders_pass_a_malformed_point_through(tmp_path):
    """``load_wires`` and the read-only loader validate no field, so nothing is dropped."""
    bad = _parallel_record("NET_A", 0.0)
    _put(bad, "anchors.0", "not-a-point")
    _put(bad, "leg_start.y", _ABSENT)
    legacy = _wire_record("NET_B", 20.0)
    board, sidecar = _sidecar_with(tmp_path, [legacy, bad, None])
    before = sidecar.read_bytes()
    assert load_wires(sidecar) == [legacy, bad, None]
    assert load_wires_if_present(board) == [legacy, bad]
    assert sidecar.read_bytes() == before


def test_writers_refuse_a_malformed_point_and_leave_the_file_alone(tmp_path):
    """``write_sidecar`` and ``drop_wires`` used to raise TypeError from the anchor match."""
    p, sidecar, pristine = _damaged(tmp_path, "NET_B", "anchors.0", "not-a-point", "NET_A")
    net_b = next(w for w in pristine if w["net"] == "NET_B")
    before = sidecar.read_bytes()
    with pytest.raises(ValueError, match=r"wires\[1\]\.anchors\[0\] is a string"):
        load_wire_records(sidecar)
    with pytest.raises(ValueError, match=r"wires\[1\]\.anchors\[0\]"):
        write_sidecar(p, [_wire_record("NET_C", 40.0)])
    with pytest.raises(ValueError, match=r"wires\[1\]\.anchors\[0\]"):
        drop_wires(sidecar, "NET_B", [(a["x"], a["y"]) for a in net_b["anchors"]])
    assert sidecar.read_bytes() == before


@pytest.mark.parametrize("net", ["NET_A", "NET_B"])
@pytest.mark.parametrize(("damage", "fragment"), _BAD_POINTS)
def test_clear_refuses_malformed_coordinate_data_before_writing(
    tmp_path, capsys, net, damage, fragment
):
    """The list names the anchors to remove and every writer keys on the leg ends, so
    the whole list is refused, whichever net is named, where a malformed anchor or
    leg end used to raise TypeError or KeyError."""
    p, sidecar, _pristine = _damaged(tmp_path, "NET_B", *damage, "NET_A")
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    rc = run_pcb_command(_args(p, net, clear=True))

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == f"Error: {sidecar}: wires[1]{fragment}\n"
    assert captured.out == ""
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize(
    ("mode", "report"),
    [
        ({"adopt": True}, "Error reading {sidecar}: {diagnostic}"),
        ({"clear": True}, "Error: {sidecar}: {diagnostic}"),
        ({"replace": True}, "Error: {sidecar}: {diagnostic}"),
    ],
    ids=["adopt", "clear", "replace"],
)
def test_adopt_clear_and_replace_refuse_a_malformed_anchor(tmp_path, capsys, mode, report):
    p, sidecar, _pristine = _damaged(tmp_path, "NET_B", "anchors.0", "not-a-point", "NET_A")
    expected = report.format(
        sidecar=sidecar, diagnostic=f"wires[1].anchors[0] is a string, expected {_POINT}"
    )
    before = _tree_bytes(tmp_path)
    for fmt in ("text", "json"):
        capsys.readouterr()
        assert run_pcb_command(_args(p, "NET_B", format=fmt, **mode)) == 1
        captured = capsys.readouterr()
        assert captured.err == f"{expected}\n" and captured.out == ""
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("mode", [{"clear": True}, {"replace": True}], ids=["clear", "replace"])
def test_dry_run_clear_refuses_a_malformed_anchor_like_a_non_object(tmp_path, capsys, mode):
    """Under --dry-run the list is still read to name the anchors to report."""
    p, sidecar, _pristine = _damaged(tmp_path, "NET_B", "anchors.0", "not-a-point", "NET_A")
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    rc = run_pcb_command(_args(p, "NET_B", dry_run=True, **mode))

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == f"Error: {sidecar}: wires[1].anchors[0] is a string, expected {_POINT}\n"
    assert captured.out == "" and _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_add_refuses_a_malformed_leg_end_before_saving_the_board(tmp_path, capsys, fmt):
    """A plain pass merges its record into the list and sorts it, which keys on the
    leg ends; it used to raise TypeError or KeyError there."""
    p, sidecar, _pristine = _damaged(tmp_path, "NET_B", "leg_start.x", "oops", "NET_A")
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    rc = run_pcb_command(_args(p, "NET_A", format=fmt))

    captured = capsys.readouterr()
    assert rc == 1
    assert (
        captured.err
        == f"Error updating reinforcement sidecar: {sidecar}: wires[1].leg_start.x is a string, expected a number\n"
    )
    assert captured.out == "" and _tree_bytes(tmp_path) == before


@pytest.mark.parametrize(("damage", "fragment"), _BAD_COPY_POINTS)
def test_clear_and_a_parallel_rerun_refuse_a_malformed_copy_record(
    tmp_path, capsys, damage, fragment
):
    """A copy's segments and stitching vias are how --clear and a parallel re-run tell
    the tool's own copper from copper that predates it."""
    p = _short_board(tmp_path, 12.0)
    assert run_pcb_command(_args(p, strategy="parallel")) == 0
    (record,) = load_wires(sidecar_path(p))
    _put(record, *damage)
    sidecar = _rewrite_sidecar(p, [record])
    before = _tree_bytes(tmp_path)
    for mode in ({"clear": True}, {"strategy": "parallel"}):
        capsys.readouterr()
        assert run_pcb_command(_args(p, **mode)) == 1
        captured = capsys.readouterr()
        assert captured.err == f"Error: {sidecar}: wires[0]{fragment}\n"
        assert captured.out == ""
    assert _tree_bytes(tmp_path) == before


def test_list_wires_text_refuses_a_malformed_anchor_and_json_prints_the_entries(tmp_path, capsys):
    """The text listing formats the points; the JSON forms only filter or dump the entries."""
    p, sidecar, _pristine = _damaged(tmp_path, "NET_B", "anchors.0", "not-a-point", "NET_A")
    entries = load_wires(sidecar)
    before = _tree_bytes(tmp_path)
    for net in ("NET_B", None):
        capsys.readouterr()
        assert run_pcb_command(_args(p, net, list_wires=True)) == 1
        captured = capsys.readouterr()
        assert (
            captured.err
            == f"Error reading {sidecar}: wires[1].anchors[0] is a string, expected {_POINT}\n"
        )
        assert captured.out == ""
    capsys.readouterr()
    assert run_pcb_command(_args(p, "NET_B", list_wires=True, format="json")) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"sidecar": str(sidecar), "wires": [entries[1]]}
    assert captured.err == ""
    capsys.readouterr()
    assert run_pcb_command(_args(p, net=None, list_wires=True, format="json")) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"sidecar": str(sidecar), "wires": entries}
    assert captured.err == ""
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("existing_output", [False, True])
@pytest.mark.parametrize("fmt", ["text", "json"])
def test_clear_to_an_output_with_a_malformed_destination_sidecar_writes_nothing(
    tmp_path, capsys, fmt, existing_output
):
    """The destination list names the anchors to drop; its malformed anchor used to
    raise after the output board had been saved."""
    p, (net_a,) = _reinforced(tmp_path, "NET_A")
    out = tmp_path / "out.kicad_pcb"
    if existing_output:
        _two_net_pcb(length=80.0).save(out)
    bad_record = json.loads(json.dumps(net_a))
    bad_record["anchors"][0] = "not-a-point"
    bad = _rewrite_sidecar(out, [bad_record])
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    rc = run_pcb_command(_args(p, "NET_A", clear=True, output=str(out), format=fmt))

    captured = capsys.readouterr()
    assert rc == 1
    assert (
        captured.err
        == f"Error reading {bad}: wires[0].anchors[0] is a string, expected {_POINT} -- nothing was written\n"
    )
    assert captured.out == ""
    assert out.exists() is existing_output
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize(
    ("mode", "report"),
    [
        ({}, "Error updating reinforcement sidecar: {sidecar}: {diagnostic}"),
        ({"replace": True}, "Error reading {sidecar}: {diagnostic} -- nothing was written"),
    ],
    ids=["add", "replace"],
)
def test_add_or_replace_to_an_output_with_a_malformed_destination_sidecar_writes_nothing(
    tmp_path, capsys, mode, report
):
    """Add merges its record into the destination list and --replace drops the cleared
    wires from it first; either way the list is refused before the board is saved."""
    p, (net_a,) = _reinforced(tmp_path, "NET_A")
    out = tmp_path / "out" / "out.kicad_pcb"
    out.parent.mkdir()
    bad_record = json.loads(json.dumps(net_a))
    bad_record["leg_end"] = [120.0, 30.0]
    bad = _rewrite_sidecar(out, [bad_record])
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    rc = run_pcb_command(_args(p, "NET_A", output=str(out), **mode))

    captured = capsys.readouterr()
    assert rc == 1
    assert (
        captured.err
        == f"{report.format(sidecar=bad, diagnostic=f'wires[0].leg_end is an array, expected {_POINT}')}\n"
    )
    assert captured.out == "" and not out.exists()
    assert _tree_bytes(tmp_path) == before


def test_clear_to_an_output_refuses_a_malformed_source_sidecar(tmp_path, capsys):
    p, sidecar, _pristine = _damaged(tmp_path, "NET_B", "leg_start.y", _ABSENT, "NET_A")
    out = tmp_path / "out" / "out.kicad_pcb"
    out.parent.mkdir()
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    rc = run_pcb_command(_args(p, "NET_B", clear=True, output=str(out)))

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == f"Error: {sidecar}: wires[1].leg_start has no 'y', expected a number\n"
    assert captured.out == "" and not out.exists()
    assert _tree_bytes(tmp_path) == before


def test_cli_reports_a_malformed_point_and_exits_1(tmp_path, capsys):
    """Through the entry point, the refusal is the handler's diagnostic, not a traceback."""
    p, sidecar, _pristine = _damaged(tmp_path, "NET_B", "anchors.0", "not-a-point", "NET_A")
    before = _tree_bytes(tmp_path)

    capsys.readouterr()
    rc = main(["pcb", "reinforce", str(p), "--net", "NET_B", "--clear"])

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err == f"Error: {sidecar}: wires[1].anchors[0] is a string, expected {_POINT}\n"
    assert _tree_bytes(tmp_path) == before
