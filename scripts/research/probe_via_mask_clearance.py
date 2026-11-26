#!/usr/bin/env python3
"""Black-box probe (Issue #10384): is an untented via's solder-mask opening
expanded by the board's ``pad_to_mask_clearance``?

Everything here is measured on the pinned KiCad image; nothing is inferred.

Part A -- plot.  For every board-level clearance variant this writes one
synthetic board, plots the mask layers and reads each flashed aperture's
diameter straight out of the X2 Gerber (``%ADD`` / ``D03``), so nothing depends
on rasterising the plot.  Pads sit next to the vias as a control group: the pad
side is known to follow the board clearance, so a probe whose pads do not move
between variants is measuring the wrong thing.  It then asks the parser whether
a via accepts a local ``solder_mask_margin`` node at all.

Part B -- DRC.  One untented via or round SMD pad per silk line, each line at a
controlled distance from its feature, through ``kicad-cli pcb drc`` and
``kct check --mfr jlcpcb`` on the same board. Thin lines locate the copper
edge and thick lines test the ring inside an expanded mask opening.

Run from a checkout::

    KCT_KICAD_DOCKER=1 uv run python scripts/research/probe_via_mask_clearance.py
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kicad_tools.cli.kicad_toolchain import (
    KICAD_DOCKER_ENV,
    PINNED_KICAD_VERSION,
    docker_fallback_enabled,
    evidence_toolchain,
)

#: ``(label, setup text)`` -- the board-level ``pad_to_mask_clearance`` variants.
CLEARANCES: tuple[tuple[str, str], ...] = (
    ("c=0", "(pad_to_mask_clearance 0)"),
    ("c=0.4", "(pad_to_mask_clearance 0.4)"),
    ("c=absent", ""),
)

#: The KiCad 10 board format every committed KiCad-10 board declares.
BOARD_VERSION = "20260206"

#: The rest of a pcbnew-written KiCad 10 ``setup`` block, for the second shape.
FULL_SETUP = """\
        (allow_soldermask_bridges_in_footprints no)
        (covering (front no) (back no))
        (plugging (front no) (back no))
        (capping no)
        (filling no)"""
BOARD_SHAPES: tuple[tuple[str, str], ...] = (
    ("minimal", ""),
    ("fullsetup", FULL_SETUP),
)

VIA_Y = 10.0
PAD_Y = 20.0

# (name, via size, x).  Untented through vias, nothing local.
VIAS: tuple[tuple[str, float, float], ...] = (
    ("via d0.6", 0.6, 10.0),
    ("via d1.0", 1.0, 20.0),
    ("via d1.6", 1.6, 30.0),
)

# (name, pad size, x, y, pad-local token).  Circular SMD pads: the control group.
PADS: tuple[tuple[str, float, float, float, str], ...] = (
    ("pad d1.0", 1.0, 10.0, PAD_Y, ""),
    ("pad d1.0 margin0.25", 1.0, 20.0, PAD_Y, "(solder_mask_margin 0.25)"),
    ("pad d1.0 margin0", 1.0, 30.0, PAD_Y, "(solder_mask_margin 0)"),
)

#: Via-local nodes the probe offers the parser.  The first is the pad-style mask
#: margin; the second is the per-via tenting node the repository's own fixtures
#: write (``tests/test_validate_silkscreen.py``).
VIA_LOCAL_NODES: tuple[str, ...] = (
    "(solder_mask_margin 0.25)",
    "(tenting (front no) (back no))",
)

#: Copper radius (mm) of each Part B feature, and the pitch between two probes.
FEATURE_RADII = {"via": 0.3, "via1.6": 0.8, "pad": 0.5}
_SWEEP_X0 = 10.0
_SWEEP_PITCH = 5.0

#: Silk line ``(length, stroke width)`` for the ring and edge controls.
THICK_LINE = (0.4, 0.2)
THIN_LINE = (0.2, 0.05)

#: ``(feature, distance of the line's centre from the feature centre, direction
#: of that offset in degrees (0 = right), line length, stroke width)``.
#:
#: The copper edge of ``via`` / ``via1.6`` / ``pad`` is 0.3 / 0.8 / 0.5 mm out
#: and the plotted opening's edge is that plus the board clearance.  The
#: ``via1.6`` line sits on the diagonal, outside a circular 1.2 mm opening but
#: inside the corners of its axis-aligned square.
_SPECS: tuple[tuple[str, float, float, float, float], ...] = (
    *(("via", distance, 0.0, *THICK_LINE) for distance in (0.0, 0.5, 1.0)),
    *(("pad", distance, 0.0, *THICK_LINE) for distance in (0.0, 0.7, 1.3)),
    ("via1.6", 1.4, 45.0, 0.6, 0.2),
    *(
        ("via", distance, 0.0, *THIN_LINE)
        for distance in (0.0, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60, 0.65, 0.75)
    ),
    *(
        ("pad", distance, 0.0, *THIN_LINE)
        for distance in (0.0, 0.45, 0.50, 0.55, 0.60, 0.70, 0.80, 0.95)
    ),
)

_BOARD = """\
(kicad_pcb
    (version {version})
    (generator "pcbnew")
    (setup
        {clearance}
        (tenting (front no) (back no))
{extra_setup}
    )
    (net 0 "")
    (net 1 "GND")
    (net 2 "SIG")
{vias}
{footprints}
{graphics}
    (gr_rect (start 0 0) (end {outline_end_x:g} 30)
        (stroke (width 0.1) (type solid))
        (fill no)
        (layer "Edge.Cuts") (uuid "9000"))
)
"""

_VIA = """\
    (via (at {x:g} {y:g}) (size {size:g}) (drill {drill:g}) (layers "F.Cu" "B.Cu")
        (net 1) {token} (uuid "{uuid}"))"""

_FOOTPRINT = """\
    (footprint "Probe:Pad" (layer "F.Cu") (at {x:g} {y:g}) (uuid "{uuid}")
        (attr smd)
        (fp_text reference "{ref}" (at 0 1.5) (layer "F.Fab") (uuid "{uuid}t"))
        (pad "1" smd circle (at 0 0) (size {size:g} {size:g})
            (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "SIG") {token} (uuid "{uuid}p"))
    )"""

_SILK_LINE = """\
    (gr_line (start {start[0]:g} {start[1]:g}) (end {end[0]:g} {end[1]:g})
        (stroke (width {width:g}) (type solid))
        (layer "F.SilkS") (uuid "{uuid}"))"""


def _slug(clabel: str) -> str:
    return clabel[1:].replace("=", "")


def plot_board_text(
    clearance: str, extra_setup: str, via_tokens: dict[int, str] | None = None
) -> str:
    vias = "\n".join(
        _VIA.format(
            x=x,
            y=VIA_Y,
            size=size,
            drill=round(size / 2.0, 3),
            token=(via_tokens or {}).get(index, ""),
            uuid=f"111{index + 1}",
        )
        for index, (_name, size, x) in enumerate(VIAS)
    )
    footprints = "\n".join(
        _FOOTPRINT.format(ref=name, x=x, y=y, size=size, token=token, uuid=f"211{index + 1}")
        for index, (name, size, x, y, token) in enumerate(PADS)
    )
    return _BOARD.format(
        version=BOARD_VERSION,
        clearance=clearance,
        extra_setup=extra_setup,
        vias=vias,
        footprints=footprints,
        graphics="",
        outline_end_x=60.0,
    )


def _tangential(
    x: float, y: float, distance: float, angle_deg: float, length: float
) -> tuple[tuple[float, float], tuple[float, float]]:
    """A segment of *length* centred *distance* from ``(x, y)`` toward
    *angle_deg*, perpendicular to that direction."""
    angle = math.radians(angle_deg)
    cx, cy = x + distance * math.cos(angle), y + distance * math.sin(angle)
    dx, dy = -math.sin(angle) * length / 2.0, math.cos(angle) * length / 2.0
    return (round(cx - dx, 4), round(cy - dy, 4)), (round(cx + dx, 4), round(cy + dy, 4))


@dataclass(frozen=True)
class SilkProbe:
    """One untented via or round SMD pad at ``(x, VIA_Y)`` and an F.SilkS line."""

    name: str
    feature: str
    x: float
    start: tuple[float, float]
    end: tuple[float, float]
    width: float
    thin: bool


def _reference(index: int) -> str:
    return f"PROBE{index + 1}"


def _silk_probes() -> tuple[SilkProbe, ...]:
    probes: list[SilkProbe] = []
    for index, (feature, distance, angle, length, width) in enumerate(_SPECS):
        x = _SWEEP_X0 + _SWEEP_PITCH * index
        weight = "thin" if (length, width) == THIN_LINE else "thick"
        where = "across centre" if distance == 0 else f"{distance:g} mm off centre"
        if angle:
            where += f" toward {angle:g} deg"
        start, end = _tangential(x, VIA_Y, distance, angle, length)
        probes.append(
            SilkProbe(
                name=f"{feature}, {weight} line {where}",
                feature=feature,
                x=x,
                start=start,
                end=end,
                width=width,
                thin=(length, width) == THIN_LINE,
            )
        )
    return tuple(probes)


SILK_PROBES: tuple[SilkProbe, ...] = _silk_probes()


def drc_board_text(clearance: str) -> str:
    vias = "\n".join(
        _VIA.format(
            x=p.x,
            y=VIA_Y,
            size=2 * FEATURE_RADII[p.feature],
            drill=FEATURE_RADII[p.feature],
            token="",
            uuid=f"311{index + 1}",
        )
        for index, p in enumerate(SILK_PROBES)
        if p.feature != "pad"
    )
    footprints = "\n".join(
        _FOOTPRINT.format(
            ref=_reference(index),
            x=p.x,
            y=VIA_Y,
            size=2 * FEATURE_RADII[p.feature],
            token="",
            uuid=f"211{index + 1}",
        )
        for index, p in enumerate(SILK_PROBES)
        if p.feature == "pad"
    )
    graphics = "\n".join(
        _SILK_LINE.format(start=p.start, end=p.end, width=p.width, uuid=f"411{index + 1}")
        for index, p in enumerate(SILK_PROBES)
    )
    return _BOARD.format(
        version=BOARD_VERSION,
        clearance=clearance,
        extra_setup="",
        vias=vias,
        footprints=footprints,
        graphics=graphics,
        outline_end_x=SILK_PROBES[-1].x + _SWEEP_PITCH,
    )


# --- Part A: plotted aperture diameters ---------------------------------------


@dataclass(frozen=True)
class Flash:
    aperture: int
    diameter: float
    ref: str
    x: float
    y: float


def _attrib(line: str) -> str:
    """``%TO.C,REF*%`` -> ``TO.C,REF``."""
    return line.strip()[1:].rstrip("%").rstrip("*")


def parse_flashes(text: str) -> list[Flash]:
    """Every flash in an X2 Gerber with its aperture diameter and attributes."""
    scale = 1e6
    apertures: dict[int, float] = {}
    ref = ""
    current: int | None = None
    flashes: list[Flash] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("%FSLAX"):
            m = re.search(r"X(\d)(\d)Y(\d)(\d)", line)
            if m:
                scale = 10.0 ** int(m.group(2))
        elif line.startswith("%ADD"):
            m = re.match(r"%ADD(\d+)([A-Za-z_]),([^*]+)\*%$", line)
            if m and m.group(2) == "C":
                apertures[int(m.group(1))] = float(m.group(3))
        elif line.startswith("%TO.C,"):
            ref = _attrib(line).split(",", 1)[1]
        elif line.startswith("%TD"):
            if _attrib(line)[2:] in ("", ".C"):
                ref = ""
        elif m := re.match(r"^D(\d+)\*$", line):
            current = int(m.group(1))
        elif m := re.match(r"^X(-?\d+)Y(-?\d+)D0?3\*$", line):
            assert current is not None, f"flash before any aperture selection: {line}"
            assert current in apertures, f"flash with undefined aperture D{current}"
            flashes.append(
                Flash(
                    aperture=current,
                    diameter=apertures[current],
                    ref=ref,
                    x=int(m.group(1)) / scale,
                    y=-int(m.group(2)) / scale,
                )
            )
    return flashes


def _nearest(x: float, y: float, placed: list[tuple[str, float, float]]) -> str | None:
    """The name of the probe feature within 0.01 mm of ``(x, y)``, if any."""
    name, dist = min(
        ((name, math.hypot(x - px, y - py)) for name, px, py in placed),
        key=lambda item: item[1],
    )
    return name if dist < 0.01 else None


def _plot_feature(flash: Flash) -> str:
    """The probe feature a plot flash sits on.

    KiCad attributes a component pad's flash to its reference designator
    (``%TO.C``) and leaves a via's unattributed, which independently confirms
    the mapping done by position.
    """
    placed = [(name, x, VIA_Y) for name, _size, x in VIAS]
    placed += [(name, x, y) for name, _size, x, y, _token in PADS]
    name = _nearest(flash.x, flash.y, placed)
    assert name is not None, f"flash at ({flash.x}, {flash.y}) matches no probe feature"
    expected_ref = name if name.startswith("pad") else ""
    assert flash.ref == expected_ref, f"{name}: TO.C {flash.ref!r}, expected {expected_ref!r}"
    return name


@contextmanager
def pinned_cli() -> Iterator[Any]:
    if not docker_fallback_enabled():
        sys.exit(f"set {KICAD_DOCKER_ENV}=1: the probe must run on the pinned image")
    with evidence_toolchain() as chain:
        print(f"kicad-cli {chain.version} ({chain.docker_image or chain.kicad_cli})")
        if chain.version != PINNED_KICAD_VERSION:
            sys.exit(f"not the pinned KiCad {PINNED_KICAD_VERSION}")
        yield chain


def _kicad_cli(*argv: str) -> tuple[bool, str]:
    """Run kicad-cli; ``(ok, diagnostic text)``."""
    proc = subprocess.run(["kicad-cli", *argv], capture_output=True, text=True, timeout=600)
    return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()


def _plot(board: Path, out: Path, layers: str) -> tuple[dict[str, str], str]:
    """``({file name: Gerber text}, diagnostic text)``; empty when the board
    does not load."""
    out.mkdir(parents=True, exist_ok=True)
    ok, note = _kicad_cli(
        "pcb", "export", "gerbers", "--layers", layers, "--output", str(out), str(board)
    )
    if not ok:
        return {}, note
    gerbers = {p.name: p.read_text() for p in sorted(out.iterdir()) if p.suffix != ".gbrjob"}
    stems = {Path(name).stem[-7:] for name in gerbers}
    assert stems == {f"-{layer.replace('.', '_')}" for layer in layers.split(",")}, (
        f"unexpected plot output in {out}: {sorted(gerbers)}"
    )
    return gerbers, note


def plot_masks(board: Path, out: Path, layers: str = "F.Mask,B.Mask") -> dict[str, str]:
    """``{file name: Gerber text}`` for the board's plotted mask layer(s)."""
    gerbers, note = _plot(board, out, layers)
    if not gerbers:
        sys.exit(f"kicad-cli could not plot {board.name}:\n{note}")
    return gerbers


