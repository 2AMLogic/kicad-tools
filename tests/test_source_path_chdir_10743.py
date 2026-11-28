"""A board loaded by relative path keeps selecting its own project after a ``chdir``.

``PCB.source_path`` (Issue #10683) made ``DRCChecker`` read the ``.kicad_pro``
of the directory a board was *loaded* from instead of the process's current
working directory.  The other public APIs that resolve a project, a schematic
or a decisions ledger beside the caller-visible (relative) ``pcb.path`` still
picked up whatever sat beside that name in the new directory (Issue #10743).

Each test writes two disposable projects, A and B, that hold the same file
name and identical board bytes but different sidecars, loads A by relative
path, changes to B and keeps the same ``PCB`` object.  The result must equal
an absolute-load control, and a board loaded relatively *in B* shows that the
fixture discriminates (the non-vacuous control).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.schema.pcb import PCB

from .test_bypass_distance_rule import _HEADER as _BYPASS_HEAD
from .test_bypass_distance_rule import _cap, _ic
from .test_decoupling_association import make_sheet
from .test_pcb_layout_bridge import (
    N_TRACES,
    N_VIAS,
    NET_MAP,
    PAD_NETS,
    REFS,
    _build_board,
    _layout,
)
from .test_pour_stub_10137 import fixture_board as _stub_board
from .test_pour_stub_10137 import require_native_refill
from .test_relocate_pad_edge_vias_7296 import (
    _EDGE_VIA,
    _FOREIGN_TRACK,
    _RULES,
    _power_project,
)
from .test_relocate_pad_edge_vias_7296 import _HEAD as _VIA_HEAD

BOARD = Path("board.kicad_pcb")
SCHEMATIC = BOARD.with_suffix(".kicad_sch")
LEDGER = BOARD.with_suffix(".decisions.json")

ISOLATE_U1 = """(version 1)
        (rule "Isolate U1" (condition "A.Type == 'Pad' && A.memberOfFootprint('U1')")
         (constraint zone_connection none))
"""


def _paired(tmp_path: Path, monkeypatch, name: str, board: bytes) -> tuple[Path, Path, Path]:
    """Write identical ``board`` bytes as ``name`` into projects A and B; cwd becomes A.

    Returns ``(a, b, name)`` with ``name`` the relative path of A's board.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    for project in (a, b):
        project.mkdir()
        (project / name).write_bytes(board)
    monkeypatch.chdir(a)
    return a, b, Path(name)


def _default_class_project(board: Path, clearance: float) -> None:
    board.with_suffix(".kicad_pro").write_text(
        json.dumps({"net_settings": {"classes": [{"name": "Default", "clearance": clearance}]}})
    )


def _snapshot(project: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in project.iterdir()}


# ---------------------------------------------------------------------------
# KiCadPCBExporter.apply_to
# ---------------------------------------------------------------------------


def test_layout_exporter_keeps_the_loaded_project_after_chdir(tmp_path, monkeypatch):
    """``KiCadPCBExporter.apply_to`` grades copper against the loaded board's rules."""
    from kicad_tools.pcb import KiCadPCBExporter, LayoutApplyError
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules

    seed = _build_board(tmp_path / "seed.kicad_pcb", REFS, PAD_NETS)
    a, b, name = _paired(tmp_path, monkeypatch, BOARD.name, seed.read_bytes())
    _default_class_project(a / name, 0.3)
    _default_class_project(b / name, 0.2)
    origin = PCB.load(str(a / name)).board_origin

    def exporter(**kwargs) -> KiCadPCBExporter:
        return KiCadPCBExporter(_layout(origin), net_map=NET_MAP, **kwargs)

    # The LDO block's GND run clears C1.1 by 0.25 mm: legal at B's 0.2 mm, not A's 0.3 mm.
    pcb = PCB.load(name)
    monkeypatch.chdir(b)
    with pytest.raises(LayoutApplyError) as excinfo:
        exporter().apply_to(pcb)
    assert any("0.250 mm from pad C1.1" in p for p in excinfo.value.problems), (
        excinfo.value.problems
    )
    assert pcb.path == name

    with pytest.raises(LayoutApplyError) as control:
        exporter().apply_to(PCB.load(str(a / name)))
    assert control.value.problems == excinfo.value.problems

    # Non-vacuous: a board loaded in B takes B's project and applies cleanly.
    report = exporter().apply_to(PCB.load(name))
    assert report.skipped == []
    assert (report.segments_added, report.vias_added) == (N_TRACES, N_VIAS)

    # Explicit rules still win over whatever project sits beside the board.
    lenient = BoardClearanceRules(class_clearance={"Default": 0.2})
    report = exporter(clearance_rules=lenient).apply_to(pcb)
    assert report.skipped == []
    assert (report.segments_added, report.vias_added) == (N_TRACES, N_VIAS)


