"""Issue #9946: ``_kicad_drc_report(refill=True)`` never silently skips the refill.

The refill run used to gate ``--refill-zones --save-board`` on the capability
probe alone, so an *inconclusive* probe (killed, timed-out or garbled
``--help``) read as "unsupported" and the "refill" became a plain read-only
DRC: the starved-thermal remediation never baked its forced-solid overrides
in and nothing reported it.  The shared policy has lived in
``run_kicad_drc_command`` since #9938 -- pass the flags unless the probe
*definitively* said "no", retry once without them on an ``Unknown argument:
--refill-zones`` rejection (memoising the answer), WARN when the refill could
not run -- and this call site now uses it.
"""

from __future__ import annotations

import json
import logging
import stat
import sys
from pathlib import Path

import pytest

from kicad_tools.cli import runner

KICAD9_HELP = (
    "Usage: drc [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE] "
    "[--format FORMAT] [--all-track-errors] [--schematic-parity] [--units UNITS] "
    "[--severity-all] [--severity-error] [--severity-warning] "
    "[--severity-exclusions] [--exit-code-violations] INPUT_FILE\n"
)
KICAD10_HELP = KICAD9_HELP + "  --refill-zones          Refill zones before running DRC\n"

VALID_REPORT = json.dumps(
    {
        "coordinate_units": "mm",
        "date": "2026-11-22T00:00:00+0000",
        "kicad_version": "10.0.6",
        "source": "b.kicad_pcb",
        "violations": [],
        "unconnected_items": [],
        "schematic_parity": [],
    }
)

#: A board whose net table the fake kicad-cli can strip and the snapshot /
#: restore pass must put back.
BOARD = '(kicad_pcb (version 20240108) (generator "pcbnew") (net 0 "") (net 1 "GND"))\n'

#: What the fake kicad-cli's ``--save-board`` leaves behind: no net
#: declarations, the quirk ``_restore_net_declarations`` repairs.
NETS_STRIPPED = '(kicad_pcb (version 20240108) (generator "pcbnew"))\n'


def _script_kicad_cli(
    tmp_path: Path,
    *,
    help_text: str = KICAD10_HELP,
    kill_help: bool = False,
    rejects: tuple[str, ...] = (),
    strip_nets: bool = False,
    kill_drc: bool = False,
    reject_after_write: bool = False,
    plain_output: str = "report",
) -> Path:
    """A real executable kicad-cli stand-in driven through real ``subprocess``.

    ``--help`` prints ``help_text`` (or SIGKILLs itself when ``kill_help``,
    making the capability probe inconclusive).  Any argument in ``rejects``
    is rejected KiCad-9 style: ``Unknown argument: <opt>`` plus usage on
    stdout, exit 1, no report, no board write.  ``--save-board`` strips the
    board's net declarations when ``strip_nets``; ``kill_drc`` SIGKILLs the
    DRC before writing anything.  ``reject_after_write`` makes a rejecting
    launch first leave a complete report and strip the nets (a partial write);
    ``plain_output`` is what a launch *without* ``--refill-zones`` writes:
    ``"report"``, ``"none"`` or an ``"{}"``-style raw string.  Every launch
    logs the net declarations it found in the board.
    """
    log = tmp_path / "argv.log"
    cli = tmp_path / "kicad-cli"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys\n"
        f"log, rejects, help_text, report = {str(log)!r}, {rejects!r}, {help_text!r}, "
        f"{VALID_REPORT!r}\n"
        f"kill_help, strip_nets, kill_drc, stripped = "
        f"{kill_help!r}, {strip_nets!r}, {kill_drc!r}, {NETS_STRIPPED!r}\n"
        f"reject_after_write, plain_output = {reject_after_write!r}, {plain_output!r}\n"
        "argv = sys.argv[1:]\n"
        "open(log, 'a').write(json.dumps(argv) + '\\n')\n"
        "if '--help' in argv:\n"
        "    if kill_help:\n"
        "        os.kill(os.getpid(), signal.SIGKILL)\n"
        "    sys.stdout.write(help_text); sys.exit(0)\n"
        "if '--help' not in argv:\n"
        "    n = open(argv[-1]).read().count('(net ')\n"
        "    open(log + '.nets', 'a').write(str(n) + '\\n')\n"
        "for arg in argv:\n"
        "    if arg in rejects:\n"
        "        if reject_after_write:\n"
        "            open(argv[argv.index('--output') + 1], 'w').write(report)\n"
        "            open(argv[-1], 'w').write(stripped)\n"
        "        sys.stdout.write('Unknown argument: ' + arg + '\\n' + help_text); sys.exit(1)\n"
        "if kill_drc:\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
        "if strip_nets and '--save-board' in argv:\n"
        "    open(argv[-1], 'w').write(stripped)\n"
        "if '--refill-zones' not in argv and plain_output != 'report':\n"
        "    if plain_output != 'none':\n"
        "        open(argv[argv.index('--output') + 1], 'w').write(plain_output)\n"
        "    sys.exit(0)\n"
        "open(argv[argv.index('--output') + 1], 'w').write(report)\n"
    )
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli


