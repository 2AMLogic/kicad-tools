"""Regression test for ``boards/00-simple-led/`` power-net stranding.

Issue #3148 — Board 00 (the "Hello World" LED board) routed only the
single signal net ``LED_ANODE`` and left both power nets (``VCC`` and
``GND``) stranded, producing 2 connectivity DRC errors.

The board defines **3 nets**: ``VCC``, ``LED_ANODE``, ``GND``.  ``VCC``
and ``GND`` are power/pour nets (``is_pour_net=True`` in
``DEFAULT_NET_CLASS_MAP``) that are meant to be connected via copper
zones, not traces.  The router correctly auto-skips them, but the
board's hand-rolled ``route_pcb()`` never created copper pours -- so
both power nets ended up with neither a trace nor a zone and DRC
reported "1 of 2 pads stranded" for each.

This was NOT a router regression (it reproduced on commits predating
all suspected router PRs).  The fix routes board 00 through the same
pour-aware path the official ``kct route`` CLI uses:
``auto_pour_if_missing()`` creates the VCC/GND zone outlines and
``_fill_zones_after_route()`` fills them.

This test pins the post-fix behavior by running the generator
end-to-end and asserting:

- The routable signal net ``LED_ANODE`` is routed (>= 1 segment).
- The routed PCB has at least 2 filled copper zones (VCC + GND).
- ``kct check`` reports 0 connectivity errors (no stranded pads).

It FAILS against the unpatched ``route_pcb()`` (0 zones, 2 connectivity
errors) and PASSES after the fix.

Requires ``kicad-cli`` for ERC + zone fill; skipped otherwise.  Marked
``@pytest.mark.slow`` (full generate + route is ~15-20s).  The nightly
slow-tests workflow (``.github/workflows/slow-tests.yml``, ``-m slow``)
picks this up; PR-time CI excludes it.
"""

from __future__ import annotations

import json
import re
import runpy
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "00-simple-led"
GENERATOR = BOARD_DIR / "generate_design.py"

# Net 2 is the only routable signal net on board 00 (R1.2 -> D1.1).
# Nets 1 (VCC) and 3 (GND) are pour nets connected via copper zones.
LED_ANODE_NET_NUM = 2
LED_ANODE_NET_NAME = "LED_ANODE"


def _kicad_cli_available() -> bool:
    """Whether kicad-cli is installed (needed for ERC + zone fill)."""
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli() is not None


def _count_filled_zones(pcb_text: str) -> int:
    """Count ``(zone ...)`` blocks that contain a ``filled_polygon``.

    A zone with ``fill enabled`` but no ``filled_polygon`` is an empty
    outline that contributes no plane copper -- it does NOT connect the
    net.  We only count zones that were actually filled.
    """
    # Split on zone openings (both single-line and newline-wrapped forms).
    chunks = re.split(r"\(zone[\s\(]", pcb_text)
    return sum(1 for chunk in chunks[1:] if "filled_polygon" in chunk)


def _count_segments_on_net(pcb_text: str, net_num: int) -> int:
    """Count routed copper segments belonging to ``net_num``.

    Segments serialize as ``(segment ... (net N) ...)``.  We match the
    ``(net N)`` reference form used inside segment/via blocks (distinct
    from the ``(net N "name")`` header declarations).
    """
    return len(re.findall(rf"\(segment\b[^()]*?(?:\([^)]*\)[^()]*?)*\(net {net_num}\)", pcb_text))


def _connectivity_error_count(check_stdout: str) -> int:
    """Count connectivity DRC errors in ``kct check`` output.

    The pre-fix failure mode prints lines like::

        [X] connectivity
            Net 'GND' is partially routed: 1 of 2 pads stranded
    """
    return len(
        re.findall(
            r"\bpartially routed\b|\bpads stranded\b|\bstranded\b",
            check_stdout,
        )
    )