def test_relative_save_as_selects_the_new_project(tmp_path, monkeypatch):
    """A successful Save As binds later implicit lookups to the new project."""
    from kicad_tools.pcb import KiCadPCBExporter

    seed = _build_board(tmp_path / "seed.kicad_pcb", REFS, PAD_NETS)
    a, b, _ = _paired(tmp_path, monkeypatch, BOARD.name, seed.read_bytes())
    _default_class_project(a / BOARD, 0.3)
    copied = Path("copy.kicad_pcb")
    _default_class_project(b / copied, 0.2)
    origin = PCB.load(str(a / BOARD)).board_origin

    pcb = PCB.load(BOARD)
    monkeypatch.chdir(b)
    pcb.save(copied)
    # A stricter stranger now sits beside the relative name in the new directory.
    monkeypatch.chdir(tmp_path)
    _default_class_project(tmp_path / copied, 0.3)
    assert pcb.path == copied

    report = KiCadPCBExporter(_layout(origin), net_map=NET_MAP).apply_to(pcb)
    assert report.skipped == []
    assert (report.segments_added, report.vias_added) == (N_TRACES, N_VIAS)


# ---------------------------------------------------------------------------
# relocate_pad_edge_vias
# ---------------------------------------------------------------------------


def test_via_relocation_keeps_the_loaded_project_after_chdir(tmp_path, monkeypatch):
    """``relocate_pad_edge_vias`` honours the loaded board's netclass clearances."""
    from kicad_tools.cli.relocate_in_pad_vias import relocate_pad_edge_vias
    from kicad_tools.validate.rules.netclass_clearance_copper import (
        check_netclass_clearance_copper,
    )

    board = (_VIA_HEAD.format(mask=0.0) + _FOREIGN_TRACK + _EDGE_VIA).encode()
    a, b, name = _paired(tmp_path, monkeypatch, BOARD.name, board)
    _power_project(a / name)  # SIG1 in a 0.2 mm ``Power`` class
    # B has no project: the fab floor alone accepts the 0.16 mm spot.

    pcb = PCB.load(name)
    monkeypatch.chdir(b)
    result = relocate_pad_edge_vias(pcb, _RULES)
    assert [m.kind for m in result.moved] == ["pad-edge"] and not result.unresolvable
    assert pcb.path == name

    control = PCB.load(str(a / name))
    relocate_pad_edge_vias(control, _RULES)
    assert pcb.vias[0].position == control.vias[0].position

    # A's own 0.200 mm ``Power`` class accepts the result ...
    checked = tmp_path / "relocated.kicad_pcb"
    project = (a / name).with_suffix(".kicad_pro").read_bytes()
    checked.with_suffix(".kicad_pro").write_bytes(project)
    pcb.save(checked)
    assert check_netclass_clearance_copper(checked, PCB.load(checked), _RULES).violations == []

    # ... and rejects the 0.16 mm spot a board loaded in B is moved to.
    wrong = PCB.load(name)
    relocate_pad_edge_vias(wrong, _RULES)
    assert wrong.vias[0].position != control.vias[0].position
    wrong.save(checked)
    violations = check_netclass_clearance_copper(checked, PCB.load(checked), _RULES).violations
    assert [v.rule_id for v in violations] == ["netclass_clearance_copper"]


# ---------------------------------------------------------------------------
# BypassCapDistanceCheck.check
# ---------------------------------------------------------------------------


def _bypass_board(c1: float, c2: float) -> bytes:
    return (
        _BYPASS_HEAD + _ic("U1", 20) + _ic("U2", 60) + _cap("C1", c1) + _cap("C2", c2) + ")\n"
    ).encode()


def _bypass_sheet(path: Path, *, beside_other_ic: bool) -> None:
    """Draw each cap beside its own IC, or beside the other one."""
    near_u1, near_u2 = ("C2", "C1") if beside_other_ic else ("C1", "C2")
    make_sheet(
        path,
        [
            ("U1", 50.8, 50.8, "+3V3", None),
            ("U2", 101.6, 50.8, "+3V3", None),
            (near_u1, 55.88, 50.8, "+3V3", None),
            (near_u2, 106.68, 50.8, "+3V3", None),
        ],
        [(50.8, 50.8, 55.88, 50.8), (101.6, 50.8, 106.68, 50.8)],
    )


def _pairs(mistakes: list) -> list[tuple[str, str]]:
    return sorted(tuple(m.components) for m in mistakes)


def test_bypass_check_keeps_the_loaded_schematic_after_chdir(tmp_path, monkeypatch):
    """``BypassCapDistanceCheck.check`` pairs caps through the loaded board's schematic."""
    from kicad_tools.explain.checks.bypass import BypassCapDistanceCheck

    # The caps are swapped on the board: far from the IC each schematic draws them beside.
    a, b, name = _paired(tmp_path, monkeypatch, BOARD.name, _bypass_board(c1=61.5, c2=21.5))
    _bypass_sheet(a / SCHEMATIC, beside_other_ic=False)
    _bypass_sheet(b / SCHEMATIC, beside_other_ic=True)

    pcb = PCB.load(name)
    monkeypatch.chdir(b)
    mistakes = BypassCapDistanceCheck().check(pcb)
    assert sorted((tuple(m.components), m.measurements["distance_mm"]) for m in mistakes) == [
        (("C1", "U1"), 41.0),
        (("C2", "U2"), 39.0),
    ]
    assert pcb.path == name

    control = BypassCapDistanceCheck().check(PCB.load(str(a / name)))
    assert [(tuple(m.components), m.measurements["distance_mm"]) for m in control] == [
        (tuple(m.components), m.measurements["distance_mm"]) for m in mistakes
    ]

    # Non-vacuous: loaded in B, the same board pairs each cap with the near IC.
    assert BypassCapDistanceCheck().check(PCB.load(name)) == []

    # An explicit schematic stays authoritative whatever the directory.
    assert BypassCapDistanceCheck(b / SCHEMATIC).check(pcb) == []
    explicit = BypassCapDistanceCheck(a / SCHEMATIC).check(PCB.load(name))
    assert _pairs(explicit) == [("C1", "U1"), ("C2", "U2")]


