"""A lost capability probe must not turn `kct zones fill` into silent no-fill (Issue #9938).

``_DrcRefillProbe`` answers ``False`` (and records "inconclusive") when the
``pcb drc --help`` probe is killed, times out or replies with nothing.  The
DRC-based fill runner used to treat that ``False`` as "this kicad-cli has no
``--refill-zones``": it ran a plain DRC, which on KiCad 10 writes a report and
saves nothing, and reported ``Zones filled`` with the board byte-identical to
the unfilled input.  On the 20 x 20 mm wired-pad fixture that shipped
10.376 mm2 of F.Cu instead of the answered control's 255.033 mm2, with zero
DRC errors -- connectivity alone cannot see the lost plane.  A later probe
that *did* answer then made thermal remediation skip its own first refill on
top of the fill that never saved.

The fake-driven tests pin the contract the issue demands:

* an inconclusive probe requests the explicit ``--refill-zones
  --save-board`` and lets kicad-cli's parser decide;
* a flag-driven run that writes a report without saving the board is an
  explicit failure that preserves the prior copper (in place and for a
  pre-existing separate destination), as is a run killed or timed out
  mid-save, with or without prior fill polygons;
* the runner judges the completed write, never the polygon count;
* a genuine ``Unknown argument`` rejection restores the original fills
  before the single no-flags retry, memoises the definitive answer, and
  keeps the byte restoration for a retry that dies mid-write;
* the caller's timeout bounds both attempts without growing;
* only a verified flag-driven save (``refill_saved``) lets the post-fill
  remediation skip its first refill -- never a fresh probe -- and a clean
  supported fill is still exactly one native fill.

``TestPinnedKicad`` drives the public ``kct zones fill`` on the wired-pad and
pour-only boards against the pinned KiCad release and compares saved
polygons, plotted F.Cu copper and native DRC with the answered control.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import pytest

from kicad_tools.cli import runner
from kicad_tools.schema.pcb import PCB

#: The wired-pad shape from the issue: zone with ``(fill yes ...)`` and no
#: saved ``filled_polygon`` -- exactly the board a lost probe used to leave
#: "successfully" unfilled.
UNFILLED_BOARD = """(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "") (net 1 "GND")
  (zone (net 1) (net_name "GND") (layer "F.Cu") (hatch edge 0.5)
   (min_thickness 0.2)
   (fill yes (thermal_gap 0.25) (thermal_bridge_width 0.3))
   (polygon (pts (xy 12 12) (xy 28 12) (xy 28 28) (xy 12 28)))))