class TestBoard00D1Polarity:
    """Regression test for #3747: schematic↔PCB D1 polarity disagreement.

    The schematic generator originally wired R1.2 to D1 pin 1 (cathode, K)
    and D1 pin 2 (anode, A) to GND, leaving the LED reverse-biased per the
    schematic. The PCB generator at ``generate_led("D1", ..., "LED_ANODE",
    "GND")`` already binds pad 1 → GND and pad 2 → LED_ANODE (forward
    biased). The two halves of the recipe disagreed.

    This test runs the schematic and PCB generation phases and asserts that:

    - Schematic: D1 pin 1 (K) is on net ``GND``; pin 2 (A) is on net
      ``LED_ANODE``.
    - PCB: pad 1 is on net ``GND`` (net 3); pad 2 is on net ``LED_ANODE``
      (net 2).

    Routing and manufacturing export do not determine these source pin
    bindings. The end-to-end routing tests below and the Board00 CI job
    exercise those phases separately.
    """

    @pytest.fixture(scope="class")
    def generated_design(self, tmp_path_factory: pytest.TempPathFactory) -> Path:
        """Generate the actual source artifacts whose pin bindings we check."""
        out_dir = tmp_path_factory.mktemp("board00_polarity")
        generator = runpy.run_path(str(GENERATOR))
        generator["create_project"](out_dir, "simple_led")
        generator["create_led_schematic"](out_dir)
        generator["create_led_pcb"](out_dir)
        return out_dir

    def test_schematic_d1_cathode_on_gnd(self, generated_design: Path) -> None:
        """D1 pin 1 (K, cathode) must be on the GND net in the schematic."""
        from kicad_tools.schematic.models.schematic import Schematic

        sch = Schematic.load(generated_design / "simple_led.kicad_sch")
        net = sch.get_net_for_pin("D1", "1")
        assert net == "GND", (
            f"D1 pin 1 (cathode K) is on net {net!r}, expected 'GND'. "
            "The schematic generator wired the LED reverse-biased "
            "(see #3747)."
        )

    def test_schematic_d1_anode_on_led_anode(self, generated_design: Path) -> None:
        """D1 pin 2 (A, anode) must be on the LED_ANODE net in the schematic."""
        from kicad_tools.schematic.models.schematic import Schematic

        sch = Schematic.load(generated_design / "simple_led.kicad_sch")
        net = sch.get_net_for_pin("D1", "2")
        assert net == "LED_ANODE", (
            f"D1 pin 2 (anode A) is on net {net!r}, expected 'LED_ANODE'. "
            "The schematic generator wired the LED reverse-biased "
            "(see #3747)."
        )

    def test_pcb_d1_pad_to_net_mapping(self, generated_design: Path) -> None:
        """The PCB must agree with the schematic on D1's pin-to-net mapping.

        ``generate_led("D1", D1_POS, "LED_ANODE", "GND")`` binds pad 1 to
        GND (net 3) and pad 2 to LED_ANODE (net 2). The schematic must
        match this mapping for LVS sanity.
        """
        pcb_text = (generated_design / "simple_led.kicad_pcb").read_text()
        # Net header declarations (id-to-name binding):
        assert '(net 2 "LED_ANODE")' in pcb_text, (
            "PCB net 2 is not LED_ANODE; net numbering changed."
        )
        assert '(net 3 "GND")' in pcb_text, "PCB net 3 is not GND; net numbering changed."
        # Locate D1's footprint and verify each pad's net. The KiCad
        # serializer uses either ``(fp_text reference "D1" ...)`` (the form
        # this repo's PCB generator emits) or ``(property "Reference" "D1"
        # ...)`` (KiCad 8 stable form); accept either.
        d1_marker = re.search(
            r'\(fp_text\s+reference\s+"D1"|\(property\s+"Reference"\s+"D1"',
            pcb_text,
        )
        assert d1_marker is not None, "Could not find D1 footprint block in PCB."
        # Walk back to the enclosing ``(footprint`` opening, then bound the
        # block to its matching close paren so the pad search is scoped to D1.
        start = pcb_text.rfind("(footprint", 0, d1_marker.start())
        assert start >= 0, "Could not find enclosing (footprint for D1."
        depth = 0
        end = start
        for i in range(start, len(pcb_text)):
            ch = pcb_text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        d1_block = pcb_text[start:end]
        # Match each pad and its (net N "NAME") binding.
        pad_to_net: dict[str, tuple[str, str]] = {
            m.group(1): (m.group(2), m.group(3))
            for m in re.finditer(
                r'\(pad\s+"(\d+)"[^()]*(?:\([^()]*\)[^()]*)*?\(net\s+(\d+)\s+"([^"]+)"\)',
                d1_block,
            )
        }
        pad1 = pad_to_net.get("1")
        pad2 = pad_to_net.get("2")
        assert pad1 is not None and pad1[1] == "GND", (
            f"D1 pad 1 is on net {pad1!r}, expected GND. "
            "PCB net assignment changed; the schematic↔PCB agreement (#3747) "
            "now needs re-verification."
        )
        assert pad2 is not None and pad2[1] == "LED_ANODE", (
            f"D1 pad 2 is on net {pad2!r}, expected LED_ANODE."
        )


