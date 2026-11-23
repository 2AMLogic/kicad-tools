"""Board 05's U2/U3 escapes: the pour-slot recipe state and its physical criterion (Issue #9921).

The ``pour_slot`` advisory originally reported 20 witnesses -- nineteen U2
lead-escape pairs and the U3 GND/RESET pair -- from adjacent pad-row escapes
running parallel with 0.49 mm (0.16 mm tracks) or 0.25 mm (0.4 mm tracks)
gaps inside the ``2 x clearance + min_thickness`` (0.7 mm) band.

The recipe repairs eleven of them using horizontal, vertical and 45-degree
copper only (no off-angle exemption: the fleet 45-degree criterion stays
intact).  Five signal escapes are widened/repacked so their copper-to-copper
gap drops below the 0.400 mm lower edge of the band the front GND pour cannot
fill, and the two redundant GND escapes the attaching pour replaces are
removed.  **Nine advisories remain by design**: seven involve the
power/GND_SENSE bank or its IREF neighbour, and two (IREF/SENSE_IN,
EN_A/PWM_A) lie wholly within the stock U2 lead length, where the pad edges
set the gap and need a ground-plane or pad-edge criterion before they count as
repaired.  These tests pin that state -- exactly those nine pairs, and the
0.001 mm cross-section criterion across the specified channel windows --
rather than a quiet detector or a global minimum distance that a partially
reopened window can still satisfy.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_file, parse_string, serialize_sexp

REPO_ROOT = Path(__file__).resolve().parent.parent
REDESIGN = REPO_ROOT / "boards/05-bldc-motor-controller/redesign"

#: Board 05's front GND zone has 0.200 mm clearance and 0.300 mm min_thickness,
#: so an unpoured gap in [0.400, 0.700] mm separates the clearance halos while
#: leaving an island narrower than min_thickness that KiCad drops.
POUR_VOID_BAND_MM = (0.400, 0.700)
WITNESS_X_STEP_MM = 0.001

#: The two operator-specified front-copper witness rectangles beyond U2's right
#: pad tips (X = 142.150 mm): (upper_net, lower_net, x_start, x_end, y_bounds).
WITNESS_CHANNEL_WINDOWS = (
    ("EN_C", "PWM_C", 142.180, 142.340, (98.400, 100.200)),
    ("EN_B", "PWM_B", 142.180, 142.340, (97.100, 98.900)),
)

#: Sibling U2 escape channels bounded by the five repacked 45-degree escapes
#: (DRIVER_SLEEP, nTRIP, PWM_C, PWM_B on the right column; VCP on the left
#: column, symmetric 0.030-0.190 mm beyond the X = 134.850 mm left pad tips).
OTHER_REPACKED_ESCAPE_WINDOWS = (
    ("+5V", "DRIVER_SLEEP", 142.180, 142.340, (102.300, 104.100)),
    ("DRIVER_SLEEP", "nFAULT", 142.180, 142.340, (101.700, 103.500)),
    ("nFAULT", "nTRIP", 142.180, 142.340, (101.000, 102.800)),
    ("PWM_C", "EN_B", 142.180, 142.340, (97.800, 99.600)),
    ("PWM_B", "EN_A", 142.180, 142.340, (96.500, 98.300)),
    ("VM", "VCP", 134.660, 134.820, (96.500, 98.300)),
    ("VCP", "CPH", 134.660, 134.820, (95.800, 97.600)),
)


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"board05_deslot_{name}", REDESIGN / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _replayed_board(tmp_path: Path) -> Path:
    hardware = load("hardware")
    routing = load("routing")
    path = tmp_path / "board.kicad_pcb"
    hardware.build_pcb().save(path)
    routing.apply_routing(path)
    return path


def _apply_recipe_with_edits(
    unrouted_path: Path, out_path: Path, edits: list[tuple[str, str, str]]
) -> Path:
    """Replay ``routing.json`` onto a copy of ``unrouted_path`` with targeted edits.

    Each edit is ``(uuid, old_fragment, new_fragment)`` and must match exactly
    one copper item so a stale control fails loudly.
    """
    recipe = json.loads((REDESIGN / "routing.json").read_text())
    board = parse_file(unrouted_path)
    nets: dict[str, int] = {
        n: net_id
        for node in board.find_all("net")
        if (n := node.get_string(1)) is not None and (net_id := node.get_int(0)) is not None
    }
    counts = [0] * len(edits)
    for source in recipe["copper"]:
        for idx, (uuid, old_frag, new_frag) in enumerate(edits):
            if uuid in source:
                assert old_frag in source, (uuid, old_frag, source)
                source = source.replace(old_frag, new_frag, 1)
                counts[idx] += 1
        node = parse_string(source)
        net = node.find("net")
        if net is not None:
            name = net.get_string(0)
            assert name is not None
            net.set_value(0, nets.get(name, 0))
            if node.name == "zone":
                old_nn = node.find("net_name")
                if old_nn is not None:
                    old_nn.set_value(0, name)
                else:
                    node.append(parse_string(f"(net_name {json.dumps(name)})"))
        board.append(node)
    assert all(c == 1 for c in counts), counts
    out_path.write_text(serialize_sexp(board))
    return out_path


@pytest.fixture(scope="module")
def replayed_pcb(tmp_path_factory: pytest.TempPathFactory) -> PCB:
    return PCB.load(_replayed_board(tmp_path_factory.mktemp("board05_deslot")))


#: The nine pour-slot advisories that remain open by design (module docstring):
#: sorted (net, net) pairs.  A change here is a recipe change -- update the
#: docstring and the #9921 evidence with it.
BOARD05_OPEN_POUR_SLOT_PAIRS = sorted(
    [
        ("EN_A", "PWM_A"),
        ("PHASE_A", "VM"),
        ("GND_SENSE", "PHASE_A"),
        ("GND_SENSE", "PHASE_B"),
        ("GND_SENSE", "PHASE_C"),
        ("PHASE_B", "PHASE_C"),
        ("GND_SENSE", "VM"),
        ("IREF", "SENSE_IN"),
        ("IREF", "VM"),
    ]
)


def _y_interval_circle(cx: float, cy: float, radius: float, x: float) -> tuple[float, float] | None:
    dx = abs(x - cx)
    if dx > radius:
        return None
    dy = math.sqrt(max(0.0, radius * radius - dx * dx))
    return (cy - dy, cy + dy)


def _y_interval_capsule(
    x1: float, y1: float, x2: float, y2: float, radius: float, x: float
) -> tuple[float, float] | None:
    """Vertical Y span where line ``X = x`` meets a rounded-cap track segment."""
    spans: list[tuple[float, float]] = []
    for cx, cy in ((x1, y1), (x2, y2)):
        cap = _y_interval_circle(cx, cy, radius, x)
        if cap is not None:
            spans.append(cap)
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length > 1e-12:
        nx, ny = -dy / length, dx / length
        poly = (
            (x1 + radius * nx, y1 + radius * ny),
            (x2 + radius * nx, y2 + radius * ny),
            (x2 - radius * nx, y2 - radius * ny),
            (x1 - radius * nx, y1 - radius * ny),
        )
        ys: list[float] = []
        for i in range(4):
            ax, ay = poly[i]
            bx, by = poly[(i + 1) % 4]
            if abs(ax - x) <= 1e-12:
                ys.append(ay)
            if (ax - x) * (bx - x) < 0.0:
                t = (x - ax) / (bx - ax)
                ys.append(ay + t * (by - ay))
        if ys:
            spans.append((min(ys), max(ys)))
    if not spans:
        return None
    return (min(lo for lo, _ in spans), max(hi for _, hi in spans))


def _y_interval_roundrect(
    cx: float, cy: float, width: float, height: float, rratio: float, x: float
) -> tuple[float, float] | None:
    hw, hh = width / 2.0, height / 2.0
    dx = abs(x - cx)
    if dx > hw:
        return None
    corner_r = min(width, height) * rratio
    if dx <= hw - corner_r:
        return (cy - hh, cy + hh)
    rem = dx - (hw - corner_r)
    dy_corner = math.sqrt(max(0.0, corner_r * corner_r - rem * rem))
    half_y = hh - corner_r + dy_corner
    return (cy - half_y, cy + half_y)


def _collect_fcu_shapes(pcb: PCB, net_name: str) -> list[tuple]:
    """Return sheet-coordinate F.Cu copper primitives (tracks, vias, pads) for ``net_name``."""
    ox, oy = pcb.board_origin
    shapes: list[tuple] = []
    for seg in pcb.segments:
        if seg.layer == "F.Cu" and seg.net_name == net_name:
            shapes.append(
                (
                    "cap",
                    seg.start[0] + ox,
                    seg.start[1] + oy,
                    seg.end[0] + ox,
                    seg.end[1] + oy,
                    seg.width / 2.0,
                )
            )
    for via in pcb.vias:
        if via.net_name == net_name and "F.Cu" in via.layers:
            shapes.append(("cir", via.position[0] + ox, via.position[1] + oy, via.size / 2.0))
    for fp in pcb.footprints:
        fx, fy = fp.position[0] + ox, fp.position[1] + oy
        for pad in fp.pads:
            if pad.net_name == net_name and ("F.Cu" in pad.layers or "*.Cu" in pad.layers):
                rot = (fp.rotation + pad.rotation) % 360.0
                w, h = (pad.size[1], pad.size[0]) if rot in (90.0, 270.0) else pad.size
                if pad.shape in ("circle", "oval"):
                    rratio = 0.5
                elif pad.shape == "roundrect":
                    rratio = getattr(pad, "roundrect_rratio", 0.25) or 0.25
                else:
                    rratio = 0.0
                shapes.append(("rr", fx + pad.position[0], fy + pad.position[1], w, h, rratio))
    return shapes


def _eval_shape_y(shape: tuple, x: float) -> tuple[float, float] | None:
    kind = shape[0]
    if kind == "cap":
        return _y_interval_capsule(shape[1], shape[2], shape[3], shape[4], shape[5], x)
    if kind == "cir":
        return _y_interval_circle(shape[1], shape[2], shape[3], x)
    return _y_interval_roundrect(shape[1], shape[2], shape[3], shape[4], shape[5], x)


def channel_window_sections(
    pcb: PCB,
    upper_net: str,
    lower_net: str,
    x_start_mm: float,
    x_end_mm: float,
    y_bounds_mm: tuple[float, float],
    *,
    require_full_span: bool = True,
) -> list[tuple[float, float]]:
    """Sample vertical F.Cu edge-to-edge gaps at 0.001 mm intervals across a channel window."""
    upper_shapes = _collect_fcu_shapes(pcb, upper_net)
    lower_shapes = _collect_fcu_shapes(pcb, lower_net)
    y_lo, y_hi = y_bounds_mm
    n_steps = round((x_end_mm - x_start_mm) / WITNESS_X_STEP_MM) + 1
    sections: list[tuple[float, float]] = []
    for i in range(n_steps):
        x = round(x_start_mm + i * WITNESS_X_STEP_MM, 6)
        upper_ivs = [
            iv
            for sh in upper_shapes
            if (iv := _eval_shape_y(sh, x)) is not None and iv[1] >= y_lo and iv[0] <= y_hi
        ]
        lower_ivs = [
            iv
            for sh in lower_shapes
            if (iv := _eval_shape_y(sh, x)) is not None and iv[1] >= y_lo and iv[0] <= y_hi
        ]
        if not upper_ivs or not lower_ivs:
            assert not require_full_span, (
                f"{lower_net}/{upper_net} missing F.Cu copper at X = {x:.3f} mm"
            )
            continue
        gap = min(lo for lo, _ in upper_ivs) - max(hi for _, hi in lower_ivs)
        sections.append((x, gap))
    return sections


def assert_channel_windows_clear_of_void_band(pcb: PCB) -> None:
    """Verify every 0.001 mm cross-section in both witness rectangles and sibling escapes."""
    band_lo, band_hi = POUR_VOID_BAND_MM
    for upper, lower, x0, x1, y_bounds in WITNESS_CHANNEL_WINDOWS:
        sections = channel_window_sections(
            pcb, upper, lower, x0, x1, y_bounds, require_full_span=True
        )
        assert len(sections) == 161, (lower, upper, len(sections))
        assert all(gap > 0.0 for _, gap in sections), f"{lower}/{upper} copper overlaps"
        reopened = [(x, gap) for x, gap in sections if band_lo <= gap <= band_hi]
        assert not reopened, (
            f"{lower}/{upper} reopens the {band_lo:.3f}-{band_hi:.3f} mm void band on "
            f"{len(reopened)}/{len(sections)} sections "
            f"(X = {reopened[0][0]:.3f}..{reopened[-1][0]:.3f} mm, "
            f"max gap {max(g for _, g in reopened):.6f} mm)"
        )
        assert all(gap < band_lo for _, gap in sections), (
            f"{lower}/{upper} exceeds {band_lo:.3f} mm inside X = {x0:.3f}..{x1:.3f} mm"
        )

    for upper, lower, x0, x1, y_bounds in OTHER_REPACKED_ESCAPE_WINDOWS:
        sections = channel_window_sections(
            pcb, upper, lower, x0, x1, y_bounds, require_full_span=False
        )
        assert all(gap > 0.0 for _, gap in sections), f"{lower}/{upper} copper overlaps"
        reopened = [(x, gap) for x, gap in sections if band_lo <= gap <= band_hi]
        assert not reopened, (
            f"{lower}/{upper} reopens the {band_lo:.3f}-{band_hi:.3f} mm void band on "
            f"{len(reopened)}/{len(sections)} sections "
            f"(X = {reopened[0][0]:.3f}..{reopened[-1][0]:.3f} mm)"
        )


def test_replayed_copper_pins_the_open_pour_slot_pairs(replayed_pcb: PCB) -> None:
    from kicad_tools.validate.rules.pour_slot import iter_pour_slot_pairs

    pairs = list(iter_pour_slot_pairs(replayed_pcb))
    found = sorted(tuple(sorted((p.first.net_name, p.second.net_name))) for p in pairs)
    assert found == BOARD05_OPEN_POUR_SLOT_PAIRS, (
        "Board 05's pour-slot advisory set changed: expected the nine open"
        " pairs documented for #9921, got " + repr(found)
    )


def test_witness_channels_leave_no_pour_void_band(replayed_pcb: PCB) -> None:
    """Every 0.001 mm cross-section across both specified rectangles stays out of the void band.

    Calibrated directly against the pinned KiCad 10.0.6 native F.Cu Gerber film
    across sheet X = 142.180-142.340 mm (161 vertical sections each):
    PWM_C/EN_C spans 0.243601-0.388081 mm and PWM_B/EN_B spans
    0.264234-0.397010 mm, with 0 / 161 sections in [0.400, 0.700] mm.
    """
    assert_channel_windows_clear_of_void_band(replayed_pcb)

    pwm_c_sections = channel_window_sections(
        replayed_pcb, "EN_C", "PWM_C", 142.180, 142.340, (98.400, 100.200)
    )
    pwm_c_gaps = [gap for _, gap in pwm_c_sections]
    assert min(pwm_c_gaps) == pytest.approx(0.243601, abs=5e-6)
    assert max(pwm_c_gaps) == pytest.approx(0.388081, abs=5e-6)

    pwm_b_sections = channel_window_sections(
        replayed_pcb, "EN_B", "PWM_B", 142.180, 142.340, (97.100, 98.900)
    )
    pwm_b_gaps = [gap for _, gap in pwm_b_sections]
    assert min(pwm_b_gaps) == pytest.approx(0.264234, abs=5e-6)
    assert max(pwm_b_gaps) == pytest.approx(0.397010, abs=5e-6)


def test_witness_channel_guard_rejects_partially_reopened_window(tmp_path: Path) -> None:
    """A single width edit that partially reopens a window fails even when min < 0.400 mm.

    Pins the reviewed one-width red control (PWM_B segment ``0a8a617a`` narrowed
    from 0.28 to 0.18 mm, which reopens 67 / 161 sections across
    X = 142.263-142.329 mm on the native F.Cu film while keeping the 9
    pour-slot advisories, 0 off-angle segments, and a 0.320167 mm window
    minimum) as well as the analogous partial-reopen controls on PWM_C and the
    three sibling repacked U2 escapes.
    """
    from kicad_tools.router.quantize import segment_angle_census
    from kicad_tools.validate.rules.pour_slot import iter_pour_slot_pairs

    unrouted = tmp_path / "unrouted.kicad_pcb"
    load("hardware").build_pcb().save(unrouted)

    # 1. Exact native-film-calibrated red control on PWM_B/EN_B (0.28 -> 0.18 mm).
    pwm_b_ctrl = _apply_recipe_with_edits(
        unrouted,
        tmp_path / "ctrl_pwm_b.kicad_pcb",
        [("0a8a617a-8b1b-40e0-8926-0e2a10919dc5", "(width 0.28)", "(width 0.18)")],
    )
    pwm_b_pcb = PCB.load(pwm_b_ctrl)
    found_pairs = sorted(
        tuple(sorted((p.first.net_name, p.second.net_name)))
        for p in iter_pour_slot_pairs(pwm_b_pcb)
    )
    assert found_pairs == BOARD05_OPEN_POUR_SLOT_PAIRS
    assert not segment_angle_census(pwm_b_ctrl)[1]

    sections = channel_window_sections(
        pwm_b_pcb, "EN_B", "PWM_B", 142.180, 142.340, (97.100, 98.900)
    )
    gaps = [gap for _, gap in sections]
    reopened = [x for x, gap in sections if 0.400 <= gap <= 0.700]
    assert min(gaps) == pytest.approx(0.320167, abs=5e-6)  # min inside window still < 0.400
    assert max(gaps) == pytest.approx(0.457721, abs=5e-6)
    assert len(reopened) == 67
    assert (reopened[0], reopened[-1]) == (142.263, 142.329)

    with pytest.raises(
        AssertionError, match=r"PWM_B/EN_B.*67/161 sections \(X = 142\.263\.\.142\.329 mm"
    ):
        assert_channel_windows_clear_of_void_band(pwm_b_pcb)

    # 2. Symmetric partial-reopen controls on PWM_C/EN_C and the three sibling escapes.
    sibling_controls = (
        (
            "74e079c0-522a-464b-a0a7-462a94741a0d",
            "(width 0.27)",
            "(width 0.17)",
            r"PWM_C/EN_C.*59/161",
        ),
        (
            "d3667215-43af-44a0-a0fe-b5a4ce212e71",
            "(width 0.28)",
            "(width 0.18)",
            r"DRIVER_SLEEP/\+5V.*67/161",
        ),
        (
            "220723fc-b9db-4d89-81b4-07f27bcc0a6f",
            "(width 0.28)",
            "(width 0.18)",
            r"nTRIP/nFAULT.*67/161",
        ),
        ("c3f3a1b7-3e5f-4b5c-8897-a61280adf527", "(width 0.27)", "(width 0.17)", r"VCP/VM"),
    )
    for idx, (uuid, old_w, new_w, expected_msg) in enumerate(sibling_controls):
        ctrl_path = _apply_recipe_with_edits(
            unrouted, tmp_path / f"ctrl_{idx}.kicad_pcb", [(uuid, old_w, new_w)]
        )
        ctrl_pcb = PCB.load(ctrl_path)
        ctrl_pairs = sorted(
            tuple(sorted((p.first.net_name, p.second.net_name)))
            for p in iter_pour_slot_pairs(ctrl_pcb)
        )
        assert ctrl_pairs == BOARD05_OPEN_POUR_SLOT_PAIRS
        assert not segment_angle_census(ctrl_path)[1]
        with pytest.raises(AssertionError, match=expected_msg):
            assert_channel_windows_clear_of_void_band(ctrl_pcb)


def test_pour_replaced_gnd_escapes_stay_gone() -> None:
    recipe = (REDESIGN / "routing.json").read_text()
    # U2 pad 20 (right column) and U3 pad 4 (left column) escape stubs: the
    # front GND pour attaches both pads and reaches their stitch vias, so the
    # parallel stubs were removed rather than sloped.
    assert "2aa4f094-cc7c-4416-bfe5-f9277df1eaec" not in recipe
    assert "e5531a28-a0e7-5f32-ab2f-ba3f2c843b86" not in recipe
    assert "908df261-a3db-4df7-87a2-e15ddbd29a2e" not in recipe
    # Their stitch vias remain (the pour still ties the layers there).
    assert "1eeefbf1-a3cd-4bd8-98d0-0e08f226e4db" in recipe
    assert "dbc23801-258b-4620-9555-825f028d7570" in recipe