# ---------------------------------------------------------------------------
# prove_pour_stubs / select_pour_stubs
# ---------------------------------------------------------------------------


def test_pour_stub_proof_keeps_the_loaded_rule_files_after_chdir(tmp_path, monkeypatch):
    """The native pour trial reads the loaded board's own rule files."""
    require_native_refill()
    from kicad_tools.validate.pour_stub import prove_pour_stubs, select_pour_stubs

    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    seed = _stub_board(seed_dir, "connected")
    a, b, name = _paired(tmp_path, monkeypatch, seed.name, seed.read_bytes())
    dru = name.with_suffix(".kicad_dru")
    for project in (a, b):
        (project / name).with_suffix(".kicad_pro").write_bytes(
            seed.with_suffix(".kicad_pro").read_bytes()
        )
    (b / dru).write_text("(version 1)\n")
    (a / dru).write_text(ISOLATE_U1)  # A isolates U1 from the zone: its branch must stay
    before = {project.name: _snapshot(project) for project in (a, b)}

    pcb = PCB.load(name)
    monkeypatch.chdir(b)
    proof = prove_pour_stubs(pcb)
    assert proof is not None and proof.verified == []
    assert pcb.path == name
    control = prove_pour_stubs(PCB.load(str(a / name)))
    assert (control.evaluated, control.verified) == (proof.evaluated, proof.verified)

    # Non-vacuous: loaded in B, the plain rules verify U1.1 as removable ...
    wrong = prove_pour_stubs(PCB.load(name))
    assert wrong is not None and [stub.pad_name for stub, _ in wrong.verified] == ["U1.1"]

    # ... and explicit rule files replace the project beside the loaded board.
    selected = select_pour_stubs(pcb, rule_files=[b / dru])
    assert selected is not None and [stub.pad_name for stub, _ in selected] == ["U1.1"]

    assert {project.name: _snapshot(project) for project in (a, b)} == before


# ---------------------------------------------------------------------------
# Design decisions
# ---------------------------------------------------------------------------


def test_design_decisions_follow_the_loaded_board_after_chdir(tmp_path, monkeypatch):
    """``get_decisions`` / ``record_decision`` use the loaded board's own ledger."""
    from kicad_tools.explain.rationale import (
        get_decision_store,
        get_decisions,
        record_decision,
        save_decisions,
    )

    board = (_VIA_HEAD.format(mask=0.0) + _EDGE_VIA).encode()
    a, b, name = _paired(tmp_path, monkeypatch, BOARD.name, board)
    for project, ref in ((a, "R1"), (b, "R2")):
        record_decision(
            PCB.load(str(project / name)),
            action="place",
            components=[ref],
            rationale=f"{project.name}: {ref}",
        )
    b_before = (b / LEDGER).read_bytes()

    def components(pcb: PCB) -> list[list[str]]:
        return sorted(ref for d in get_decisions(pcb) for ref in d.components)

    pcb = PCB.load(name)
    monkeypatch.chdir(b)
    assert components(pcb) == ["R1"]
    assert [d.components for d in get_decision_store(pcb)] == [["R1"]]

    record_decision(pcb, action="move", components=["U1"], rationale="a: moved")
    assert components(pcb) == ["R1", "U1"]
    assert (b / LEDGER).read_bytes() == b_before
    assert save_decisions(pcb) is True
    assert (b / LEDGER).read_bytes() == b_before
    assert components(PCB.load(str(a / name))) == ["R1", "U1"]
    assert components(PCB.load(str(b / name))) == ["R2"]
    assert pcb.path == name


def test_in_memory_and_path_only_boards_keep_their_behavior():
    """Boards created in memory, and PCB-like objects exposing only ``path``, are unchanged."""
    from kicad_tools.explain.rationale import get_decisions, record_decision, save_decisions
    from kicad_tools.schema.pcb import source_path_of

    created = PCB.create(width=10, height=10)
    assert created.path is None and source_path_of(created) is None
    assert get_decisions(created) == []
    record_decision(created, action="move", components=["U1"], rationale="in memory")
    assert [d.components for d in get_decisions(created)] == [["U1"]]
    assert save_decisions(created) is False

    assert source_path_of(SimpleNamespace(path=BOARD)) == BOARD
    assert source_path_of(object()) is None