def measure_plot(
    root: Path, shape: str, clabel: str, clearance: str, extra: str
) -> dict[str, float]:
    """``"feature [layer]" -> aperture diameter`` for one board variant."""
    board = root / f"{shape}_{_slug(clabel)}.kicad_pcb"
    board.write_text(plot_board_text(clearance, extra))
    print(f"\n== A: plot / {shape} setup / pad_to_mask_clearance {clabel[2:]} ==")
    diameters: dict[str, float] = {}
    for fname, text in plot_masks(board, root / board.stem).items():
        layer = "F.Mask" if "F_Mask" in fname else "B.Mask"
        for flash in parse_flashes(text):
            name = _plot_feature(flash)
            key = f"{name} [{layer}]"
            assert diameters.get(key, flash.diameter) == flash.diameter, f"{key}: two flashes"
            diameters[key] = flash.diameter
            print(f"  {layer}  {name:<24} TO.C={flash.ref or '-':<20} d={flash.diameter:.4f}")
    return diameters


def probe_via_local_nodes(root: Path) -> None:
    """Ask the parser whether a via accepts each local node."""
    print("\n== A: via-local nodes ==")
    for index, token in enumerate(VIA_LOCAL_NODES):
        board = root / f"vialocal_{index}.kicad_pcb"
        board.write_text(plot_board_text(CLEARANCES[0][1], "", via_tokens={0: token}))
        gerbers, note = _plot(board, root / board.stem, layers="F.Mask")
        if not gerbers:
            print(f"  {token}: REJECTED -- {note}")
            continue
        flashes = parse_flashes(next(iter(gerbers.values())))
        diameters = sorted({f.diameter for f in flashes if not f.ref})
        print(f"  {token}: ACCEPTED -- via openings on F.Mask have diameters {diameters}")


