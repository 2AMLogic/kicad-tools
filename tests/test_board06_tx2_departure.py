"""J1/B2 positive escape, using both seed-42 TX2 rails and their actual terminal pads."""

import json
import shutil
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.router.rules import net_class_map_to_dict
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.mask_copper import default_request_for_manufacturer

ROOT = Path(__file__).resolve().parents[1]
BOARD06 = ROOT / "boards/06-diffpair-test"
NETS = ("USB3_TX2+", "USB3_TX2-")
OLD_BEND = "287d06ca-6f4c-469a-8b22-d3081c8eaee9"


@pytest.fixture
def specimen(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(BOARD06))
    import pad_departures
    from generate_design import usb3_net_class

    path = tmp_path / "tx2.kicad_pcb"
    shutil.copyfile(ROOT / "tests/fixtures/board06_j1_tx2.kicad_pcb", path)
    project = {
        "board": {"design_settings": {"rules": {"min_clearance": 0.1016}}},
        "net_settings": {
            "classes": [
                {"name": "Default", "clearance": 0.1016},
                {"name": "USB3", "clearance": 0.15},
                {"name": "Power", "clearance": 0.20},
            ],
            "netclass_assignments": {**dict.fromkeys(NETS, "USB3"), "GND": "Power"},
        },
    }
    path.with_suffix(".kicad_pro").write_text(json.dumps(project))
    path.with_suffix(".kicad_dru").write_text("(version 1)\n")
    path.with_name("net_class_map.json").write_text(
        json.dumps(net_class_map_to_dict(dict.fromkeys(NETS, usb3_net_class())))
    )
    return path, pad_departures


def run(specimen):
    path, departures = specimen
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    policy = default_request_for_manufacturer("jlcpcb").policy
    return departures._clear_tx2_exit(path, rules, policy)


def geometry(board, net):
    return {s.uuid: (s.start, s.end, s.width, s.layer) for s in board.segments if s.net_name == net}


@pytest.fixture
def local_specimen(specimen, monkeypatch):
    path, departures = specimen
    original_ids = set(geometry(PCB.load(path), NETS[0]))

    def accept_drc(candidate, *, output_path, **kwargs):
        assert (
            candidate.with_suffix(".kicad_dru").read_bytes()
            == path.with_suffix(".kicad_dru").read_bytes()
        )
        output_path.write_text(json.dumps({"violations": [], "unconnected_items": []}))
        return SimpleNamespace(success=True)

    def accept_mask(candidate, policy):
        changed = geometry(PCB.load(candidate), NETS[0]).keys() - original_ids
        return SimpleNamespace(
            coverage="complete",
            measurements=[
                SimpleNamespace(
                    conductor_uuid=uuid, owner_ref="J1/B2", disposition="within_own_pad"
                )
                for uuid in changed
            ],
        )

    monkeypatch.setattr(
        departures, "active_evidence_toolchain", lambda: SimpleNamespace(kicad_cli="kicad-cli")
    )
    monkeypatch.setattr(departures, "run_drc", accept_drc)
    monkeypatch.setattr(departures, "check_mask_to_copper", accept_mask)
    return specimen


def test_tx2_native_rules_and_mask_accept_the_exit(specimen):
    from kicad_tools.cli.kicad_toolchain import PINNED_KICAD_VERSION, version_number
    from kicad_tools.cli.runner import find_kicad_cli, get_kicad_version

    cli = find_kicad_cli()
    if cli is None or version_number(get_kicad_version(cli)) != PINNED_KICAD_VERSION:
        pytest.skip("The native TX2 regression uses the pinned KiCad toolchain")
    assert run(specimen) == 1