@pytest.mark.slow
@pytest.mark.skipif(
    not _kicad_cli_available(),
    reason="kicad-cli not installed (required for ERC + zone fill)",
)
class TestBoard00SimpleLed:
    """Pin board 00's pour-aware routing against the #3148 fix.

    Runs the generator script as a subprocess (the same path the fleet
    audit / a developer invokes), then inspects the routed artifact and
    runs ``kct check`` on it.  The fixture runs once per class; each test
    asserts a distinct aspect for sharp failure attribution.
    """

    @pytest.fixture(scope="class")
    def routed_pcb(self, tmp_path_factory: pytest.TempPathFactory) -> Path:
        """Run ``generate_design.py`` and return the routed PCB path."""
        out_dir = tmp_path_factory.mktemp("board00")
        proc = subprocess.run(
            [sys.executable, str(GENERATOR), str(out_dir)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        routed = out_dir / "simple_led_routed.kicad_pcb"
        if not routed.exists():
            pytest.fail(
                f"Generator did not produce {routed.name} "
                f"(exit {proc.returncode}).\n"
                f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}\n"
                f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}"
            )
        # The generator's exit gate is ``0 if erc_success and drc_success``.
        # Post-fix, DRC must pass, so a non-zero exit is itself a regression.
        assert proc.returncode == 0, (
            f"generate_design.py exited {proc.returncode} (expected 0; "
            "ERC + DRC must both pass after the #3148 pour fix).\n"
            f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
        )
        return routed

    def test_led_anode_routed(self, routed_pcb: Path) -> None:
        """The only routable signal net (LED_ANODE) must have a trace."""
        text = routed_pcb.read_text()
        # Sanity: confirm the net numbering matches our assumption.
        assert f'(net {LED_ANODE_NET_NUM} "{LED_ANODE_NET_NAME}")' in text, (
            f"Expected net {LED_ANODE_NET_NUM} to be '{LED_ANODE_NET_NAME}' "
            "in the routed PCB header; the board's net map may have changed. "
            "Update LED_ANODE_NET_NUM in this test if so."
        )
        segments = _count_segments_on_net(text, LED_ANODE_NET_NUM)
        assert segments >= 1, (
            f"LED_ANODE (net {LED_ANODE_NET_NUM}) has no routed segments. "
            "The only routable signal net on board 00 was not routed."
        )

    def test_power_nets_have_filled_zones(self, routed_pcb: Path) -> None:
        """VCC + GND must be connected via filled copper zones.

        Pre-fix, ``route_pcb()`` created zero zones (it never called
        ``auto_pour_if_missing()``), so this asserts the fix wired the
        pour step in.  Empty zone outlines (fill enabled, no
        ``filled_polygon``) do NOT count -- they carry no copper.
        """
        text = routed_pcb.read_text()
        filled = _count_filled_zones(text)
        assert filled >= 2, (
            f"Routed PCB has only {filled} filled copper zone(s); expected "
            ">= 2 (one VCC, one GND).  This is the #3148 failure: the board "
            "script bypassed auto_pour_if_missing()/zone-fill, leaving both "
            "power nets without copper."
        )

    def test_no_connectivity_errors(self, routed_pcb: Path) -> None:
        """``kct check`` must report 0 connectivity errors.

        This is the user-visible DRC failure from #3148 ("Net 'GND'/'VCC'
        is partially routed: 1 of 2 pads stranded").
        """
        # Issue #7738: a full ``kct check`` also runs a Manifest leg, which
        # needs the bundle the generator's ``kct export`` step writes; with
        # no bundle it reports ``Overall: INCOMPLETE`` and exits 2.  The
        # generator still exits 0 when that export fails (the bundle is not
        # part of its ERC/DRC/LVS gate).  The usual cause is a venv synced
        # without the ``dev`` extra: ``kct export`` aborts when the optional
        # ``requests`` dependency (``parts`` extra) is missing (#4104).
        # Assert the precondition first so that failure names the export
        # step instead of reading as a board 00 DRC regression.
        manifest = routed_pcb.parent / "manufacturing" / "manifest.json"
        assert manifest.exists(), (
            "generate_design.py wrote no manufacturing bundle "
            f"({manifest} is missing), so `kct check` cannot pass its "
            "Manifest leg.  The usual cause is the environment, not board "
            "00: `kct export` aborts when the 'parts' extra ('requests') is "
            "missing.  If so, re-sync with "
            "`uv sync --frozen --extra dev`, which CI and the worktree hook use."
        )
        proc = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "check", str(routed_pcb)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        conn_errors = _connectivity_error_count(proc.stdout)
        assert conn_errors == 0, (
            f"kct check reported {conn_errors} connectivity issue(s) "
            "(stranded / partially-routed pads).  VCC/GND should be fully "
            "connected via filled copper zones after the #3148 fix.\n"
            f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
        )
        assert proc.returncode == 0, (
            f"kct check exited {proc.returncode} (expected 0 -- DRC clean).\n"
            f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
        )

    def test_led_anode_copper_and_sidecar_follow_the_reviewed_class(self, routed_pcb: Path) -> None:
        """Issue #10448: the recipe's copper and sidecar both say Digital at 0.127 mm."""
        assert _led_anode_segments(routed_pcb) == [(0.127, "F.Cu")] * 2
        sidecar = routed_pcb.parent / "net_class_map.json"
        assert json.loads(sidecar.read_text()) == json.loads(SIDECAR.read_text())

    def test_led_anode_class_reaches_the_fresh_routed_and_packaged_projects(
        self, routed_pcb: Path
    ) -> None:
        """Issue #10448: a clean build resolves LED_ANODE to Digital, 0.127 mm / 0.150 mm.

        The sidecar and copper alone are not enough: both the freshly
        generated routed ``.kicad_pro`` and the copy packed into the
        manufacturing ZIP must resolve the net to the reviewed class.
        """
        import zipfile

        from kicad_tools.core.project_clearance import (
            authored_netclass_widths,
            resolve_project_clearances,
        )

        out_dir = routed_pcb.parent
        sidecar_entry = json.loads((out_dir / "net_class_map.json").read_text())[LED_ANODE_NET_NAME]
        assert sidecar_entry["target_ampacity"] == 0.01
        with zipfile.ZipFile(out_dir / "manufacturing" / "kicad_project.zip") as bundle:
            packaged = json.loads(bundle.read("simple_led.kicad_pro"))
        fresh = json.loads((out_dir / "simple_led_routed.kicad_pro").read_text())
        for label, project in (("routed", fresh), ("packaged", packaged)):
            widths = authored_netclass_widths(project, [LED_ANODE_NET_NAME])
            clearance = resolve_project_clearances(project, [LED_ANODE_NET_NAME])
            assert widths[LED_ANODE_NET_NAME].source_class == "Digital", label
            assert widths[LED_ANODE_NET_NAME].width == 0.127, label
            assert clearance[LED_ANODE_NET_NAME].clearance == 0.15, label

    def test_fiducials_hold_the_pour_off_their_mask_openings(self, routed_pcb: Path) -> None:
        """Issue #10448: the stock fiducial clearance is widened before the pours fill."""
        fiducials = _fiducial_pads(routed_pcb)
        assert len(fiducials) == 3
        assert {fid["clearance"] - fid["margin"] for fid in fiducials} == {1.0}