# --- Part B: native DRC against kct on the same boards ------------------------


def native_silk_over_copper(board: Path, out: Path) -> dict[str, int]:
    """``probe name -> native ``silk_over_copper`` findings on its feature``."""
    out.mkdir(parents=True, exist_ok=True)
    report = out / "drc.json"
    ok, note = _kicad_cli(
        "pcb", "drc", "--severity-all", "--format", "json", "--output", str(report), str(board)
    )
    if not ok or not report.exists():
        sys.exit(f"kicad-cli drc failed on {board.name}:\n{note}")
    data = json.loads(report.read_text())
    assert data.get("coordinate_units") == "mm", data.get("coordinate_units")
    assert not any(
        check.get("key") == "silk_over_copper" for check in data.get("ignored_checks", [])
    ), "native DRC skipped silk_over_copper"
    placed = [(p.name, p.x, VIA_Y) for p in SILK_PROBES]
    counts = {p.name: 0 for p in SILK_PROBES}
    for violation in data.get("violations", []):
        if violation.get("type") != "silk_over_copper":
            continue
        owners = set()
        for item in violation.get("items", []):
            pos = item.get("pos") or {}
            if "x" in pos and "y" in pos:
                # The feature item sits on a probe centre; the silk item does not.
                owner = _nearest(float(pos["x"]), float(pos["y"]), placed)
                if owner is not None:
                    owners.add(owner)
        assert len(owners) == 1, f"finding spans several probes: {sorted(owners)}"
        counts[owners.pop()] += 1
    return counts