"""

#: The same board carrying a saved fill -- the "prior copper" a failed
#: transaction must preserve.
FILLED_BOARD = UNFILLED_BOARD.replace(
    "  (polygon (pts (xy 12 12) (xy 28 12) (xy 28 28) (xy 12 28)))",
    "  (filled_polygon (pts (xy 12 12) (xy 28 12) (xy 28 28) (xy 12 28)))"
    "\n  (polygon (pts (xy 12 12) (xy 28 12) (xy 28 28) (xy 12 28)))",
)

#: A rewritten board whose pour is legitimately empty: kicad-cli re-serialized
#: the file and saved no ``filled_polygon``.
EMPTY_FILL_BOARD = UNFILLED_BOARD.replace("(version 20240108)", "(version 20260206)")

CLI = Path("/fake/kicad-cli")

_REAL_REMEDIATE = runner._remediate_starved_thermal


@pytest.fixture(autouse=True)
def _clear_capability_caches():
    runner._kicad_cli_has_fill_zones.cache_clear()
    runner._kicad_drc_supports_refill.cache_clear()
    yield
    runner._kicad_cli_has_fill_zones.cache_clear()
    runner._kicad_drc_supports_refill.cache_clear()


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def _drc_runs(commands: list[list[str]]) -> list[list[str]]:
    return [c for c in commands if "--help" not in c]


class _FakeCli:
    """A kicad-cli double with a lost first ``pcb drc --help`` reply.

    The first ``pcb drc --help`` probe raises ``TimeoutExpired`` -- the
    reproduction from the issue -- unless ``first_probe_timeout`` is false.
    Later probes answer per ``later_help``.  Every real invocation records
    its argv and the timeout it was given, then behaves per the kwargs:

    * ``saves``: a run carrying ``--save-board`` rewrites the board with
      ``FILLED_BOARD`` (a completed save always rewrites the file), or with
      ``saved_board`` when given;
    * ``rejects_refill``: a run carrying ``--refill-zones`` exits 1 with
      ``Unknown argument: --refill-zones`` and touches nothing -- the
      definitive rejection of a kicad-cli without the flag;
    * ``dies``: a run carrying ``--save-board`` leaves the board
      half-written and is killed (``"killed"``) or raises ``TimeoutExpired``
      (``"timeout"``);
    * ``retry_dies``: the same, for the no-flags run after a rejection;
    * otherwise the DRC writes an empty-violations report.

    Post-fill remediation is stubbed out; a test that needs the real loop
    re-patches ``runner._remediate_starved_thermal``.
    """

    def __init__(
        self,
        monkeypatch,
        *,
        first_probe_timeout=True,
        later_help=True,
        saves=True,
        saved_board=FILLED_BOARD,
        rejects_refill=False,
        dies=None,
        retry_dies=None,
    ):
        self.commands: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self._first_probe_timeout = first_probe_timeout
        self._first_probe_pending = True
        self._later_help = later_help
        self._saves = saves
        self._saved_board = saved_board
        self._rejects_refill = rejects_refill
        self._dies = dies
        self._retry_dies = retry_dies
        monkeypatch.setattr(runner.subprocess, "run", self._run)
        monkeypatch.setattr(runner, "_normalize_pad_connection_in_place", lambda _p: None)
        monkeypatch.setattr(runner, "_remediate_starved_thermal", MagicMock(return_value=None))

    def _die(self, cmd, kwargs, how):
        Path(cmd[-1]).write_text("(kicad_pcb (versi")  # killed / timed out mid-save
        if how == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 30)
        return _completed(cmd, -9, "", "")

    def _run(self, cmd, capture_output=True, text=True, **kwargs):
        cmd = [str(c) for c in cmd]
        self.commands.append(cmd)
        self.timeouts.append(kwargs.get("timeout"))
        if "--help" in cmd:
            if "fill-zones" in cmd:
                return _completed(cmd, stdout="")
            if self._first_probe_pending and self._first_probe_timeout:
                self._first_probe_pending = False
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 30)
            self._first_probe_pending = False
            return _completed(
                cmd,
                stdout="--refill-zones Usage: kicad-cli pcb drc"
                if self._later_help
                else "Usage: kicad-cli pcb drc",
            )
        if "--refill-zones" in cmd and self._rejects_refill:
            return _completed(cmd, 1, "", "Unknown argument: --refill-zones")
        Path(cmd[cmd.index("--output") + 1]).write_text('{"violations":[]}')
        if "--save-board" in cmd:
            if self._dies is not None:
                return self._die(cmd, kwargs, self._dies)
            if self._saves:
                Path(cmd[-1]).write_text(self._saved_board)
        elif self._rejects_refill and self._retry_dies is not None:
            return self._die(cmd, kwargs, self._retry_dies)
        return _completed(cmd)


def test_lost_probe_still_requests_refill_and_save(monkeypatch, tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(UNFILLED_BOARD)
    cli = _FakeCli(monkeypatch)

    result = runner.run_fill_zones(pcb, kicad_cli=CLI)

    assert result.success, result.stderr
    fill_cmd = next(c for c in cli.commands if "--help" not in c)
    assert "--refill-zones" in fill_cmd
    assert "--save-board" in fill_cmd
    assert result.refill_saved is True
    assert pcb.read_text() == FILLED_BOARD


def test_lost_probe_report_without_save_is_failure_preserving_input(monkeypatch, tmp_path):
    """The wired-pad reproduction: report written, board never saved."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(UNFILLED_BOARD)
    _FakeCli(monkeypatch, saves=False)

    result = runner.run_fill_zones(pcb, kicad_cli=CLI)

    assert result.success is False
    assert "did not save the refilled board" in result.stderr
    assert result.refill_saved is False
    # The prior copper -- here the unfilled input bytes -- is preserved.
    assert pcb.read_text() == UNFILLED_BOARD