def _logged_argvs(tmp_path: Path) -> list[list[str]]:
    lines = (tmp_path / "argv.log").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _drc_runs(tmp_path: Path) -> list[list[str]]:
    return [argv for argv in _logged_argvs(tmp_path) if "--help" not in argv]


def _probes(tmp_path: Path) -> list[list[str]]:
    return [argv for argv in _logged_argvs(tmp_path) if "--help" in argv]


def _board(tmp_path: Path) -> Path:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(BOARD)
    return pcb


def _net_count(pcb: Path) -> int:
    return len(runner._snapshot_net_declarations(pcb))


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    """The probe memoises per kicad-cli path; keep tests independent."""
    runner._kicad_drc_supports_refill.cache_clear()
    yield
    runner._kicad_drc_supports_refill.cache_clear()


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")
class TestInconclusiveProbe:
    """A killed ``--help`` probe must not degrade the refill to read-only."""

    def test_flags_are_passed_despite_the_unknown_answer(self, tmp_path):
        cli = _script_kicad_cli(tmp_path, kill_help=True)
        pcb = _board(tmp_path)

        report = runner._kicad_drc_report(pcb, cli, refill=True)

        assert isinstance(report, dict) and report["violations"] == []
        (run,) = _drc_runs(tmp_path)
        assert "--refill-zones" in run and "--save-board" in run
        assert len(_probes(tmp_path)) == 1, "an inconclusive probe is not memoised"

    def test_rejection_is_retried_once_without_the_flags_and_memoised(self, tmp_path, caplog):
        cli = _script_kicad_cli(tmp_path, kill_help=True, rejects=("--refill-zones",))
        pcb = _board(tmp_path)

        with caplog.at_level(logging.WARNING):
            report = runner._kicad_drc_report(pcb, cli, refill=True)

        assert isinstance(report, dict) and report["violations"] == []
        runs = _drc_runs(tmp_path)
        assert len(runs) == 2, "exactly one retry, never a loop"
        first, second = runs
        assert "--refill-zones" in first and "--save-board" in first
        assert "--refill-zones" not in second and "--save-board" not in second
        assert any("WITHOUT --refill-zones" in r.getMessage() for r in caplog.records)
        # The rejection is definitive and memoised: the next refill request
        # goes straight to the no-flags command, with no new probe.
        runner._kicad_drc_report(pcb, cli, refill=True)
        runs = _drc_runs(tmp_path)
        assert len(runs) == 3 and "--refill-zones" not in runs[2]
        assert len(_probes(tmp_path)) == 1
        assert runner._kicad_drc_supports_refill(cli) is False
        assert runner._kicad_drc_supports_refill.inconclusive(cli) is False

    def test_stripped_net_table_is_restored_around_the_flags_run(self, tmp_path):
        cli = _script_kicad_cli(tmp_path, kill_help=True, strip_nets=True)
        pcb = _board(tmp_path)

        report = runner._kicad_drc_report(pcb, cli, refill=True)

        assert isinstance(report, dict)
        assert _net_count(pcb) == 2, "--save-board stripping must be repaired"

    def test_net_table_survives_the_retried_run(self, tmp_path):
        cli = _script_kicad_cli(
            tmp_path, kill_help=True, rejects=("--refill-zones",), strip_nets=True
        )
        pcb = _board(tmp_path)

        report = runner._kicad_drc_report(pcb, cli, refill=True)

        assert isinstance(report, dict)
        assert _net_count(pcb) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")