def kct_silk_over_copper(board: Path) -> dict[str, int]:
    """``probe name -> kct ``silk_over_copper`` findings on its feature``."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(board),
            "--mfr",
            "jlcpcb",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    # The probe deliberately violates other manufacturing rules.
    if proc.returncode not in (0, 2):
        raise RuntimeError(proc.stderr)
    report = json.loads(proc.stdout)
    labels = {
        f"{_reference(i)} pad 1"
        if p.feature == "pad"
        else f"via [GND] at ({p.x:.3f}, {VIA_Y:.3f})": p.name
        for i, p in enumerate(SILK_PROBES)
    }
    counts = {p.name: 0 for p in SILK_PROBES}
    for violation in report["violations"]:
        if violation["rule_id"] != "silk_over_copper":
            continue
        name = labels[violation["items"][1]]
        counts[name] += 1
    return counts


def measure_drc(root: Path) -> dict[str, dict[str, tuple[int, int]]]:
    """``clearance label -> {probe: (native count, kct count)}``."""
    results: dict[str, dict[str, tuple[int, int]]] = {}
    for clabel, clearance in CLEARANCES:
        board = root / f"drc_{_slug(clabel)}.kicad_pcb"
        board.write_text(drc_board_text(clearance))
        native = native_silk_over_copper(board, root / board.stem)
        kct = kct_silk_over_copper(board)
        results[clabel] = {p.name: (native[p.name], kct[p.name]) for p in SILK_PROBES}
    return results


def report_drc(drc: dict[str, dict[str, tuple[int, int]]]) -> None:
    print("\n== B: silk_over_copper findings by pad_to_mask_clearance ==")
    for thin, (length, width) in ((False, THICK_LINE), (True, THIN_LINE)):
        print(f"\n  {length:g} x {width:g} mm lines -- native / kct")
        for probe in SILK_PROBES:
            if probe.thin is not thin:
                continue
            cells = []
            for clabel, _clearance in CLEARANCES:
                native, kct = drc[clabel][probe.name]
                cells.append(f"{clabel:>8}: {native}/{kct}")
            print(f"  {probe.name:<34} " + "   ".join(cells))


def main() -> int:
    measured: dict[str, dict[str, float]] = {}
    with tempfile.TemporaryDirectory(prefix="kct-10384-") as tmp, pinned_cli():
        root = Path(tmp)
        for shape, extra in BOARD_SHAPES:
            for clabel, clearance in CLEARANCES:
                measured[f"{shape} {clabel}"] = measure_plot(root, shape, clabel, clearance, extra)
        probe_via_local_nodes(root)
        drc = measure_drc(root)

    print("\n== A: aperture diameter (mm) by pad_to_mask_clearance ==")
    for shape, _extra in BOARD_SHAPES:
        rows = [(name, layer) for name, *_ in VIAS for layer in ("F.Mask", "B.Mask")]
        rows += [(name, "F.Mask") for name, *_ in PADS]
        for name, layer in rows:
            cells = [
                f"{clabel:>8}: {measured[f'{shape} {clabel}'][f'{name} [{layer}]']:.3f}"
                for clabel, _clearance in CLEARANCES
            ]
            print(f"  {shape:<10} {name:<24} {layer}  " + "   ".join(cells))

    report_drc(drc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