def test_lost_probe_failure_restores_stale_fills(monkeypatch, tmp_path):
    """A board with prior fill polygons gets them back on the failed save."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(FILLED_BOARD)
    _FakeCli(monkeypatch, saves=False)

    result = runner.run_fill_zones(pcb, kicad_cli=CLI)

    assert result.success is False
    assert "did not save the refilled board" in result.stderr
    assert pcb.read_text() == FILLED_BOARD


def test_completed_write_with_no_polygons_is_not_a_missing_fill(monkeypatch, tmp_path):
    """The runner judges the write, not the polygon count.

    A legitimately empty zone saves zero ``filled_polygon`` nodes; what
    separates it from the lost pour is that kicad-cli rewrote the board.
    """
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(UNFILLED_BOARD)
    _FakeCli(monkeypatch, saved_board=EMPTY_FILL_BOARD)

    result = runner.run_fill_zones(pcb, kicad_cli=CLI)

    assert result.success, result.stderr
    assert pcb.read_text() == EMPTY_FILL_BOARD
    assert result.refill_saved is True


@pytest.mark.parametrize("prior", [UNFILLED_BOARD, FILLED_BOARD], ids=["no-prior", "prior-fill"])
@pytest.mark.parametrize("dies", ["killed", "timeout"])
def test_lost_probe_run_that_dies_mid_save_restores_prior_copper(
    monkeypatch, tmp_path, prior, dies
):
    """Issue #6724's byte restoration still holds behind a lost probe."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(prior)
    _FakeCli(monkeypatch, dies=dies)

    result = runner.run_fill_zones(pcb, kicad_cli=CLI)

    assert result.success is False
    assert result.refill_saved is False
    assert pcb.read_text() == prior


def _failing_cli(monkeypatch, failure):
    if failure == "report_only":
        return _FakeCli(monkeypatch, saves=False)
    if failure in ("killed", "timeout"):
        return _FakeCli(monkeypatch, dies=failure)
    return _FakeCli(monkeypatch, rejects_refill=True, retry_dies=failure)


@pytest.mark.parametrize(
    "failure", ["report_only", "killed", "timeout", "retry_killed", "retry_timeout"]
)
def test_lost_probe_failure_preserves_preexisting_destination(monkeypatch, tmp_path, failure):
    """A seeded known-good destination survives every failed separate-output fill."""
    src = tmp_path / "in.kicad_pcb"
    src.write_text(UNFILLED_BOARD)
    dest = tmp_path / "out.kicad_pcb"
    dest.write_text(FILLED_BOARD)
    _failing_cli(monkeypatch, failure)

    result = runner.run_fill_zones(src, output_path=dest, kicad_cli=CLI)

    assert result.success is False
    assert dest.read_text() == FILLED_BOARD
    assert src.read_text() == UNFILLED_BOARD


@pytest.mark.parametrize("preexisting", [False, True])
@pytest.mark.parametrize("verification", ["regression", "failed"])
def test_failed_final_check_never_publishes_partial_output(
    monkeypatch, tmp_path, preexisting, verification
):
    """The final saved-copper check sees the output's project and only the candidate changes."""
    from kicad_tools.cli import main
    from kicad_tools.drc import geometric

    src = tmp_path / "source.kicad_pcb"
    src.write_text(UNFILLED_BOARD)
    dest = tmp_path / "output.kicad_pcb"
    if preexisting:
        dest.write_bytes(b"private prior output")
    for suffix in (".kicad_pro", ".kicad_dru"):
        dest.with_suffix(suffix).write_text("destination " + suffix)
        src.with_suffix(suffix).write_text("source " + suffix)
    _FakeCli(monkeypatch)
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: CLI)

    def check_candidate(board, **kwargs):
        assert board.name == dest.name and board != dest
        assert board.read_text() == FILLED_BOARD
        assert dest.exists() == preexisting
        if preexisting:
            assert dest.read_bytes() == b"private prior output"
        for suffix in (".kicad_pro", ".kicad_dru"):
            assert board.with_suffix(suffix).read_text() == "destination " + suffix
        if verification == "failed":
            return geometric.SavedFillCheck(ran=False, failed=True, note="native DRC failed")
        return geometric.SavedFillCheck(ran=True, regressions=["disconnected saved copper"])

    monkeypatch.setattr(geometric, "check_saved_fill", check_candidate)
    assert main(["zones", "fill", str(src), "--output", str(dest)]) == 1
    assert src.read_text() == UNFILLED_BOARD
    assert dest.exists() == preexisting
    if preexisting:
        assert dest.read_bytes() == b"private prior output"
    assert not list(tmp_path.glob(".kct-zone-fill-*"))