class TestBoard00OfflineBomDeterminism:
    """Issue #7741 -- the LCSC column must not depend on network or host.

    ``kct export`` finds the ``project.kct`` BOM spec by walking up from the
    PCB directory, which misses it for any output dir outside the project
    tree.  The generator now passes ``--spec`` explicitly; this pins that the
    spec alone (no live API, no enrichment cache) fills every LCSC number.
    """

    @staticmethod
    def _export_bom(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *extra: str,
        calls: list[tuple[set[str], set[str]]] | None = None,
    ) -> list[str]:
        import shutil

        from kicad_tools.cli import export_cmd

        src = BOARD_DIR / "output"
        work = tmp_path / "out"
        work.mkdir()
        for name in ("simple_led.kicad_sch", "simple_led_routed.kicad_pcb"):
            shutil.copy(src / name, work / name)

        def _offline_api(items, *args, spec_refs=frozenset(), **kwargs):
            # Simulate a host with no LCSC API access: auto-matching is
            # still invoked (with the spec-resolved refs exempt), but it can
            # contribute nothing.  ``assembly`` downgrades this to a warning.
            if calls is not None:
                calls.append(({item.reference for item in items}, set(spec_refs)))
            raise ConnectionError("simulated offline host: LCSC API unreachable")

        monkeypatch.setattr("kicad_tools.export.assembly.enrich_bom_lcsc", _offline_api)
        rc = export_cmd.main(
            [
                str(work / "simple_led_routed.kicad_pcb"),
                "--output",
                str(work / "manufacturing"),
                "--mfr",
                "jlcpcb",
                "--skip-preflight",
                "--no-gerbers",
                "--no-cpl",
                "--no-report",
                "--no-project-zip",
                *extra,
            ]
        )
        assert rc == 0
        return (work / "manufacturing" / "bom_jlcpcb.csv").read_text().splitlines()

    def test_spec_flag_fills_lcsc_outside_project_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[set[str], set[str]]] = []
        lines = self._export_bom(
            tmp_path, monkeypatch, "--spec", str(BOARD_DIR / "project.kct"), calls=calls
        )
        committed = (BOARD_DIR / "output" / "manufacturing" / "bom_jlcpcb.csv").read_text()
        assert "\n".join(lines) == committed.rstrip("\n")
        assert all(line.rsplit(",", 1)[-1].strip() for line in lines[1:])
        # Every BOM item was resolved by the spec, so none was left for the
        # (offline) API to match -- the LCSC column comes from the spec alone.
        assert calls, "auto-matching stage was not reached"
        for refs, spec_refs in calls:
            assert refs <= spec_refs, f"not spec-resolved: {sorted(refs - spec_refs)}"

    def test_no_spec_overrides_spec_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``--no-spec`` wins over ``--spec``: no overlay, so offline the LCSC
        # column stays blank for every part.
        lines = self._export_bom(
            tmp_path, monkeypatch, "--spec", str(BOARD_DIR / "project.kct"), "--no-spec"
        )
        assert len(lines) > 1
        assert not any(line.rsplit(",", 1)[-1].strip() for line in lines[1:])

    def test_missing_spec_file_errors(self, tmp_path: Path) -> None:
        from kicad_tools.cli import export_cmd

        rc = export_cmd.main(
            [
                str(BOARD_DIR / "output" / "simple_led_routed.kicad_pcb"),
                "--output",
                str(tmp_path / "m"),
                "--spec",
                str(tmp_path / "nope.kct"),
            ]
        )
        assert rc == 1

    def test_generator_passes_spec_to_export(self) -> None:
        text = GENERATOR.read_text()
        assert '"--spec"' in text and '"project.kct"' in text


