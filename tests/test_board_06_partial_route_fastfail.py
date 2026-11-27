"""Partial-route fast-fail gate for board 06 (diffpair-test) — issue #4066.

Sibling of ``tests/test_board_03_copper_lvs.py::TestBoard03PartialRouteFastFail``.
Under concurrent CPU load the wall-clock ``--timeout`` safety backstop can fire
before every signal net lands, so ``route_pcb`` returns ``False``.  Without the
#4066 gate the ``--step all`` branch fell through to
``write_lvs_report(require_clean=True)`` (re-enabled by #4013), which raised
``BoardNetlistMismatch`` on the unrouted net's copper OPEN and surfaced as a
misleading LVS failure.

SCOPE: the #4066 gate is confined to the ``--step all`` branch.  The separate
``--step route`` branch is the Phase 4N CI re-route path (#2677) that TOLERATES
a partial route by design and does not call LVS; it must NOT gain the gate.
These tests therefore invoke ``main()`` with the default ``--step all`` (no
``--step route``) so they exercise exactly the branch that got the gate.

Fast and hermetic: monkeypatch the recipe's module-level functions so
``main()`` runs without the router, ``kicad-cli``, or the LVS comparator.

Issue #10308 extends this file with the full-recipe stage-ordering contract:
``--step all`` must grade the artifact it DELIVERS -- ``run_drc`` runs after
``export_manufacturing_bundle`` (whose silk legalisation pass finalises the
routed PCB and whose manifest the full checker verifies), never before it.
The #4066 partial-route gate and the clean-LVS gate stay ahead of the export,
and an export failure still fails the recipe even when an older package is
present.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from kicad_tools.lvs import BoardNetlistMismatch

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "06-diffpair-test"


def _load_board06_module():
    """Import the board-06 ``generate_design.py`` recipe module."""
    gen = BOARD_DIR / "generate_design.py"
    spec = importlib.util.spec_from_file_location("board06_generate_design", gen)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestBoard06PartialRouteFastFail:
    """A partial route (``--step all``) fails fast, not with an LVS trace."""

    def _stub_pipeline_prefix(self, module, monkeypatch, tmp_path: Path) -> None:
        sch = tmp_path / "diffpair_test.kicad_sch"
        sch.write_text("(kicad_sch)")
        pcb = tmp_path / "diffpair_test.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        monkeypatch.setattr(module, "create_project", lambda *a, **k: tmp_path / "p.kicad_pro")
        monkeypatch.setattr(module, "create_schematic", lambda *a, **k: sch)
        monkeypatch.setattr(module, "create_pcb", lambda *a, **k: pcb)

    def _forbid_downstream(self, module, monkeypatch) -> None:
        def _boom(name):
            def _raise(*a, **k):
                raise AssertionError(
                    f"{name} ran despite a partial route -- the route_success "
                    "gate (#4066) did not short-circuit the pipeline"
                )

            return _raise

        monkeypatch.setattr(module, "run_drc", _boom("run_drc"))
        monkeypatch.setattr(module, "write_lvs_report", _boom("write_lvs_report"))
        monkeypatch.setattr(
            module, "export_manufacturing_bundle", _boom("export_manufacturing_bundle")
        )

    def test_partial_route_fails_fast_with_distinct_message(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        module = _load_board06_module()
        self._stub_pipeline_prefix(module, monkeypatch, tmp_path)
        self._forbid_downstream(module, monkeypatch)
        monkeypatch.setattr(module, "route_pcb", lambda *a, **k: False)
        # Default --step all (NOT --step route): exercise the gated branch.
        monkeypatch.setattr(module.sys, "argv", ["generate_design.py", str(tmp_path / "out")])

        rc = module.main()

        assert rc == 1, "partial route must make main() exit non-zero"
        err = capsys.readouterr().err
        assert "partial route" in err.lower(), (
            "partial-route failure must be reported with a distinct 'partial "
            f"route' message, got stderr:\n{err}"
        )
        assert "per-pair a* budget" in err.lower()
        assert "BoardNetlistMismatch" not in err, (
            "a partial route must NOT surface as an LVS BoardNetlistMismatch"
        )

    def test_full_route_still_reaches_lvs_gate(self, monkeypatch, tmp_path: Path) -> None:
        # Issue #6724: route_pcb is stubbed, so the routed board is never
        # written and a real kicad-cli cannot load it.  That load failure
        # used to read as a clean native DRC; stub the leg as clean instead.
        from kicad_tools.drc.geometric import GeometricDRCResult, SavedFillCheck

        monkeypatch.setattr(
            "kicad_tools.recipes.gate.run_geometric_drc",
            lambda *a, **k: GeometricDRCResult(ran=True),
        )
        monkeypatch.setattr(
            "kicad_tools.recipes.gate.check_saved_fill", lambda *a, **k: SavedFillCheck(ran=True)
        )
        module = _load_board06_module()
        self._stub_pipeline_prefix(module, monkeypatch, tmp_path)
        monkeypatch.setattr(module, "route_pcb", lambda *a, **k: True)
        monkeypatch.setattr(module, "run_drc", lambda *a, **k: True)

        lvs_called: list[bool] = []

        def _fake_lvs(*a, **k):
            lvs_called.append(True)
            return (True, True)

        monkeypatch.setattr(module, "write_lvs_report", _fake_lvs)
        monkeypatch.setattr(module, "export_manufacturing_bundle", lambda *a, **k: True)
        monkeypatch.setattr(module.sys, "argv", ["generate_design.py", str(tmp_path / "out")])

        rc = module.main()

        assert lvs_called == [True], (
            "a full (N==M) route must still reach write_lvs_report -- the "
            "#4066 fast-fail gate must not fire on a complete route"
        )
        assert rc == 0


# --- Issue #10308: grade the delivered artifact, in delivery order ------------

# Bytes a stubbed ``route_pcb`` writes, and the different bytes a stubbed
# ``export_manufacturing_bundle`` leaves behind: the real export's silk
# legalisation pass rewrites the routed PCB, so a checker that runs at the
# right point must observe the POST-export bytes.
PRE_EXPORT_PCB_BYTES = b"(kicad_pcb routed, silk not yet legalised)"
POST_EXPORT_PCB_BYTES = b"(kicad_pcb routed, silk legalised by export)"


def _stub_gate_legs_clean(monkeypatch) -> None:
    """Stub the shared gate's kicad-cli DRC legs clean (issue #6724 pattern).

    ``route_pcb`` is stubbed by these tests, so no routed board a real
    kicad-cli could load ever exists; a load failure must not masquerade
    as a clean native DRC verdict.
    """
    from kicad_tools.drc.geometric import GeometricDRCResult, SavedFillCheck

    monkeypatch.setattr(
        "kicad_tools.recipes.gate.run_geometric_drc",
        lambda *a, **k: GeometricDRCResult(ran=True),
    )
    monkeypatch.setattr(
        "kicad_tools.recipes.gate.check_saved_fill", lambda *a, **k: SavedFillCheck(ran=True)
    )


class TestBoard06FinalArtifactGrading:
    """``--step all`` grades the post-export PCB and manufacturing bundle.

    Issue #10308: ``run_drc`` used to run BEFORE the export, so the recipe
    graded a different artifact from the one it delivers -- no manifest
    existed yet, and the silk legalisation performed by ``kct export``
    had not run.  These tests pin the delivery order (route -> clean LVS ->
    export -> full check, exactly one checker invocation) and the early
    exits around it.
    """

    def _stub_prefix(self, module, monkeypatch, tmp_path: Path, out: Path) -> None:
        sch = tmp_path / "diffpair_test.kicad_sch"
        sch.write_text("(kicad_sch)")
        pcb = tmp_path / "diffpair_test.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        monkeypatch.setattr(module, "create_project", lambda *a, **k: tmp_path / "p.kicad_pro")
        monkeypatch.setattr(module, "create_schematic", lambda *a, **k: sch)
        monkeypatch.setattr(module, "create_pcb", lambda *a, **k: pcb)
        out.mkdir(parents=True, exist_ok=True)

    def _stub_route(self, module, monkeypatch, calls: list[str]) -> None:
        def _fake_route(_src, dst, *a, **k):
            dst.write_bytes(PRE_EXPORT_PCB_BYTES)
            calls.append("route")
            return True

        monkeypatch.setattr(module, "route_pcb", _fake_route)

    def _stub_clean_lvs(self, module, monkeypatch, calls: list[str]) -> None:
        def _fake_lvs(*a, **k):
            calls.append("lvs")
            return (True, True)

        monkeypatch.setattr(module, "write_lvs_report", _fake_lvs)

    def _stub_export(self, module, monkeypatch, calls: list[str], *, succeed: bool = True) -> None:
        """Export stub: finalise the PCB bytes and write a fresh manifest.

        On ``succeed=False`` it returns False WITHOUT touching the routed
        PCB or the bundle on disk (a real failed export leaves whatever an
        earlier run wrote).
        """

        def _fake_export(routed_path, output_dir, *a, **k):
            calls.append("export")
            if not succeed:
                return False
            routed_path.write_bytes(POST_EXPORT_PCB_BYTES)
            mfg = output_dir / "manufacturing"
            mfg.mkdir(parents=True, exist_ok=True)
            (mfg / "manifest.json").write_text('{"files": {}}')
            return True

        monkeypatch.setattr(module, "export_manufacturing_bundle", _fake_export)

    def _stub_check(
        self, module, monkeypatch, calls: list[str], checker_seen: list, verdict: bool
    ) -> None:
        """``run_drc`` stub recording WHAT it observed at call time.

        Records (pcb path, pcb bytes, manifest-exists) so the assertions run
        AFTER ``main()`` -- an assert raised inside the stub would be
        swallowed by ``main()``'s broad ``except`` and misread as ``rc == 1``.
        """

        def _fake_check(pcb_path, *a, **k):
            calls.append("check")
            p = Path(pcb_path)
            manifest = p.parent / "manufacturing" / "manifest.json"
            checker_seen.append((p, p.read_bytes() if p.is_file() else None, manifest.is_file()))
            return verdict

        monkeypatch.setattr(module, "run_drc", _fake_check)

    def _run_main(self, module, monkeypatch, out: Path) -> int:
        monkeypatch.setattr(module.sys, "argv", ["generate_design.py", str(out)])
        return module.main()

    def test_check_runs_after_export_on_finalized_pcb(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        _stub_gate_legs_clean(monkeypatch)
        module = _load_board06_module()
        out = tmp_path / "out"
        self._stub_prefix(module, monkeypatch, tmp_path, out)
        routed = out / "diffpair_test_routed.kicad_pcb"

        calls: list[str] = []
        checker_seen: list[tuple[Path, bytes | None, bool]] = []
        self._stub_route(module, monkeypatch, calls)
        self._stub_clean_lvs(module, monkeypatch, calls)
        self._stub_export(module, monkeypatch, calls)
        self._stub_check(module, monkeypatch, calls, checker_seen, verdict=True)

        rc = self._run_main(module, monkeypatch, out)

        assert calls == ["route", "lvs", "export", "check"], (
            "the full recipe must run route -> clean LVS -> export -> full check"
        )
        assert len(checker_seen) == 1, "exactly one checker invocation"
        assert checker_seen[0][0] == routed.resolve()
        assert checker_seen[0][1] == POST_EXPORT_PCB_BYTES, (
            "the checker must grade the PCB bytes export finalised, not the "
            "pre-export snapshot (#10308)"
        )
        assert checker_seen[0][2] is True, (
            "the checker must see the manifest export just wrote, at "
            f"{routed.parent / 'manufacturing' / 'manifest.json'}"
        )
        assert rc == 0
        assert "Overall: PASS" in capsys.readouterr().out

    def test_failing_final_check_fails_summary_and_exit(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        _stub_gate_legs_clean(monkeypatch)
        module = _load_board06_module()
        out = tmp_path / "out"
        self._stub_prefix(module, monkeypatch, tmp_path, out)

        calls: list[str] = []
        checker_seen: list[tuple[Path, bytes | None, bool]] = []
        self._stub_route(module, monkeypatch, calls)
        self._stub_clean_lvs(module, monkeypatch, calls)
        self._stub_export(module, monkeypatch, calls)
        self._stub_check(module, monkeypatch, calls, checker_seen, verdict=False)

        rc = self._run_main(module, monkeypatch, out)

        assert calls == ["route", "lvs", "export", "check"]
        assert checker_seen[0][1] == POST_EXPORT_PCB_BYTES
        assert rc != 0, "a failed final check must fail the recipe"
        stdout = capsys.readouterr().out
        assert "DRC:     FAIL" in stdout
        assert "Overall: FAIL" in stdout
        assert "kct check --mfr jlcpcb reported errors" in stdout, (
            "the failing supplemental verdict must be explained in the SUMMARY"
        )

    def test_lvs_failure_skips_export_and_final_check(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        _stub_gate_legs_clean(monkeypatch)
        module = _load_board06_module()
        out = tmp_path / "out"
        self._stub_prefix(module, monkeypatch, tmp_path, out)
        self._stub_route(module, monkeypatch, [])

        def _raising_lvs(*a, **k):
            raise BoardNetlistMismatch("copper OPEN on net USB3_TX1+")

        monkeypatch.setattr(module, "write_lvs_report", _raising_lvs)

        reached: list[str] = []

        def _forbid(stage):
            def _inner(*a, **k):
                reached.append(stage)
                raise AssertionError(f"{stage} ran despite a failed copper LVS")

            return _inner

        monkeypatch.setattr(module, "export_manufacturing_bundle", _forbid("export"))
        monkeypatch.setattr(module, "run_drc", _forbid("check"))

        rc = self._run_main(module, monkeypatch, out)

        assert rc != 0
        assert reached == [], (
            "a failed copper LVS must short-circuit before export and the full check"
        )
        assert "BoardNetlistMismatch" in capsys.readouterr().err

    def test_export_failure_fails_recipe_despite_older_package(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        _stub_gate_legs_clean(monkeypatch)
        module = _load_board06_module()
        out = tmp_path / "out"
        self._stub_prefix(module, monkeypatch, tmp_path, out)
        # An older package from a previous run is still on disk; the failed
        # export below must not touch it.
        mfg = out / "manufacturing"
        mfg.mkdir()
        (mfg / "manifest.json").write_text('{"files": {}}')

        calls: list[str] = []
        checker_seen: list[tuple[Path, bytes | None, bool]] = []
        self._stub_route(module, monkeypatch, calls)
        self._stub_clean_lvs(module, monkeypatch, calls)
        self._stub_export(module, monkeypatch, calls, succeed=False)
        # The check still passes against the older package (e.g. a
        # deterministic re-run whose PCB bytes still hash-match it); the
        # export verdict alone must fail the recipe.
        self._stub_check(module, monkeypatch, calls, checker_seen, verdict=True)

        rc = self._run_main(module, monkeypatch, out)

        assert rc != 0, (
            "an export failure must fail the recipe even when an older "
            "package lets the full check pass"
        )
        stdout = capsys.readouterr().out
        assert "MFG bundle: FAILED" in stdout
        assert "Overall: FAIL" in stdout