def test_report_allocation_failure_preserves_preexisting_destination(monkeypatch, tmp_path):
    """Setup failing before the native run (unwritable report dir) never touches the destination."""
    src = tmp_path / "in.kicad_pcb"
    src.write_text(UNFILLED_BOARD)
    dest = tmp_path / "out.kicad_pcb"
    dest.write_bytes(FILLED_BOARD.encode())
    _FakeCli(monkeypatch)

    def _denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(runner.tempfile, "mkstemp", _denied)

    with pytest.raises(PermissionError):
        runner.run_fill_zones(src, output_path=dest, kicad_cli=CLI)

    assert dest.read_bytes() == FILLED_BOARD.encode()
    assert src.read_text() == UNFILLED_BOARD


def test_unreadable_preexisting_destination_is_refused_not_overwritten(monkeypatch, tmp_path):
    """A destination the user cannot open for reading is left as it is."""
    src = tmp_path / "in.kicad_pcb"
    src.write_text(UNFILLED_BOARD)
    dest = tmp_path / "out.kicad_pcb"
    dest.write_bytes(FILLED_BOARD.encode())
    _FakeCli(monkeypatch)

    real_open = Path.open

    def deny_access(self, mode="r", *args, **kwargs):
        if self == dest and "r" in mode:
            raise PermissionError(13, "Permission denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_access)

    result = runner.run_fill_zones(src, output_path=dest, kicad_cli=CLI)

    monkeypatch.undo()
    assert result.success is False
    assert "refused" in result.stderr
    assert dest.read_bytes() == FILLED_BOARD.encode()
    assert src.read_text() == UNFILLED_BOARD


def test_lost_probe_successful_separate_output_writes_filled_copy(monkeypatch, tmp_path):
    src = tmp_path / "in.kicad_pcb"
    src.write_text(UNFILLED_BOARD)
    dest = tmp_path / "out.kicad_pcb"
    _FakeCli(monkeypatch)

    result = runner.run_fill_zones(src, output_path=dest, kicad_cli=CLI)

    assert result.success, result.stderr
    assert dest.read_text() == FILLED_BOARD
    assert src.read_text() == UNFILLED_BOARD


def test_rejection_restores_fills_then_retries_once_without_flags(monkeypatch, tmp_path):
    """A kicad-cli that rejects the flag takes the no-flags path."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(FILLED_BOARD)
    cli = _FakeCli(monkeypatch, rejects_refill=True)

    board_at_retry: list[str] = []
    real_run = cli._run

    def checking_run(cmd, **kwargs):
        if "--help" not in cmd and "--refill-zones" not in cmd:
            board_at_retry.append(pcb.read_text())
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(runner.subprocess, "run", checking_run)

    result = runner.run_fill_zones(pcb, kicad_cli=CLI)

    assert result.success, result.stderr
    runs = _drc_runs(cli.commands)
    assert len(runs) == 2, "exactly one rejected attempt and one retry"
    assert "--refill-zones" in runs[0] and "--save-board" in runs[0]
    assert "--refill-zones" not in runs[1] and "--save-board" not in runs[1]
    # The original fill was restored BEFORE the retry ran, and is what the
    # retry started from.
    assert board_at_retry == [FILLED_BOARD]
    # The definitive answer is memoised: no later probe can flip it.
    assert runner._kicad_drc_supports_refill(CLI) is False
    assert runner._kicad_drc_supports_refill.inconclusive(CLI) is False
    # The no-flags retry makes no refill_saved claim.
    assert result.refill_saved is False


@pytest.mark.parametrize("prior", [UNFILLED_BOARD, FILLED_BOARD], ids=["no-prior", "prior-fill"])
def test_rejection_then_dying_retry_restores_the_board_it_started_from(
    monkeypatch, tmp_path, prior
):
    """The no-flags retry keeps Issue #6724's byte restoration."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(prior)
    cli = _FakeCli(monkeypatch, rejects_refill=True, retry_dies="killed")

    result = runner.run_fill_zones(pcb, kicad_cli=CLI)

    assert result.success is False
    assert "SIGKILL" in result.stderr
    assert len(_drc_runs(cli.commands)) == 2
    assert pcb.read_text() == prior


def test_exit_args_without_unknown_argument_is_not_a_rejection(monkeypatch, tmp_path):
    """An unrelated exit-1 usage error must not trigger the no-flags retry."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(UNFILLED_BOARD)
    cli = _FakeCli(monkeypatch)

    def failing(cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        cli.commands.append(cmd)
        if "--help" in cmd:
            if "fill-zones" in cmd:
                return _completed(cmd, stdout="")
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 30)
        return _completed(cmd, 1, "", "bad option --parity-something")

    monkeypatch.setattr(runner.subprocess, "run", failing)

    result = runner.run_fill_zones(pcb, kicad_cli=CLI)

    assert result.success is False
    assert len(_drc_runs(cli.commands)) == 1, "no retry for a non-rejection exit 1"
    assert pcb.read_text() == UNFILLED_BOARD


def test_timeout_bounds_both_attempts_without_growing(monkeypatch, tmp_path):
    """A sub-second budget is passed through (Issue #6273), never rounded up."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(UNFILLED_BOARD)
    cli = _FakeCli(monkeypatch, rejects_refill=True)

    result = runner.run_fill_zones(pcb, kicad_cli=CLI, time_left=lambda: 0.5)

    assert result.success, result.stderr
    # The probe's own 30 s cap aside, exactly the two DRC attempts carry a timeout.
    budgets = [t for t in cli.timeouts if t is not None and t < 30]
    assert len(budgets) == 2
    assert 0 < budgets[1] <= budgets[0] <= 0.5


def test_remediation_skip_follows_the_completed_invocation_not_a_probe(monkeypatch, tmp_path):
    """``refill_saved`` alone decides ``skip_first_refill`` (Issue #9938 bug 2)."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(UNFILLED_BOARD)
    monkeypatch.setattr(runner, "_kicad_cli_has_fill_zones", lambda _cli: False)
    # The probe now answers a confident True -- e.g. it recovered after the
    # fill's own probe timed out.  That late reply must not decide anything.
    runner._kicad_drc_supports_refill.record(CLI, True)
    remediation = MagicMock(return_value=None)
    monkeypatch.setattr(runner, "_remediate_starved_thermal", remediation)

    for saved, expected_skip in ((False, False), (True, True)):
        remediation.reset_mock()
        # Set after construction so that, without the fix, this fails on the
        # remediation decision itself rather than on the field being unknown.
        fill = runner.KiCadCLIResult(success=True, output_path=pcb)
        fill.refill_saved = saved
        monkeypatch.setattr(runner, "_run_fill_zones_via_drc", lambda *a, **k: fill)
        result = runner.run_fill_zones(pcb, kicad_cli=CLI)
        assert result.success
        remediation.assert_called_once()
        assert remediation.call_args.kwargs["skip_first_refill"] is expected_skip


@pytest.mark.parametrize("probe_lost", [False, True], ids=["answered", "lost"])
def test_clean_supported_fill_is_still_one_native_fill(monkeypatch, tmp_path, probe_lost):
    """Through the real remediation: one ``--save-board`` launch, probe or not."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(UNFILLED_BOARD)
    cli = _FakeCli(monkeypatch, first_probe_timeout=probe_lost)
    seen: dict[str, object] = {}

    def remediate(*args, **kwargs):
        seen.update(kwargs)
        return _REAL_REMEDIATE(*args, **kwargs)

    import kicad_tools.zones.fill_clearance as fill_clearance

    monkeypatch.setattr(runner, "_remediate_starved_thermal", remediate)
    monkeypatch.setattr(runner, "_apply_foreign_pad_clearance", lambda _p: False)
    monkeypatch.setattr(fill_clearance, "starved_thermal_pad_uuids", lambda _r: set())
    monkeypatch.setattr(fill_clearance, "isolated_copper_zone_uuids", lambda _r: set())

    result = runner.run_fill_zones(pcb, kicad_cli=CLI)

    assert result.success, result.stderr
    assert seen["skip_first_refill"] is True
    runs = _drc_runs(cli.commands)
    assert [("--save-board" in c) for c in runs] == [True, False]


# ---------------------------------------------------------------------------
# Pinned KiCad: the public command on the issue's boards
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pinned_cli(tmp_path_factory):
    """The pinned kicad-cli and a directory it can reach; skips without KiCad 10.0.6."""
    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain

    root = tmp_path_factory.mktemp("zones_fill_probe_9938")
    try:
        with evidence_toolchain([root]) as toolchain:
            yield toolchain.kicad_cli, root
    except KiCadEnvironmentError as exc:
        pytest.skip(f"pinned kicad-cli unavailable: {exc}")


def _board_text(*, tracks=True, pads=True, zone=True) -> str:
    """The issue's 20 x 20 mm control: a 16 x 16 mm GND pour on F.Cu.

    With ``tracks`` the four segments wire the two 1 mm pads at opposite
    corners; with pads but no tracks the pads are joined by the pour alone.
    """
    points = [(17, 17), (23, 17), (23, 23), (17, 23), (17, 17)]
    segments = (
        "\n".join(
            f'(segment (start {x} {y}) (end {u} {v}) (width 0.4) (layer "F.Cu") (net 1))'
            for (x, y), (u, v) in zip(points, points[1:], strict=False)
        )
        if tracks
        else ""
    )
    footprints = (
        "".join(
            '(footprint "kicad-tools:FillProbePad_D1.0mm" (layer "F.Cu")'
            f" (at {x} {y})\n"
            "  (attr smd)\n"
            '  (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))\n'
            " )"
            for x, y in ((17, 17), (23, 23))
        )
        if pads
        else ""
    )
    pour = (
        '(zone (net 1) (net_name "GND") (layer "F.Cu") (hatch edge 0.5)\n'
        "  (connect_pads (clearance 0.2)) (min_thickness 0.2)\n"
        "  (fill yes (thermal_gap 0.25) (thermal_bridge_width 0.3) (island_removal_mode 1))\n"
        "  (polygon (pts (xy 12 12) (xy 28 12) (xy 28 28) (xy 12 28))))"
        if zone
        else ""
    )
    return (
        "(kicad_pcb (version 20240108) (generator pcbnew)\n"
        " (general (thickness 1.6))\n"
        ' (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        ' (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "GND")\n'
        " (gr_rect (start 10 10) (end 30 30) (stroke (width 0.05) (type default))\n"
        '  (fill none) (layer "Edge.Cuts"))\n'
        f" {segments}\n {footprints}\n {pour})"
    )


def _kct_zones_fill(*argv: str, lose_first_probe=False, first_save_writes=True) -> tuple:
    """Run the public ``kct zones fill`` once; return ``(exit status, flags requested)``.

    ``lose_first_probe`` makes the first ``pcb drc --help`` raise
    ``TimeoutExpired`` -- the issue's reproduction.  With
    ``first_save_writes`` false the first ``--refill-zones --save-board`` run
    is executed by the real kicad-cli *without those two flags*: it writes
    the DRC report and leaves the board alone, which is exactly what a save
    that did not happen looks like to the runner.
    """
    from kicad_tools.cli import main

    real_run = subprocess.run
    if lose_first_probe:
        # The probe memoises a definitive answer per kicad-cli path for the
        # life of the process: an earlier fill in the same test would
        # otherwise answer for the probe this run is meant to lose.
        runner._kicad_drc_supports_refill.cache_clear()
    pending = {"probe": lose_first_probe, "strip": not first_save_writes}
    requested: list[list[str]] = []

    def run(cmd, *args, **opts):
        words = [str(a) for a in cmd]
        if pending["probe"] and words[1:4] == ["pcb", "drc", "--help"]:
            pending["probe"] = False
            raise subprocess.TimeoutExpired(cmd, opts.get("timeout") or 30)
        if {"--refill-zones", "--save-board"} <= set(words):
            requested.append(words)
            if pending["strip"]:
                pending["strip"] = False
                cmd = [a for a in cmd if str(a) not in ("--refill-zones", "--save-board")]
        return real_run(cmd, *args, **opts)

    with mock.patch.object(subprocess, "run", run):
        status = main(["zones", "fill", *argv])
    return status, bool(requested)


def _saved_polygons(board: Path) -> list:
    """The saved fill geometry: one point list per ``filled_polygon``."""
    return [poly for zone in PCB.load(board).zones for poly in zone.filled_polygons]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _front_copper_mm2(cli: Path, board: Path, out: Path) -> float:
    """F.Cu copper area of the plotted board, by the production Gerber reader."""
    from kicad_tools.validate.gerber_geometry import parse_gerber_geometry

    out.mkdir()
    subprocess.run(
        [
            str(cli),
            "pcb",
            "export",
            "gerbers",
            "--layers",
            "F.Cu",
            "--disable-aperture-macros",
            "--no-protel-ext",
            "--precision",
            "6",
            "--output",
            str(out),
            str(board),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    (gerber,) = out.glob("*.gbr")
    return parse_gerber_geometry(gerber.read_text()).geometry.area


def _unconnected_items(cli: Path, board: Path, report: Path, *, refill=False) -> int:
    """Opens a native DRC reports; ``refill`` refills in memory before checking."""
    argv = [str(cli), "pcb", "drc", "--format", "json", "--output", str(report)]
    if refill:
        argv.append("--refill-zones")
    subprocess.run([*argv, str(board)], check=True, capture_output=True, text=True)
    violations = json.loads(report.read_text()).get("violations", [])
    return sum(v.get("type") == "unconnected_items" for v in violations)


class TestPinnedKicad:
    #: F.Cu mm2 on KiCad 10.0.6: the board unfilled, and with the pour saved.
    CONTROLS = {
        "wired": (10.376, 255.033),
        "pour_only": (1.571, 254.624),
    }

    @pytest.mark.parametrize("board_kind", ["wired", "pour_only"])
    def test_public_fill_matches_the_answered_control(self, pinned_cli, board_kind):
        """Lost probe == answered probe: same pour, same plotted copper, no opens."""
        cli, root = pinned_cli
        bare_mm2, filled_mm2 = self.CONTROLS[board_kind]
        text = _board_text(tracks=board_kind == "wired")
        measured: dict[str, tuple] = {}

        unfilled = root / f"{board_kind}-unfilled.kicad_pcb"
        unfilled.write_text(text)
        assert _front_copper_mm2(cli, unfilled, root / f"{board_kind}-unfilled-plot") == (
            pytest.approx(bare_mm2, abs=0.001)
        ), "the fixture no longer reproduces the issue's control board"

        for probe in ("answered", "lost"):
            board = root / f"{board_kind}-{probe}.kicad_pcb"
            board.write_text(text)
            status, requested = _kct_zones_fill(str(board), lose_first_probe=probe == "lost")
            assert (status, requested) == (0, True), f"{board_kind}/{probe}"
            measured[probe] = (
                _saved_polygons(board),
                _front_copper_mm2(cli, board, root / f"{board_kind}-{probe}-plot"),
                (
                    _unconnected_items(cli, board, root / f"{board_kind}-{probe}-saved.json"),
                    _unconnected_items(
                        cli, board, root / f"{board_kind}-{probe}-refilled.json", refill=True
                    ),
                ),
            )

        answered, lost = measured["answered"], measured["lost"]
        # The pour is on disk -- not merely "not an open", which is all a
        # wired board can show once its plane is gone.
        assert answered[1] == pytest.approx(filled_mm2, abs=0.001)
        assert lost[0] == answered[0], "the lost-probe fill saved different geometry"
        assert lost[1] == pytest.approx(answered[1], abs=0.001)
        assert lost[2] == answered[2] == (0, 0)

    def test_report_without_a_save_fails_and_preserves_prior_copper(self, pinned_cli):
        """Real kicad-cli, report written, board untouched: nonzero exit, bytes kept."""
        cli, root = pinned_cli
        text = _board_text()
        seed = root / "preserve-seed.kicad_pcb"
        seed.write_text(text)
        assert _kct_zones_fill(str(seed)) == (0, True)
        seed_sha = _sha(seed)
        assert _front_copper_mm2(cli, seed, root / "preserve-seed-plot") == pytest.approx(
            self.CONTROLS["wired"][1], abs=0.001
        )

        # In place: the known-good filled board is the prior copper.
        in_place = root / "preserve-in-place.kicad_pcb"
        in_place.write_bytes(seed.read_bytes())
        status, requested = _kct_zones_fill(
            str(in_place), lose_first_probe=True, first_save_writes=False
        )
        assert status != 0 and requested

        # Separate output over a pre-existing, previously filled destination.
        source = root / "preserve-source.kicad_pcb"
        source.write_text(text)
        source_sha = _sha(source)
        dest = root / "preserve-dest.kicad_pcb"
        dest.write_bytes(seed.read_bytes())
        status, requested = _kct_zones_fill(
            str(source), "--output", str(dest), lose_first_probe=True, first_save_writes=False
        )
        assert status != 0 and requested
        assert (_sha(in_place), _sha(dest)) == (seed_sha, seed_sha)
        assert _sha(source) == source_sha

    def test_separate_output_fills_a_new_and_a_prefilled_destination(self, pinned_cli):
        root = pinned_cli[1]
        source = root / "output-source.kicad_pcb"
        source.write_text(_board_text())
        source_sha = _sha(source)
        dest = root / "output-dest.kicad_pcb"

        status, requested = _kct_zones_fill(
            str(source), "--output", str(dest), lose_first_probe=True
        )
        assert (status, requested) == (0, True)
        assert _sha(source) == source_sha
        polygons = _saved_polygons(dest)
        assert polygons, "the new destination holds no fill"

        # Again over the destination, which now already carries a fill.
        status, requested = _kct_zones_fill(
            str(source), "--output", str(dest), lose_first_probe=True
        )
        assert (status, requested) == (0, True)
        assert _saved_polygons(dest) == polygons
        assert _sha(source) == source_sha

    @pytest.mark.skipif(os.name == "nt", reason="uses POSIX file modes")
    @pytest.mark.parametrize("malformed", [False, True], ids=["valid", "parse-error"])
    def test_readonly_source_with_private_output(self, pinned_cli, malformed, capsys):
        """A separate fill can read a 0444 source and only publish verified copper."""
        root = pinned_cli[1] / ("readonly-invalid" if malformed else "readonly-valid")
        root.mkdir()
        src = root / "source.kicad_pcb"
        dest = root / "private.kicad_pcb"
        board = _board_text()
        # Seed an actual, distinguishable filled destination.
        dest.write_text(_board_text(tracks=False))
        assert _kct_zones_fill(str(dest)) == (0, True)
        old_copper = dest.read_bytes()
        assert _saved_polygons(dest)
        if malformed:
            board = board[:-1] + "(unrecognized_test_record 1))"
        src.write_text(board)
        src.chmod(0o444)
        dest.chmod(0o600)
        capsys.readouterr()

        status, requested = _kct_zones_fill(str(src), "--output", str(dest))

        assert requested
        assert src.read_text() == board
        assert stat.S_IMODE(src.stat().st_mode) == 0o444
        if malformed:
            assert status != 0
            assert "unrecognized_test_record" in capsys.readouterr().err
            assert dest.read_bytes() == old_copper
        else:
            assert status == 0
            assert _saved_polygons(dest)
            assert dest.read_bytes() != old_copper
        assert stat.S_IMODE(dest.stat().st_mode) == 0o600

    def test_zoneless_board_still_fills(self, pinned_cli):
        """No pour to save: the completed write alone is the success criterion.

        The second run starts from kicad-cli's own serialization, so the save
        rewrites byte-identical content and only the file's mtime moves.
        """
        root = pinned_cli[1]
        board = root / "zoneless.kicad_pcb"
        board.write_text(_board_text(zone=False))
        for attempt in ("first", "resaved"):
            status, requested = _kct_zones_fill(str(board), lose_first_probe=attempt == "resaved")
            assert (status, requested) == (0, True), attempt
        assert _saved_polygons(board) == []