class TestDefinitiveProbe:
    def test_kicad10_still_gets_the_flags(self, tmp_path):
        cli = _script_kicad_cli(tmp_path)
        pcb = _board(tmp_path)

        report = runner._kicad_drc_report(pcb, cli, refill=True)

        assert isinstance(report, dict) and report["violations"] == []
        (run,) = _drc_runs(tmp_path)
        assert "--refill-zones" in run and "--save-board" in run

    def test_kicad9_runs_without_flags_and_warns(self, tmp_path, caplog):
        cli = _script_kicad_cli(tmp_path, help_text=KICAD9_HELP)
        pcb = _board(tmp_path)

        with caplog.at_level(logging.WARNING):
            report = runner._kicad_drc_report(pcb, cli, refill=True)

        assert isinstance(report, dict) and report["violations"] == []
        (run,) = _drc_runs(tmp_path)
        assert "--refill-zones" not in run and "--save-board" not in run
        assert any(
            r.levelno == logging.WARNING and "WITHOUT --refill-zones" in r.getMessage()
            for r in caplog.records
        )


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")
class TestKilledRun:
    def test_killed_drc_is_no_report_and_leaves_the_nets(self, tmp_path):
        cli = _script_kicad_cli(tmp_path, kill_drc=True)
        pcb = _board(tmp_path)

        report = runner._kicad_drc_report(pcb, cli, refill=True)

        assert report is None
        assert _net_count(pcb) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")
def test_refill_false_never_passes_the_flags_or_probes(tmp_path):
    cli = _script_kicad_cli(tmp_path, kill_help=True)
    pcb = _board(tmp_path)

    report = runner._kicad_drc_report(pcb, cli, refill=False)

    assert isinstance(report, dict) and report["violations"] == []
    (run,) = _drc_runs(tmp_path)
    assert "--refill-zones" not in run and "--save-board" not in run
    assert _probes(tmp_path) == [], "refill=False must not probe at all"


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")
class TestRetryReportIsFresh:
    """Only a complete report from the *final* launch is accepted."""

    @pytest.mark.parametrize("plain_output", ["none", "{}", '{"violations": 3}', "[1]"])
    def test_first_attempts_report_never_stands_in_for_the_retry(self, tmp_path, plain_output):
        cli = _script_kicad_cli(
            tmp_path,
            kill_help=True,
            rejects=("--refill-zones",),
            reject_after_write=True,
            plain_output=plain_output,
        )
        pcb = _board(tmp_path)

        assert runner._kicad_drc_report(pcb, cli, refill=True) is None
        assert _net_count(pcb) == 2

    def test_retry_sees_the_restored_net_table(self, tmp_path):
        cli = _script_kicad_cli(
            tmp_path,
            kill_help=True,
            rejects=("--refill-zones",),
            reject_after_write=True,
        )
        pcb = _board(tmp_path)

        report = runner._kicad_drc_report(pcb, cli, refill=True)

        assert isinstance(report, dict) and report["violations"] == []
        seen = (tmp_path / "argv.log.nets").read_text().split()
        assert seen[-1] == "2", "the retry must read the restored net table"
        assert _net_count(pcb) == 2

    def test_fresh_clean_report_with_empty_violations_succeeds(self, tmp_path):
        cli = _script_kicad_cli(tmp_path, plain_output='{"violations": []}', help_text=KICAD9_HELP)
        pcb = _board(tmp_path)

        assert runner._kicad_drc_report(pcb, cli, refill=True) == {"violations": []}

    def test_inspecting_argv_after_a_successful_write_preserves_the_report(
        self, tmp_path, monkeypatch
    ):
        """Issue #9959: ``build_cmd`` is side-effect-free, so the parity-style
        argv inspection (calling ``build_cmd`` inside a spy) never deletes a
        report a launch already wrote.  The per-launch reset lives in
        ``run_kicad_drc_command``."""
        cli = _script_kicad_cli(tmp_path)
        pcb = _board(tmp_path)
        orig = runner.run_kicad_drc_command
        checks: dict = {}

        def spy(build_cmd, kicad_cli, **kwargs):
            proc, refilled = orig(build_cmd, kicad_cli, **kwargs)
            argv = build_cmd(False)  # inspect argv after the launch wrote
            out = Path(argv[argv.index("--output") + 1])
            checks["written"] = out.read_text() == VALID_REPORT
            build_cmd(True)
            build_cmd(False)
            checks["survives"] = out.read_text() == VALID_REPORT
            return proc, refilled

        monkeypatch.setattr(runner, "run_kicad_drc_command", spy)

        report = runner._kicad_drc_report(pcb, cli, refill=True)

        assert isinstance(report, dict) and report["violations"] == []
        assert checks["written"] is True, "argv inspection must not delete the report"
        assert checks["survives"] is True