# ---------------------------------------------------------------------------
# Issue #10448: LED_ANODE class/width agreement and the fiducial pour clearance
# ---------------------------------------------------------------------------

OUTPUT_DIR = BOARD_DIR / "output"
ROUTED_PCB = OUTPUT_DIR / "simple_led_routed.kicad_pcb"
ROUTED_PROJECT = OUTPUT_DIR / "simple_led_routed.kicad_pro"
SIDECAR = OUTPUT_DIR / "net_class_map.json"
#: The sidecar entry committed before #10448 (Issue #7206): a class and a
#: width that matched neither the routed project nor the copper.
STALE_SIDECAR_ENTRY = {
    "name": "Power",
    "priority": 1,
    "trace_width": 0.25,
    "target_ampacity": 0.01,
}
#: Files beside the routed PCB that ``kct check`` reads.
ROUTED_SIDECARS = (
    "simple_led_routed.kicad_pro",
    "simple_led_routed.kicad_dru",
    "simple_led.kicad_sch",
    "simple_led.kicad_pro",
    "net_class_map.json",
    "lvs.json",
)


def _generator() -> dict:
    """The recipe's module globals, without running its ``main``."""
    return runpy.run_path(str(GENERATOR))


def _led_anode_segments(pcb_path: Path) -> list[tuple[float, str]]:
    """``(width, layer)`` of every LED_ANODE segment on ``pcb_path``."""
    from kicad_tools.schema.pcb import PCB

    return [
        (segment.width, segment.layer)
        for segment in PCB.load(str(pcb_path)).segments
        if segment.net_number == LED_ANODE_NET_NUM
    ]


def _fiducial_pads(pcb_path: Path) -> list[dict]:
    """Each fiducial pad's centre, radius, mask margin and clearance on ``pcb_path``."""
    from kicad_tools.sexp import parse_file

    pads: list[dict] = []
    for fp in parse_file(pcb_path).find_all("footprint"):
        if not (fp.get_string(0) or "").rsplit(":", 1)[-1].startswith("Fiducial"):
            continue
        at = fp.find("at")
        for pad in fp.find_all("pad"):
            size = pad.find("size")
            pads.append(
                {
                    "centre": (at.get_float(0), at.get_float(1)),
                    "radius": max(size.get_float(0), size.get_float(1)) / 2,
                    "margin": pad.find("solder_mask_margin").get_float(0),
                    "clearance": pad.find("clearance").get_float(0),
                }
            )
    return pads


def _stage_routed(board: Path) -> Path:
    """Copy the committed routed board, its sidecars and its bundle into ``board``."""
    board.mkdir(parents=True)
    for name in (ROUTED_PCB.name, *ROUTED_SIDECARS):
        shutil.copy(OUTPUT_DIR / name, board / name)
    shutil.copytree(OUTPUT_DIR / "manufacturing", board / "manufacturing")
    return board