def test_tx2_changes_only_the_positive_exit_and_is_idempotent(local_specimen):
    path, _ = local_specimen
    before = PCB.load(path)
    assert run(local_specimen) == 1
    after = PCB.load(path)
    assert geometry(before, NETS[1]) == geometry(after, NETS[1])
    original, repaired = (geometry(board, NETS[0]) for board in (before, after))
    assert len(original.keys() - repaired.keys()) == 4
    new_tracks = [repaired[key] for key in repaired.keys() - original.keys()]
    assert len(new_tracks) == 6
    assert all(width == 0.275 and layer == "F.Cu" for _, _, width, layer in new_tracks)
    center = after.get_pad_position("J1", "B2")

    def offset(point):
        return tuple(round(value - origin, 4) for value, origin in zip(point, center, strict=True))

    actual = {frozenset((offset(start), offset(end))) for start, end, _, _ in new_tracks}
    expected = [
        (0, 0),
        (0, 0.28),
        (0.43, 0.71),
        (0.5402, 0.71),
        (0.5952, 0.765),
        (0.5952, 0.8637),
        (0.6806, 0.9491),
    ]
    assert actual == {frozenset((p, q)) for p, q in pairwise(expected)}
    for key in original.keys() & repaired.keys():
        assert original[key] == repaired[key]
    applied = path.read_bytes()
    assert run(local_specimen) == 0
    assert path.read_bytes() == applied


@pytest.mark.parametrize(
    "control",
    [
        "partner",
        "ground",
        "group",
        "source",
        "branch",
        "tap",
        "mask",
        "project",
        "invalid_project",
        "missing_rules",
        "nonfile_rules",
    ],
)
def test_tx2_veto_preserves_the_source(local_specimen, monkeypatch, control):
    path, departures = local_specimen
    text = path.read_text()
    if control == "partner":
        # This mask-clean launch retains length, but comes within 0.127061 mm of TX2−.
        new = ((0, 0), (0.5834, 0.5834), (0.5834, 0.9491), (0.6806, 0.9491))
        monkeypatch.setattr(departures, "_TX2_EXIT", replace(departures._TX2_EXIT, new=new))
    elif control == "ground":
        # Clears partner copper but reaches 0.1714 mm from the 0.20 mm J1/B1 land.
        new = ((0, 0), (0.45, 0.45), (0.58, 0.45), (0.62, 0.49), (0.62, 0.8885), (0.6806, 0.9491))
        monkeypatch.setattr(departures, "_TX2_EXIT", replace(departures._TX2_EXIT, new=new))
    elif control == "group":
        text = text.rstrip()[:-1] + f'(group "Connector" (members "{OLD_BEND}"))\n)\n'
    elif control == "source":
        text = text.replace("19.5 10.3657", "19.5 10.3757")
    elif control == "tap":
        # Both shapes still touch this same-net via inside the pad. It is a separate contact.
        text = (
            text.rstrip()[:-1]
            + '(via (at 19.5 10.09) (size .2) (drill .1) (layers "F.Cu" "B.Cu") (net 1))\n)\n'
        )
    elif control in {"missing_rules", "nonfile_rules"}:
        rules = path.with_suffix(".kicad_dru")
        rules.unlink()
        if control == "nonfile_rules":
            rules.mkdir()
    elif control == "project":
        path.with_suffix(".kicad_pro").unlink()
    elif control == "invalid_project":
        path.with_suffix(".kicad_pro").write_text("{")
    elif control == "branch":
        text = (
            text.rstrip()[:-1]
            + '(segment (start 19.5 10.3657) (end 19 11) (width 0.275) (layer "F.Cu") (net 1))\n)\n'
        )
    else:
        text = text.replace("(pad_to_mask_clearance 0)", "(pad_to_mask_clearance 0.12)")
    path.write_text(text)
    original = path.read_bytes()
    assert run(local_specimen) == 0
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("diff", "single", "expected"),
    [(120.0, 60.0, 0), (90.0, 50.0, 1)],
    ids=["diff-out-of-spec-with-single", "diff-in-spec-with-single"],
)
def test_tx2_guard_judges_the_differential_target_when_both_declared(
    local_specimen, diff, single, expected
):
    path, _ = local_specimen
    mapping = json.loads(path.with_name("net_class_map.json").read_text())
    for entry in mapping.values():
        entry["target_diff_impedance"] = diff
        entry["target_single_impedance"] = single
    path.with_name("net_class_map.json").write_text(json.dumps(mapping))
    original = path.read_bytes()
    assert run(local_specimen) == expected
    if not expected:
        assert path.read_bytes() == original
