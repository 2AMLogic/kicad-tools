"""Issue #10357: plated via drills against the same-face mask, paste and SMD land.

Every fixture is synthetic and stands on its own: a small two-sided board plus
the drill and Gerber members of the archive ``kct export`` would ship. Sheet
coordinates are millimetres, y down, exactly as a ``.kicad_pcb`` stores them;
the plot helpers subtract the plot origin the way kicad-cli does with
``--use-drill-file-origin`` / ``--drill-origin plot``.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.export.via_land_overlap import CHECK_NAME, measure_via_land_overlap
from kicad_tools.schema.pcb import PCB

AUX = (100.0, 50.0)
ABSOLUTE = (0.0, 0.0)
BOARD = "probe"
FRONT = ("F.Cu", "F.Paste", "F.Mask")
BACK = ("B.Cu", "B.Paste", "B.Mask")
TWO_LAYER = (
    '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (34 "B.Paste" user) (35 "F.Paste" user)'
    ' (36 "B.SilkS" user) (37 "F.SilkS" user) (38 "B.Mask" user) (39 "F.Mask" user)'
    ' (44 "Edge.Cuts" user))'
)
FOUR_LAYER = (
    '(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)'
    ' (34 "B.Paste" user) (35 "F.Paste" user) (38 "B.Mask" user) (39 "F.Mask" user))'
)
POFV = {
    "process_id": "jlcpcb-tier1-pofv-4l",
    "name": "JLCPCB Capability Plus -- Plated-Over Filled Via (POFV), 4+ layer",
    "requires_filled_and_capped": True,
}
HEATSINK = {"source": "pad_property", "name": "pad_prop_heatsink"}
PROCESS = {"source": "fabrication_process", "process_id": "jlcpcb-tier1-pofv-4l"}
U1 = ("U1", "F.Cu", (110.0, 60.0))
U2 = ("U2", "B.Cu", (120.0, 70.0))
U5 = ("U5", "F.Cu", (115.0, 65.0))
COMPLETE = {"status": "complete", "reasons": []}


def _pad(number, at, size, layers, prop=None):
    property_node = f" (property {prop})" if prop else ""
    copper = " ".join(f'"{layer}"' for layer in layers)
    return (
        f'(pad "{number}" smd rect (at {at[0]} {at[1]}) (size {size[0]} {size[1]})'
        f'{property_node} (layers {copper}) (net 1 "GND"))'
    )


def _footprint(reference, side, at, *pads):
    label = (
        f'  (fp_text reference "{reference}" (at 0 -2) (layer "F.SilkS")'
        " (effects (font (size 1 1) (thickness 0.15))))\n"
    )
    return (
        f'(footprint "Probe:{reference}" (layer "{side}") (at {at[0]} {at[1]}) (attr smd)\n'
        + label
        + "".join(f"  {pad}\n" for pad in pads)
        + ")"
    )


def _land(spec, number="1", size=(1.0, 1.0), layers=FRONT, prop=None):
    """A 1 x 1 mm SMD land unless sized otherwise."""
    reference, side, at = spec
    return _footprint(reference, side, at, _pad(number, (0, 0), size, layers, prop))


def _via(at, index, drill=0.3):
    uuid = f"{index:08d}-1111-4111-8111-111111111111"
    return (
        f"(via (at {at[0]} {at[1]}) (size 0.6) (drill {drill}) (layers"
        f' "F.Cu" "B.Cu") (net 1) (uuid "{uuid}"))'
    )


def _board_text(*items, layers=TWO_LAYER, aux=AUX):
    return (
        '(kicad_pcb (version 20240108) (generator "test")\n'
        f"  {layers}\n"
        f"  (setup (aux_axis_origin {aux[0]} {aux[1]}))\n"
        '  (net 0 "")\n  (net 1 "GND")\n' + "".join(f"  {item}\n" for item in items) + ")\n"
    )


def _board(tmp_path, *items, **options):
    path = tmp_path / f"{BOARD}.kicad_pcb"
    path.write_text(_board_text(*items, **options))
    return PCB.load(path)


def _plot(x, y, origin=AUX):
    """Sheet millimetres -> Gerber coordinates in the ``origin`` plot frame."""
    return f"X{round((x - origin[0]) * 1e6)}Y{round((origin[1] - y) * 1e6)}"


def _hit(x, y, origin=AUX):
    """Sheet millimetres -> Excellon coordinates in the ``origin`` drill frame."""
    return f"X{x - origin[0]:.3f}Y{origin[1] - y:.3f}"


def _flash(code, x, y, origin=AUX):
    return f"D{code}*\n{_plot(x, y, origin)}D03*\n"


def _gerber(function, polarity, apertures, body, *, x2=True):
    """A Gerber layer; ``apertures`` maps a D-code to (width, height)."""
    attributes = "" if not x2 else f"%TF.FileFunction,{function}*%\n%TF.FilePolarity,{polarity}*%\n"
    return (
        attributes
        + "%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\nG01*\n"
        + "".join(f"%ADD{code}R,{w}X{h}*%\n" for code, (w, h) in apertures.items())
        + body
        + "M02*\n"
    )


def _layer(function, polarity, rects, *, x2=True, origin=AUX):
    """Rectangular flashes given as sheet (center x, center y, width, height)."""
    apertures = {code: (w, h) for code, (_, _, w, h) in enumerate(rects, 10)}
    body = "".join(_flash(code, x, y, origin) for code, (x, y, _, _) in enumerate(rects, 10))
    return _gerber(function, polarity, apertures, body, x2=x2)


def _drill(hits, origin=AUX):
    """A plated Excellon member; ``hits`` are sheet (x, y, drill diameter) triples."""
    diameters = sorted({diameter for _, _, diameter in hits})
    tools = "".join(f"T{i}C{diameter:.3f}\n" for i, diameter in enumerate(diameters, 1))
    body = "".join(
        f"T{diameters.index(diameter) + 1}\n{_hit(x, y, origin)}\n" for x, y, diameter in hits
    )
    return f"M48\n; #@! TF.FileFunction,Plated,1,2,PTH\nMETRIC\n{tools}%\nG90\nG05\n{body}M30\n"


def _archive(board, **layers):
    """Archive members for ``board``; each layer is a tuple of (x, y, w, h) flashes.

    The plated drill member carries one hit per board via, at its sheet
    position and with its own drill diameter; every unspecified layer is empty.
    """
    origin = layers.pop("origin", AUX)
    bx, by = board.board_origin
    hits = [(via.position[0] + bx, via.position[1] + by, via.drill) for via in board.vias]
    plots = {
        "mask_f": ("Soldermask,Top", "Negative", "F_Mask", "gts"),
        "paste_f": ("Paste,Top", "Positive", "F_Paste", "gtp"),
        "mask_b": ("Soldermask,Bottom", "Negative", "B_Mask", "gbs"),
        "paste_b": ("Paste,Bottom", "Positive", "B_Paste", "gbp"),
    }
    members = {f"{BOARD}-PTH.drl": _drill(hits, origin=origin)}
    members[f"{BOARD}-F_Cu.gtl"] = _layer("Copper,L1,Top", "Positive", ())
    members[f"{BOARD}-B_Cu.gbl"] = _layer("Copper,L2,Bottom", "Positive", ())
    for key, (function, polarity, name, extension) in plots.items():
        members[f"{BOARD}-{name}.{extension}"] = _layer(
            function, polarity, layers.get(key, ()), origin=origin
        )
    return members


def _window(spec, mask=1.1):
    """A mask opening and a stencil aperture over the land at ``spec``.

    The stencil aperture matches the 1 mm land and the mask opening is 0.05 mm
    larger on every side; every drill is 0.30 mm.
    """
    x, y = spec[2]
    return {"mask": ((x, y, mask, mask),), "paste": ((x, y, 1.0, 1.0),)}


def _front_window(members, spec=U1, origin=AUX):
    members[f"{BOARD}-F_Mask.gts"] = _layer(
        "Soldermask,Top", "Negative", _window(spec)["mask"], origin=origin
    )
    members[f"{BOARD}-F_Paste.gtp"] = _layer(
        "Paste,Top", "Positive", _window(spec)["paste"], origin=origin
    )
    return members


def _rows(result):
    assert result.name == CHECK_NAME
    return result.data["observations"]


def _one(result):
    (row,) = _rows(result)
    return row


def _identified(row):
    return {key: row[key] for key in ("face", "reference", "pad")}


@pytest.mark.parametrize(
    ("via_x", "distinct"),
    [(110.6, 1), (110.7, 0)],
)
def test_edge_overlap_qualifies_only_while_the_drill_reaches_the_aperture(
    tmp_path, via_x, distinct
):
    """A drill centered 0.10 mm outside the stencil edge qualifies; 0.20 mm clears it."""
    board = _board(tmp_path, _land(U1), _via((via_x, 60.0), 1))
    result = measure_via_land_overlap(
        board,
        _archive(board, mask_f=_window(U1)["mask"], paste_f=_window(U1)["paste"]),
        use_aux_origin=True,
    )

    assert result.status == ("WARN" if distinct else "OK")
    data = result.data
    assert (data["expected_vias"], data["matched_vias"]) == (1, 1)
    assert data["coverage"] == COMPLETE
    assert data["distinct_holes"] == distinct == len(data["observations"])
    assert data["frame"] == {"name": "excellon", "units": "mm", "origin": "aux_axis_origin"}
    if not distinct:
        assert "(coverage complete)" in result.message
        return
    row = _one(result)
    assert _identified(row) == {"face": "F", "reference": "U1", "pad": "1"}
    assert row["drill_file"] == f"{BOARD}-PTH.drl"
    assert row["drill_center_mm"] == [round(via_x - AUX[0], 3), -10.0]
    assert row["drill_diameter_mm"] == 0.3
    assert row["edge_overlap"] is True
    assert row["overlap_area_mm2"] == pytest.approx(0.0075, abs=2e-4)
    assert row["process_disposition"] == "undeclared"
    assert row["declarations"] == []
    assert data["faces"] == {
        "F": {"measured": True, "distinct_holes": 1, "observations": 1, "reasons": []},
        "B": {"measured": True, "distinct_holes": 0, "observations": 0, "reasons": []},
    }
    json.dumps(result.to_dict())


def test_a_contact_split_between_opposite_faces_does_not_qualify(tmp_path):
    """Openings on the front and a land on the back never meet on one face."""
    back_land_only = _land(("R2", "B.Cu", (130.0, 60.0)), layers=("B.Cu",))
    board = _board(tmp_path, back_land_only, _via((130.0, 60.0), 1))
    members = _archive(
        board,
        mask_f=((130.0, 60.0, 1.1, 1.1),),
        paste_f=((130.0, 60.0, 1.0, 1.0),),
    )

    result = measure_via_land_overlap(board, members, use_aux_origin=True)

    assert _rows(result) == []
    assert result.data["coverage"] == COMPLETE
    assert result.data["distinct_holes"] == 0
    assert result.status == "OK"


def test_disjoint_openings_inside_the_drill_disk_do_not_qualify(tmp_path):
    """The mask opens over the left half of the drill and the stencil over the right."""
    board = _board(tmp_path, _land(U1), _via((110.0, 60.0), 1))
    members = _archive(
        board,
        mask_f=((109.75, 60.0, 0.52, 1.1),),
        paste_f=((110.26, 60.0, 0.5, 1.0),),
    )

    result = measure_via_land_overlap(board, members, use_aux_origin=True)

    assert _rows(result) == []
    assert result.data["coverage"] == COMPLETE
    assert result.data["distinct_holes"] == 0


def test_a_stencil_window_void_clears_a_drill_inside_it(tmp_path):
    """A clear 0.5 mm square cut from the aperture clears the drill; its edge still reaches paste."""
    windowed = _gerber(
        "Paste,Top",
        "Positive",
        {10: (1.0, 1.0), 11: (0.5, 0.5)},
        _flash(10, 110.0, 60.0) + "%LPC*%\n" + _flash(11, 110.0, 60.0) + "%LPD*%\n",
    )
    board = _board(tmp_path, _land(U1), _via((110.0, 60.0), 1))
    members = _front_window(_archive(board))
    members[f"{BOARD}-F_Paste.gtp"] = windowed

    cleared = measure_via_land_overlap(board, members, use_aux_origin=True)

    assert _rows(cleared) == []
    assert cleared.data["coverage"] == COMPLETE

    board.vias[0].position = (110.2, 60.0)
    members[f"{BOARD}-PTH.drl"] = _drill([(110.2, 60.0, 0.3)])
    row = _one(measure_via_land_overlap(board, members, use_aux_origin=True))
    assert _identified(row) == {"face": "F", "reference": "U1", "pad": "1"}
    assert row["drill_center_mm"] == [10.2, -10.0]
    assert row["edge_overlap"] is True


def test_one_hole_observed_on_both_faces_is_counted_once(tmp_path):
    """The same hole under a front land and a back land is one hole, two observations."""
    front = _land(U1)
    back = _land(("U2", "B.Cu", (110.0, 60.0)), layers=BACK)
    board = _board(tmp_path, front, back, _via((110.0, 60.0), 1))
    members = _archive(
        board,
        mask_f=_window(U1)["mask"],
        paste_f=_window(U1)["paste"],
        mask_b=_window(U1)["mask"],
        paste_b=_window(U1)["paste"],
    )

    result = measure_via_land_overlap(board, members, use_aux_origin=True)

    rows = _rows(result)
    assert [_identified(row) for row in rows] == [
        {"face": "F", "reference": "U1", "pad": "1"},
        {"face": "B", "reference": "U2", "pad": "1"},
    ]
    assert [row["edge_overlap"] for row in rows] == [False, False]
    assert result.data["distinct_holes"] == 1
    assert len(rows) == 2
    assert {face: info["distinct_holes"] for face, info in result.data["faces"].items()} == {
        "F": 1,
        "B": 1,
    }


def test_an_overlap_below_the_drill_resolution_is_not_reported(tmp_path):
    """0.0003 mm of nominal penetration is inside the 0.0005 mm drill half-step."""
    board = _board(tmp_path, _land(U1), _via((110.65, 60.0), 1))
    members = _archive(board, mask_f=_window(U1)["mask"], paste_f=((110.0003, 60.0, 1, 1),))

    assert _rows(measure_via_land_overlap(board, members, use_aux_origin=True)) == []

    board.vias[0].position = (110.64, 60.0)
    members[f"{BOARD}-PTH.drl"] = _drill([(110.64, 60.0, 0.3)])
    assert len(_rows(measure_via_land_overlap(board, members, use_aux_origin=True))) == 1


def test_the_archive_is_read_not_loose_plot_files(tmp_path):
    board = _board(tmp_path, _land(U1), _via((110.6, 60.0), 1))
    members = _front_window(_archive(board))
    path = tmp_path / "gerbers.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)

    with zipfile.ZipFile(path) as archive:
        result = measure_via_land_overlap(board, archive, use_aux_origin=True)

    assert _identified(_one(result)) == {"face": "F", "reference": "U1", "pad": "1"}


def test_an_unreadable_archive_is_a_coverage_reason(tmp_path, monkeypatch):
    board = _board(tmp_path, _land(U1), _via((110.6, 60.0), 1))
    path = tmp_path / "gerbers.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{BOARD}-PTH.drl", _drill([(110.6, 60.0, 0.3)]))

    def broken():
        raise zipfile.BadZipFile("central directory is corrupt")

    with zipfile.ZipFile(path) as archive:
        monkeypatch.setattr(archive, "infolist", broken)
        result = measure_via_land_overlap(board, archive, use_aux_origin=True)

    assert result.status == "WARN"
    assert result.data["coverage"] == {"status": "partial", "reasons": ["archive_unreadable"]}
    assert result.data["distinct_holes"] is None
    assert result.data["observations"] == []
    assert "archive_unreadable" in result.message


def test_a_translated_board_outline_keeps_the_plot_and_the_lands_aligned(tmp_path):
    """``PCB.load`` moves a centered board to its outline origin; the plot does not."""
    outline = '(gr_rect (start 105 55) (end 125 75) (layer "Edge.Cuts") (width 0.1))'
    board = _board(tmp_path, outline, _land(U1), _via((110.6, 60.0), 1))
    assert board.board_origin == (105.0, 55.0)
    assert board.footprints[0].position == (5.0, 5.0)

    result = measure_via_land_overlap(board, _front_window(_archive(board)), use_aux_origin=True)

    assert _identified(_one(result)) == {"face": "F", "reference": "U1", "pad": "1"}


def test_an_absolute_drill_origin_registers_without_an_auxiliary_origin(tmp_path):
    board = _board(tmp_path, _land(U1), _via((110.6, 60.0), 1))
    members = _front_window(_archive(board, origin=ABSOLUTE), origin=ABSOLUTE)

    result = measure_via_land_overlap(board, members, use_aux_origin=False)

    row = _one(result)
    assert _identified(row) == {"face": "F", "reference": "U1", "pad": "1"}
    assert row["drill_center_mm"] == [110.6, -60.0]
    assert result.data["frame"] == {"name": "excellon", "units": "mm", "origin": "absolute"}


THERMAL = _footprint(
    "U3",
    "F.Cu",
    (150.0, 60.0),
    _pad("9", (0, 0), (2.0, 2.0), ("F.Cu", "F.Mask"), "pad_prop_heatsink"),
)
THERMAL_PLOTS = {"mask_f": ((150.0, 60.0, 2.1, 2.1),), "paste_f": ((150.0, 60.0, 1.0, 1.0),)}


def test_a_heatsink_pad_property_declares_the_thermal_land_but_a_name_does_not(tmp_path):
    """An exposed pad is copper and mask on the land and paste from a window aperture."""
    named = _footprint("U4", "F.Cu", (160.0, 60.0), _pad("EP", (0, 0), (1.0, 1.0), FRONT))
    board = _board(tmp_path, THERMAL, named, _via((150.0, 60.0), 1), _via((160.0, 60.0), 2))
    members = _archive(
        board,
        mask_f=(*THERMAL_PLOTS["mask_f"], (160.0, 60.0, 1.1, 1.1)),
        paste_f=(*THERMAL_PLOTS["paste_f"], (160.0, 60.0, 1.0, 1.0)),
    )

    result = measure_via_land_overlap(board, members, use_aux_origin=True)

    thermal_row, named_row = _rows(result)
    assert _identified(thermal_row) == {"face": "F", "reference": "U3", "pad": "9"}
    assert thermal_row["process_disposition"] == "exposed_thermal"
    assert thermal_row["declarations"] == [HEATSINK]
    assert _identified(named_row) == {"face": "F", "reference": "U4", "pad": "EP"}
    assert named_row["process_disposition"] == "undeclared"
    assert named_row["declarations"] == []
    assert result.status == "WARN"
    assert result.data["dispositions"] == {
        "undeclared": 1,
        "filled_capped": 0,
        "exposed_thermal": 1,
    }
    assert "1 undeclared" in result.message


def test_a_thermal_declaration_alone_reads_ok_and_stays_visible(tmp_path):
    board = _board(tmp_path, THERMAL, _via((150.0, 60.0), 1))

    result = measure_via_land_overlap(board, _archive(board, **THERMAL_PLOTS), use_aux_origin=True)

    row = _one(result)
    assert row["process_disposition"] == "exposed_thermal"
    assert result.status == "OK"
    assert "exposed_thermal" in result.message
    assert "F U3 pad 9" in result.details
    assert "\n" not in result.details


@pytest.mark.parametrize(
    ("process", "disposition", "declarations"),
    [
        (POFV, "filled_capped", [PROCESS]),
        (None, "undeclared", []),
        ({**POFV, "process_id": "not-a-registered-process"}, "undeclared", []),
    ],
)
def test_only_the_bundles_declared_process_establishes_filled_and_capped(
    tmp_path, process, disposition, declarations
):
    """Without a declared process the intersection is undeclared, capability or not."""
    board = _board(tmp_path, _land(U1), _via((110.6, 60.0), 1))

    result = measure_via_land_overlap(
        board, _front_window(_archive(board)), use_aux_origin=True, fabrication_process=process
    )

    row = _one(result)
    assert row["process_disposition"] == disposition
    assert row["declarations"] == declarations
    assert result.status == ("OK" if disposition == "filled_capped" else "WARN")


def test_the_declared_process_does_not_reach_a_drill_outside_its_window(tmp_path):
    """The POFV process publishes a 0.2-0.5 mm drill window."""
    board = _board(tmp_path, _land(U1, size=(1.4, 1.4)), _via((110.6, 60.0), 1, drill=0.6))
    members = _archive(board, mask_f=((110.0, 60.0, 1.5, 1.5),), paste_f=((110.0, 60.0, 1.4, 1.4),))

    row = _one(
        measure_via_land_overlap(board, members, use_aux_origin=True, fabrication_process=POFV)
    )

    assert row["drill_diameter_mm"] == 0.6
    assert row["process_disposition"] == "undeclared"
    assert row["declarations"] == []


@pytest.mark.parametrize(
    ("name", "function", "opening", "covering"),
    [
        ("F_Mask.gts", "Soldermask,Top", "Negative", "Positive"),
        ("F_Paste.gtp", "Paste,Top", "Positive", "Negative"),
    ],
)
def test_the_plotted_polarity_decides_whether_the_flash_is_an_opening(
    tmp_path, name, function, opening, covering
):
    """A plotted island is an opening under the conventional polarity, material otherwise.

    The island covers the land U5 sits on. As an opening it exposes the drill
    at that land's edge; as material, only the clear flashes cut out of it are
    open, and the one over U5 stops short of the drill.
    """
    square = 1.1 if name.endswith(".gts") else 1.0
    apertures = {10: (square, square), 11: (5.0, 5.0), 12: (0.8, 0.8), 20: (20.0, 20.0)}
    as_opening = _flash(10, *U1[2]) + _flash(11, *U5[2]) + "%LPC*%\n" + _flash(12, *U5[2])
    as_material = _flash(20, 112.5, 62.5) + "%LPC*%\n" + _flash(10, *U1[2]) + _flash(12, *U5[2])

    def measure(polarity):
        board = _board(tmp_path, _land(U1), _land(U5), _via((115.6, 65.0), 1))
        members = _archive(
            board,
            mask_f=(*_window(U1)["mask"], *_window(U5)["mask"]),
            paste_f=(*_window(U1)["paste"], *_window(U5)["paste"]),
        )
        body = as_opening if polarity == opening else as_material
        members[f"{BOARD}-{name}"] = _gerber(function, polarity, apertures, body + "%LPD*%\n")
        return measure_via_land_overlap(board, members, use_aux_origin=True)

    opened, covered = measure(opening), measure(covering)

    assert _identified(_one(opened)) == {"face": "F", "reference": "U5", "pad": "1"}
    assert _rows(covered) == []
    assert covered.data["coverage"] == COMPLETE


def test_layers_without_x2_attributes_fall_back_to_their_names_and_convention(tmp_path):
    board = _board(tmp_path, _land(U1), _via((110.0, 60.0), 1))
    members = _front_window(_archive(board))
    stripped = {
        name: "".join(line + "\n" for line in text.splitlines() if not line.startswith("%TF."))
        for name, text in members.items()
    }

    result = measure_via_land_overlap(board, stripped, use_aux_origin=True)

    assert _identified(_one(result)) == {"face": "F", "reference": "U1", "pad": "1"}


def test_the_x2_file_function_identifies_a_renamed_member(tmp_path):
    board = _board(tmp_path, _land(U1), _via((110.0, 60.0), 1))
    members = _front_window(_archive(board))
    renamed = {
        "fab/top-mask.gbr": members.pop(f"{BOARD}-F_Mask.gts"),
        "fab/top-paste.gbr": members.pop(f"{BOARD}-F_Paste.gtp"),
        **members,
    }

    result = measure_via_land_overlap(board, renamed, use_aux_origin=True)

    assert _identified(_one(result)) == {"face": "F", "reference": "U1", "pad": "1"}
    assert result.data["faces"]["F"]["reasons"] == []


def test_two_members_claiming_one_layer_leave_the_face_unmeasured(tmp_path):
    board = _board(tmp_path, _land(U1), _via((110.0, 60.0), 1))
    members = _front_window(_archive(board))
    members["variant-b/F_Paste.gtp"] = members[f"{BOARD}-F_Paste.gtp"]

    result = measure_via_land_overlap(board, members, use_aux_origin=True)

    assert _rows(result) == []
    assert result.data["faces"]["F"] == {
        "measured": False,
        "distinct_holes": 0,
        "observations": 0,
        "reasons": ["F.Paste:duplicate"],
    }
    assert result.data["coverage"] == {"status": "partial", "reasons": ["F.Paste:duplicate"]}


def _two_sided(tmp_path):
    front, back = _land(U1), _land(U2, layers=BACK)
    board = _board(tmp_path, front, back, _via((110.6, 60.0), 1), _via((120.0, 70.0), 2))
    members = _archive(
        board,
        mask_f=_window(U1)["mask"],
        paste_f=_window(U1)["paste"],
        mask_b=_window(U2)["mask"],
        paste_b=_window(U2)["paste"],
    )
    return board, members


def test_a_missing_plot_keeps_the_measured_faces_findings_and_marks_coverage_partial(
    tmp_path,
):
    board, members = _two_sided(tmp_path)
    complete = measure_via_land_overlap(board, members, use_aux_origin=True)
    assert complete.data["coverage"] == COMPLETE
    assert complete.data["distinct_holes"] == 2

    del members[f"{BOARD}-B_Paste.gbp"]
    partial = measure_via_land_overlap(board, members, use_aux_origin=True)

    assert [_identified(row) for row in _rows(partial)] == [
        {"face": "F", "reference": "U1", "pad": "1"}
    ]
    assert partial.data["distinct_holes"] == 1
    assert partial.data["faces"]["F"] == {
        "measured": True,
        "distinct_holes": 1,
        "observations": 1,
        "reasons": [],
    }
    assert partial.data["faces"]["B"] == {
        "measured": False,
        "distinct_holes": 0,
        "observations": 0,
        "reasons": ["B.Paste:missing"],
    }
    assert partial.data["coverage"] == {"status": "partial", "reasons": ["B.Paste:missing"]}
    assert (partial.data["expected_vias"], partial.data["matched_vias"]) == (2, 2)
    assert partial.status == "WARN"
    assert "B.Paste:missing" in partial.message


def test_a_partial_zero_is_not_reported_as_complete(tmp_path):
    board = _board(tmp_path, _land(U1), _via((115.0, 65.0), 1))
    members = _front_window(_archive(board))
    del members[f"{BOARD}-B_Mask.gbs"]

    result = measure_via_land_overlap(board, members, use_aux_origin=True)

    assert _rows(result) == []
    assert result.data["distinct_holes"] == 0
    assert result.data["coverage"] == {"status": "partial", "reasons": ["B.Mask:missing"]}
    assert result.status == "WARN"
    assert "partial" in result.message
    assert "complete" not in result.message


def test_an_unsupported_plot_leaves_the_other_face_measured(tmp_path):
    board = _board(tmp_path, _land(U2, layers=BACK), _via((120.0, 70.0), 1))
    members = _archive(board, mask_b=_window(U2)["mask"], paste_b=_window(U2)["paste"])
    members[f"{BOARD}-F_Paste.gtp"] = "%TF.FileFunction,Paste,Top*%\n%MOMM*%\nG42*\nM02*%\n"

    result = measure_via_land_overlap(board, members, use_aux_origin=True)

    assert _identified(_one(result)) == {"face": "B", "reference": "U2", "pad": "1"}
    assert result.data["faces"]["F"]["reasons"] == ["F.Paste:unsupported"]
    assert result.data["coverage"] == {"status": "partial", "reasons": ["F.Paste:unsupported"]}


def test_an_unregistered_drill_leaves_coverage_partial(tmp_path):
    board = _board(tmp_path, _land(U1), _via((110.6, 60.0), 1))
    members = _front_window(_archive(board))

    result = measure_via_land_overlap(board, members, use_aux_origin=None)

    assert result.data["coverage"] == {"status": "partial", "reasons": ["registration_unknown"]}
    assert (result.data["expected_vias"], result.data["matched_vias"]) == (1, 0)
    assert _rows(result) == []
    assert result.status == "WARN"


def test_a_plot_written_for_another_origin_leaves_the_face_unmeasured(tmp_path):
    """The drill matches a board via, but the plots sit 1 mm away from the lands."""
    board = _board(tmp_path, _land(U1), _via((110.6, 60.0), 1))
    members = _archive(
        board,
        mask_f=_window(U1)["mask"],
        paste_f=_window(U1)["paste"],
        origin=(99.0, 49.0),
    )
    members[f"{BOARD}-PTH.drl"] = _drill([(110.6, 60.0, 0.3)])

    result = measure_via_land_overlap(board, members, use_aux_origin=True)

    reasons = ["F.Mask:unregistered", "F.Paste:unregistered"]
    assert (result.data["expected_vias"], result.data["matched_vias"]) == (1, 1)
    assert _rows(result) == []
    assert result.data["faces"]["F"] == {
        "measured": False,
        "distinct_holes": 0,
        "observations": 0,
        "reasons": reasons,
    }
    assert result.data["coverage"] == {"status": "partial", "reasons": reasons}
    assert "F.Mask:unregistered" in result.message


def test_an_unmatched_via_keeps_the_verified_hit_finding_and_marks_coverage_partial(tmp_path):
    board = _board(tmp_path, _land(U1), _via((110.6, 60.0), 1), _via((115.0, 65.0), 2))
    members = _front_window(_archive(board))
    members[f"{BOARD}-PTH.drl"] = _drill([(110.6, 60.0, 0.3), (115.02, 65.0, 0.3)])

    result = measure_via_land_overlap(board, members, use_aux_origin=True)

    assert result.data["coverage"] == {"status": "partial", "reasons": ["unmatched_via"]}
    assert (result.data["expected_vias"], result.data["matched_vias"]) == (2, 1)
    assert _identified(_one(result)) == {"face": "F", "reference": "U1", "pad": "1"}
    assert result.status == "WARN"
    assert "2 expected, 1 matched" in result.message


def test_overlapping_lands_with_different_owners_are_ambiguous(tmp_path):
    stacked = _footprint(
        "U1",
        "F.Cu",
        (110.0, 60.0),
        _pad("1", (0, 0), (1, 1), FRONT),
        _pad("2", (0, 0), (1, 1), FRONT),
    )
    board = _board(tmp_path, stacked, _via((110.0, 60.0), 1))

    result = measure_via_land_overlap(board, _front_window(_archive(board)), use_aux_origin=True)

    assert _rows(result) == []
    assert result.data["distinct_holes"] == 0
    assert result.data["coverage"] == {"status": "partial", "reasons": ["ambiguous_land"]}
    assert result.data["faces"]["F"] == {
        "measured": True,
        "distinct_holes": 0,
        "observations": 0,
        "reasons": ["ambiguous_land"],
    }
    assert result.status == "WARN"
    assert "ambiguous_land" in result.message


def test_an_intersection_partly_on_two_lands_keeps_its_unambiguous_part(tmp_path):
    """Only the part of the opening that belongs to two lands at once is withheld."""
    overlapping = _footprint(
        "U1",
        "F.Cu",
        (110.0, 60.0),
        _pad("1", (0, 0), (1, 1), FRONT),
        _pad("2", (0.5, 0), (0.5, 1), FRONT),
    )
    board = _board(tmp_path, overlapping, _via((110.4, 60.0), 1))
    members = _archive(
        board, mask_f=((110.25, 60.0, 1.6, 1.1),), paste_f=((110.25, 60.0, 1.4, 1.0),)
    )

    result = measure_via_land_overlap(board, members, use_aux_origin=True)

    row = _one(result)
    assert _identified(row) == {"face": "F", "reference": "U1", "pad": "2"}
    assert row["overlap_area_mm2"] == pytest.approx(0.0075, abs=2e-4)
    assert result.data["distinct_holes"] == 1
    assert result.data["faces"]["F"] == {
        "measured": True,
        "distinct_holes": 1,
        "observations": 1,
        "reasons": ["ambiguous_land"],
    }
    assert result.data["coverage"] == {"status": "partial", "reasons": ["ambiguous_land"]}


def test_pad_objects_sharing_a_number_are_one_land(tmp_path):
    """KiCad library thermal pads are several pads with the same number."""
    split = _footprint(
        "U1",
        "F.Cu",
        (110.0, 60.0),
        _pad("9", (-0.25, 0), (0.5, 1.0), FRONT, "pad_prop_heatsink"),
        _pad("9", (0.25, 0), (0.5, 1.0), FRONT, "pad_prop_heatsink"),
    )
    board = _board(tmp_path, split, _via((110.0, 60.0), 1))

    result = measure_via_land_overlap(board, _front_window(_archive(board)), use_aux_origin=True)

    row = _one(result)
    assert _identified(row) == {"face": "F", "reference": "U1", "pad": "9"}
    assert row["process_disposition"] == "exposed_thermal"
    assert result.data["coverage"] == COMPLETE


def test_a_through_hole_land_is_not_an_smd_land(tmp_path):
    footprint = (
        '(footprint "Probe:J1" (layer "F.Cu") (at 110 60) (attr through_hole)\n'
        '  (fp_text reference "J1" (at 0 -2) (layer "F.SilkS")'
        " (effects (font (size 1 1) (thickness 0.15))))\n"
        '  (pad "1" thru_hole rect (at 0 0) (size 1 1) (drill 0.6)'
        ' (layers "*.Cu" "*.Mask") (net 1 "GND"))\n'
        ")\n"
    )
    board = _board(tmp_path, footprint, _via((110.6, 60.0), 1))
    members = _archive(
        board,
        mask_f=_window(U1)["mask"],
        paste_f=_window(U1)["paste"],
        mask_b=_window(U1)["mask"],
        paste_b=_window(U1)["paste"],
    )

    result = measure_via_land_overlap(board, members, use_aux_origin=True)

    assert _rows(result) == []
    assert result.data["coverage"] == COMPLETE
    assert result.data["distinct_holes"] == 0


def test_a_board_with_no_vias_is_a_complete_zero(tmp_path):
    board = _board(tmp_path, _land(U1))

    result = measure_via_land_overlap(board, _archive(board), use_aux_origin=True)

    assert result.status == "OK"
    assert result.data["distinct_holes"] == 0
    assert result.data["coverage"] == COMPLETE
    assert result.data["faces"] == {}
    assert (result.data["expected_vias"], result.data["matched_vias"]) == (0, 0)


def test_a_buried_via_reaching_no_outer_face_has_nothing_to_measure(tmp_path):
    buried = (
        '(via buried (at 115 65) (size 0.55) (drill 0.2) (layers "In1.Cu" "In2.Cu")'
        ' (net 1) (uuid "00000001-1111-4111-8111-111111111111"))'
    )
    board = _board(tmp_path, buried, layers=FOUR_LAYER)
    members = {
        f"{BOARD}-in1-in2.drl": (
            "M48\n; #@! TF.FileFunction,Plated,2,3,Buried\nMETRIC\nT1C0.200\n%\nG90\nG05\n"
            f"T1\n{_hit(115.0, 65.0)}\nM30\n"
        ),
        **{name: text for name, text in _archive(board).items() if not name.endswith(".drl")},
    }

    result = measure_via_land_overlap(board, members, use_aux_origin=True)

    assert (result.data["expected_vias"], result.data["matched_vias"]) == (1, 1)
    assert _rows(result) == []
    assert result.data["faces"] == {}
    assert result.data["coverage"] == COMPLETE


def _export_fixture(tmp_path):
    """A project whose mocked assembly stage ships the synthetic archive."""
    from kicad_tools.export import assembly

    project = tmp_path / "project"
    project.mkdir()
    pcb = project / f"{BOARD}.kicad_pcb"
    pcb.write_text(_board_text(_land(U1), _via((110.6, 60.0), 1)))
    (project / f"{BOARD}.kicad_sch").write_text("(kicad_sch)")
    (project / f"{BOARD}.kicad_pro").write_text("{}")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    members = _front_window(_archive(_board(scratch, _land(U1), _via((110.6, 60.0), 1))))

    def fake_export(self, output_dir=None):
        od = Path(output_dir) if output_dir else self.config.output_dir
        od.mkdir(parents=True, exist_ok=True)
        (od / "bom_jlcpcb.csv").write_text("Comment,Designator,Footprint,LCSC Part #\n")
        gerbers = od / self.config.gerbers_subdir / "gerbers.zip"
        gerbers.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(gerbers, "w") as archive:
            for name, text in members.items():
                archive.writestr(name, text)
        return assembly.AssemblyPackageResult(
            output_dir=od, bom_path=od / "bom_jlcpcb.csv", gerber_path=gerbers
        )

    return pcb, fake_export


def _export_rc(pcb, out, *flags):
    from kicad_tools.cli import export_cmd

    return export_cmd.main(
        [
            str(pcb),
            "--mfr",
            "jlcpcb",
            "-o",
            str(out),
            "--no-report",
            "--no-silk-fix",
            "--skip-preflight",
            "--format",
            "json",
            *flags,
        ]
    )


class TestExportEntryPoint:
    @staticmethod
    def _run(pcb, monkeypatch, capsys, out, *flags, assembly_export):
        from kicad_tools.export import assembly

        monkeypatch.setattr(assembly.AssemblyPackage, "export", assembly_export)
        rc = _export_rc(pcb, out, *flags)
        assert rc == 0, capsys.readouterr().out
        return out, json.loads(capsys.readouterr().out)

    def test_opt_in_records_the_measurement_in_json_and_the_manifest(
        self, tmp_path, monkeypatch, capsys
    ):
        pcb, fake_export = _export_fixture(tmp_path)
        out, exported = self._run(
            pcb,
            monkeypatch,
            capsys,
            tmp_path / "manufacturing",
            "--via-land-overlap",
            assembly_export=fake_export,
        )

        (entry,) = exported["preflight"]
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["preflight"] == exported["preflight"]

        assert entry["name"] == CHECK_NAME
        assert entry["status"] == "WARN"
        record = entry["data"]
        assert record["archive"] == "gerbers/gerbers.zip"
        assert (
            record["archive_sha256"]
            == hashlib.sha256((out / "gerbers" / "gerbers.zip").read_bytes()).hexdigest()
        )
        assert record["frame"]["origin"] == "aux_axis_origin"
        assert record["distinct_holes"] == 1
        assert _identified(record["observations"][0]) == {
            "face": "F",
            "reference": "U1",
            "pad": "1",
        }

    def test_the_default_export_is_unchanged(self, tmp_path, monkeypatch, capsys):
        from kicad_tools.export.manufacturing import ManufacturingConfig

        pcb, fake_export = _export_fixture(tmp_path)
        out, exported = self._run(
            pcb,
            monkeypatch,
            capsys,
            tmp_path / "manufacturing",
            assembly_export=fake_export,
        )

        assert ManufacturingConfig().via_land_overlap is False
        assert "preflight" not in exported
        assert "preflight" not in json.loads((out / "manifest.json").read_text())

    def test_a_missing_archive_is_a_coverage_reason(self, tmp_path, monkeypatch, capsys):
        pcb, fake_export = _export_fixture(tmp_path)

        def no_gerbers(self, output_dir=None):
            result = fake_export(self, output_dir)
            result.gerber_path.unlink()
            return result

        _out, exported = self._run(
            pcb,
            monkeypatch,
            capsys,
            tmp_path / "manufacturing",
            "--via-land-overlap",
            assembly_export=no_gerbers,
        )

        (entry,) = exported["preflight"]
        assert entry["name"] == CHECK_NAME
        assert entry["status"] == "WARN"
        assert entry["data"]["coverage"] == {"status": "partial", "reasons": ["archive_missing"]}
        assert entry["data"]["distinct_holes"] is None
        assert entry["data"]["expected_vias"] is None
        assert entry["data"]["observations"] == []
        assert "archive_missing" in entry["message"]

    def test_an_unreadable_archive_is_a_coverage_reason(self, tmp_path, monkeypatch, capsys):
        pcb, fake_export = _export_fixture(tmp_path)

        def corrupt(self, output_dir=None):
            result = fake_export(self, output_dir)
            result.gerber_path.write_bytes(b"PK\x03\x04 not an archive")
            return result

        _out, exported = self._run(
            pcb,
            monkeypatch,
            capsys,
            tmp_path / "manufacturing",
            "--via-land-overlap",
            assembly_export=corrupt,
        )

        (entry,) = exported["preflight"]
        assert entry["status"] == "WARN"
        assert entry["data"]["coverage"] == {
            "status": "partial",
            "reasons": ["archive_unreadable"],
        }
        assert entry["data"]["archive_sha256"] is not None

    def test_a_failed_measurement_never_breaks_the_export(self, tmp_path, monkeypatch, capsys):
        from kicad_tools.export import via_land_overlap

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        pcb, fake_export = _export_fixture(tmp_path)
        monkeypatch.setattr(via_land_overlap, "measure_via_land_overlap", boom)
        out, exported = self._run(
            pcb,
            monkeypatch,
            capsys,
            tmp_path / "manufacturing",
            "--via-land-overlap",
            assembly_export=fake_export,
        )

        assert exported["success"] is True
        (entry,) = exported["preflight"]
        assert entry["status"] == "WARN"
        assert entry["data"]["coverage"] == {
            "status": "partial",
            "reasons": ["measurement_failed:RuntimeError"],
        }
        assert (out / "manifest.json").exists()

    def test_the_flag_reaches_the_config(self):
        from kicad_tools.cli.export_cmd import build_manufacturing_config

        config = build_manufacturing_config(
            output_dir=Path("out"), via_land_overlap=True, silk_fix=False
        )
        assert config.via_land_overlap is True
        assert build_manufacturing_config(output_dir=Path("out")).via_land_overlap is False


@pytest.mark.parametrize(
    ("manufacturer", "custom", "expected"),
    [
        ("jlcpcb", None, True),
        ("jlcpcb-tier1", None, True),
        ("oshpark", None, False),
        ("generic", None, True),
        ("unknown-fab", None, True),
        ("generic", "plot-absolute", False),
    ],
)
def test_the_measured_origin_is_the_one_the_package_plots_with(
    tmp_path, monkeypatch, manufacturer, custom, expected
):
    """``plot_use_aux_origin`` must agree with ``AssemblyPackage._generate_gerbers``."""
    from kicad_tools.export import assembly
    from kicad_tools.export.gerber import GerberConfig, GerberExporter

    seen = {}

    def capture(self, config, output_dir=None, progress_callback=None, **_options):
        seen["origin"] = config.use_aux_origin
        return tmp_path / "gerbers.zip"

    monkeypatch.setattr(GerberExporter, "export", capture)
    monkeypatch.setattr(GerberExporter, "__init__", lambda self, path: None)
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    gerber_config = GerberConfig(use_aux_origin=False) if custom else None
    package = assembly.AssemblyPackage(
        pcb,
        manufacturer=manufacturer,
        config=assembly.AssemblyConfig(
            include_bom=False, bom_source="pcb", gerber_config=gerber_config
        ),
    )
    package._generate_gerbers(tmp_path)

    assert seen["origin"] is expected
    assert assembly.plot_use_aux_origin(package.fab_family, package.config) is seen["origin"]


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
def test_a_kicad_cli_archive_measures_as_plotted(tmp_path):
    """Black-box check on the pinned toolchain's own drill and Gerber output."""
    from kicad_tools.export.gerber import GerberExporter

    pcb = tmp_path / "plotted.kicad_pcb"
    outline = '(gr_rect (start 105 55) (end 125 75) (layer "Edge.Cuts") (width 0.1))'
    pcb.write_text(
        _board_text(
            outline,
            _land(U1),
            _land(U2, layers=BACK),
            _via((110.6, 60.0), 1),
            _via((120.0, 70.0), 2),
            _via((115.0, 65.0), 3),
        )
    )
    zip_path = GerberExporter(pcb).export_for_manufacturer("jlcpcb", tmp_path / "gerbers")
    board = PCB.load(pcb)

    with zipfile.ZipFile(zip_path) as archive:
        result = measure_via_land_overlap(board, archive, use_aux_origin=True)

    rows = _rows(result)
    assert [_identified(row) for row in rows] == [
        {"face": "F", "reference": "U1", "pad": "1"},
        {"face": "B", "reference": "U2", "pad": "1"},
    ]
    assert [row["edge_overlap"] for row in rows] == [True, False]
    assert [row["drill_center_mm"] for row in rows] == [[10.6, -10.0], [20.0, -20.0]]
    assert result.data["distinct_holes"] == 2
    assert result.data["coverage"] == COMPLETE
    assert (result.data["expected_vias"], result.data["matched_vias"]) == (3, 3)