def _agreement_results(board: Path):
    """``check_netclass_agreement`` over the routed board staged in ``board``."""
    from kicad_tools.router.rules import net_class_map_from_dict
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.netclass_agreement import check_netclass_agreement

    pcb_path = board / ROUTED_PCB.name
    sidecar = json.loads((board / SIDECAR.name).read_text())
    return check_netclass_agreement(
        pcb_path, PCB.load(str(pcb_path)), net_class_map_from_dict(sidecar)
    )


def _pinned_check(board: Path) -> dict:
    """``kct check --mfr jlcpcb --format json`` over the routed board staged in ``board``."""
    report = board / "check.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(board / ROUTED_PCB.name),
            "--mfr",
            "jlcpcb",
            "--format",
            "json",
            "--output",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert report.is_file(), proc.stderr[-2000:]
    return json.loads(report.read_text())


def _mask_findings(report: dict) -> list[dict]:
    """The ``mask_to_copper`` findings of warning severity or worse."""
    return [
        v
        for v in report["violations"]
        if v["rule_id"] == "mask_to_copper" and v["severity"] in ("warning", "error")
    ]


class TestBoard00LedAnodeNetclass:
    """Issue #10448: sidecar, routed project and copper agree on LED_ANODE.

    The recipe declared LED_ANODE ``Power`` at 0.25 mm in the sidecar while the
    routed project resolved it to ``Digital`` at 0.127 mm and the copper was
    0.200 mm wide, which ``kct check`` reports as ``netclass_sidecar_mismatch``.
    """

    def test_reviewed_class_is_the_router_class(self) -> None:
        """The recipe's constants describe the class the router files LED_ANODE under."""
        from kicad_tools.router.net_class import classify_and_apply_rules

        routing = classify_and_apply_rules({LED_ANODE_NET_NUM: LED_ANODE_NET_NAME})[
            LED_ANODE_NET_NAME
        ]
        gen = _generator()
        assert routing.name == gen["LED_ANODE_CLASS"]
        assert routing.priority == gen["LED_ANODE_CLASS_PRIORITY"]
        assert routing.clearance == gen["LED_ANODE_CLASS_CLEARANCE_MM"]

    def test_ampacity_target_is_the_spec_current(self) -> None:
        """The sidecar keeps declaring the 10 mA ``project.kct`` asks for."""
        gen = _generator()
        assert gen["AMPACITY_TARGETS_A"] == {LED_ANODE_NET_NAME: 0.01}
        assert "led_current: 10mA" in (BOARD_DIR / "project.kct").read_text()

    def test_sidecar_identical_from_a_clean_and_a_stale_directory(self, tmp_path: Path) -> None:
        """A stale entry is replaced: clean generation and regeneration agree byte for byte."""
        gen = _generator()
        clean, stale = tmp_path / "clean", tmp_path / "stale"
        clean.mkdir()
        stale.mkdir()
        (stale / SIDECAR.name).write_text(
            json.dumps({LED_ANODE_NET_NAME: STALE_SIDECAR_ENTRY}, indent=2)
        )
        gen["declare_ampacity_targets"](clean)
        gen["declare_ampacity_targets"](stale)
        assert (clean / SIDECAR.name).read_bytes() == SIDECAR.read_bytes()
        assert (stale / SIDECAR.name).read_bytes() == SIDECAR.read_bytes()

    def test_committed_sidecar_project_and_copper_agree(self) -> None:
        """The committed sidecar, routed project and LED_ANODE copper say the same thing."""
        from kicad_tools.core.project_clearance import authored_netclass_widths
        from kicad_tools.validate.rules.netclass_clearance_copper import load_project

        entry = json.loads(SIDECAR.read_text())[LED_ANODE_NET_NAME]
        assert entry == {
            "name": "Digital",
            "priority": 4,
            "trace_width": 0.127,
            "clearance": 0.15,
            "target_ampacity": 0.01,
        }
        settings = json.loads(ROUTED_PROJECT.read_text())["net_settings"]
        assert settings["netclass_assignments"][LED_ANODE_NET_NAME] == "Digital"
        digital = next(cls for cls in settings["classes"] if cls["name"] == "Digital")
        assert digital["track_width"] == entry["trace_width"]
        assert digital["clearance"] == entry["clearance"]
        resolved = authored_netclass_widths(
            load_project(ROUTED_PCB.resolve()), [LED_ANODE_NET_NAME]
        )
        assert (resolved[LED_ANODE_NET_NAME].source_class, resolved[LED_ANODE_NET_NAME].width) == (
            "Digital",
            0.127,
        )
        assert _led_anode_segments(ROUTED_PCB) == [(0.127, "F.Cu")] * 2

    def test_agreement_rule_is_silent_on_the_committed_board(self, tmp_path: Path) -> None:
        """``check_netclass_agreement`` reports nothing for the committed files."""
        assert _agreement_results(_stage_routed(tmp_path / "committed")).violations == []

    def test_agreement_rule_reports_a_conflicting_declaration(self, tmp_path: Path) -> None:
        """A sidecar that disagrees with the project is reported for LED_ANODE only."""
        from kicad_tools.validate.rules.netclass_agreement import RULE_SIDECAR_MISMATCH

        board = _stage_routed(tmp_path / "conflict")
        (board / SIDECAR.name).write_text(
            json.dumps({LED_ANODE_NET_NAME: STALE_SIDECAR_ENTRY}, indent=2)
        )
        violations = _agreement_results(board).violations
        assert [v.rule_id for v in violations] == [RULE_SIDECAR_MISMATCH]
        assert violations[0].nets == (LED_ANODE_NET_NAME,)
        assert "sidecar class 'Power' but project resolves 'Digital'" in violations[0].message


class TestBoard00FiducialPourClearance:
    """Issue #10448: the pour keeps off each fiducial's mask opening.

    The stock ``Fiducial_1mm_Mask2mm`` pad carries a 0.5 mm solder-mask margin
    and a 0.6 mm local clearance, so a pour around it stopped 0.1005 mm from the
    mask opening: a ``mask_to_copper`` warning on pinned KiCad 10.0.6.
    """

    def test_committed_web_clears_the_jlcpcb_warning_band(self) -> None:
        """Each pad clearance leaves more than the 0.15 mm band beyond its opening."""
        from kicad_tools.validate.mask_copper import _JLCPCB_MASK_COPPER_POLICY

        fiducials = _fiducial_pads(ROUTED_PCB)
        assert len(fiducials) == 3
        band = _JLCPCB_MASK_COPPER_POLICY.warning_clearance_mm
        assert all(fid["clearance"] - fid["margin"] > band for fid in fiducials)

    def test_saved_pour_holds_that_distance(self) -> None:
        """The saved VCC fill is as far from each opening as the pad clearance says."""
        from shapely.geometry import Point, Polygon

        from kicad_tools.sexp import parse_file

        pours: list[Polygon] = []
        for zone in parse_file(ROUTED_PCB).find_all("zone"):
            if zone.find("net").get_string(0) != "VCC":
                continue
            for filled in zone.find_all("filled_polygon"):
                pours.append(
                    Polygon(
                        [
                            (xy.get_float(0), xy.get_float(1))
                            for xy in filled.find("pts").find_all("xy")
                        ]
                    )
                )
        assert pours
        for fid in _fiducial_pads(ROUTED_PCB):
            opening = Point(fid["centre"]).buffer(fid["radius"] + fid["margin"], 256)
            distance = min(pour.distance(opening) for pour in pours)
            assert distance == pytest.approx(fid["clearance"] - fid["margin"], abs=0.01)

    def test_recipe_sets_the_clearance_and_is_idempotent(self, tmp_path: Path) -> None:
        """The committed pads already carry the recipe's clearance; a changed pad is restored."""
        gen = _generator()
        staged = tmp_path / ROUTED_PCB.name
        staged.write_bytes(ROUTED_PCB.read_bytes())
        gen["_set_fiducial_pour_clearance"](staged)
        assert staged.read_bytes() == ROUTED_PCB.read_bytes()

        text = ROUTED_PCB.read_text()
        narrowed = text.replace("(clearance 1.5)", "(clearance 0.6)", 1)
        assert narrowed != text
        staged.write_text(narrowed)
        gen["_set_fiducial_pour_clearance"](staged)
        assert staged.read_bytes() == ROUTED_PCB.read_bytes()

    def test_recipe_refuses_a_board_without_a_fiducial(self, tmp_path: Path) -> None:
        """No fiducial to widen is an error, never a silent pass."""
        gen = _generator()
        staged = tmp_path / ROUTED_PCB.name
        staged.write_text(
            ROUTED_PCB.read_text().replace("Fiducial:Fiducial_1mm_Mask2mm", "Test:Pad")
        )
        with pytest.raises(ValueError, match="no fiducial footprint"):
            gen["_set_fiducial_pour_clearance"](staged)


@pytest.fixture(scope="module")
def genuine_violation_stage(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Path]:
    """Stage disposable copies of the committed package once its pinned check is clean.

    Skips where the pristine board's mask-to-copper assessment cannot run.
    """
    root = tmp_path_factory.mktemp("board00_genuine")
    baseline = _pinned_check(_stage_routed(root / "pristine"))
    if baseline["coverage"]["mask_to_copper"]["state"] != "checked":
        pytest.skip("mask_to_copper assessment needs a native KiCad toolchain")
    summary = baseline["summary"]
    assert (summary["errors"], summary["warnings"]) == (0, 0)

    def stage(name: str) -> Path:
        return _stage_routed(root / name)

    return stage


@pytest.mark.slow
class TestBoard00PinnedCheckStillFlagsGenuineViolations:
    """Issue #10448: the corrected board's clean result does not hide genuine violations.

    Each case stages a disposable copy of the committed package and runs the
    pinned ``kct check --mfr jlcpcb``. The mask-to-copper assessment needs a
    native KiCad toolchain, so the class skips where the pristine board's
    assessment cannot run.
    """

    def test_conflicting_declaration_is_reported(
        self, genuine_violation_stage: Callable[[str], Path]
    ) -> None:
        """The pre-#10448 sidecar entry is the ``netclass_sidecar_mismatch`` it used to be."""
        board = genuine_violation_stage("conflict")
        (board / SIDECAR.name).write_text(
            json.dumps({LED_ANODE_NET_NAME: STALE_SIDECAR_ENTRY}, indent=2)
        )
        warnings = [v for v in _pinned_check(board)["violations"] if v["severity"] == "warning"]
        assert [v["rule_id"] for v in warnings] == ["netclass_sidecar_mismatch"]
        assert warnings[0]["message"] == (
            "Net 'LED_ANODE': sidecar class 'Power' but project resolves 'Digital'; "
            "sidecar trace_width 0.250mm but project class 'Digital' track_width 0.127mm"
        )

    def test_different_net_copper_beside_an_opening_is_reported(
        self, genuine_violation_stage: Callable[[str], Path]
    ) -> None:
        """A GND track stopped 0.05 mm short of J1/1's (VCC) opening is a different-net violation."""
        board = genuine_violation_stage("different_net")
        pcb = board / ROUTED_PCB.name
        # Anchored on J1/2 (GND): KiCad reassigns a floating stub to the pad it
        # touches, which would make the probe same-net.
        y_end = 87.08 + 0.05 + 0.127 / 2
        segment = (
            f"\t(segment\n\t\t(start 141.0 88.77) (end 141.0 {y_end:.6f}) (width 0.127) "
            f'(layer "F.Cu")\n\t\t(uuid "00000000-0000-4000-8000-0000000000a4") (net 3)\n\t)\n'
        )
        text = pcb.read_text()
        assert text.rstrip().endswith(")")
        pcb.write_text(text.rstrip()[:-1] + segment + ")\n")
        findings = _mask_findings(_pinned_check(board))
        assert len(findings) == 1
        assert findings[0]["severity"] == "error"
        assert "Mask-to-copper disjoint: violation" in findings[0]["message"]
        assert (
            "opening pad:J1/1; exposed copper segment (net GND); different-net"
            in (findings[0]["message"])
        )
        assert findings[0]["actual_value"] == pytest.approx(0.05, abs=0.001)

    def test_narrow_fiducial_web_is_reported(
        self, genuine_violation_stage: Callable[[str], Path]
    ) -> None:
        """A fiducial whose pour web drops to 0.05 mm is the ``mask_to_copper`` finding again."""
        from kicad_tools.cli.runner import find_kicad_cli
        from kicad_tools.sexp import SExp, parse_file

        board = genuine_violation_stage("narrow_web")
        pcb = board / ROUTED_PCB.name
        doc = parse_file(pcb)
        narrowed = 0
        for fp in doc.find_all("footprint"):
            if not (fp.get_string(0) or "").rsplit(":", 1)[-1].startswith("Fiducial"):
                continue
            for pad in fp.find_all("pad"):
                pad.children[pad.children.index(pad.find("clearance"))] = SExp.list(
                    "clearance", 0.55
                )
                narrowed += 1
        assert narrowed == 3
        pcb.write_text(doc.to_string())
        kicad_cli = find_kicad_cli()
        assert kicad_cli is not None
        refill = subprocess.run(
            [
                kicad_cli,
                "pcb",
                "drc",
                str(pcb),
                "--refill-zones",
                "--save-board",
                "--format",
                "json",
                "--output",
                str(board / "refill.json"),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert (board / "refill.json").is_file(), refill.stderr[-2000:]
        findings = _mask_findings(_pinned_check(board))
        assert len(findings) == 3
        assert {finding["severity"] for finding in findings} == {"error"}
        assert all("Mask-to-copper disjoint: violation" in f["message"] for f in findings)
        assert all(f["actual_value"] == pytest.approx(0.0505, abs=0.001) for f in findings)
