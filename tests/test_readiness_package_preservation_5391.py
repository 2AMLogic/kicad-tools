"""Readiness operates on snapshots and preserves already shipped releases."""

import json

import pytest

from kicad_tools.cli import readiness_cmd as cmd
from tests.test_kct_readiness_cmd import FakeEngines, make_board


def inventory(board):
    return {
        p.relative_to(board).as_posix(): p.read_bytes() for p in board.rglob("*") if p.is_file()
    }


def test_default_verification_never_exports_or_rewrites_sources(tmp_path):
    board = make_board(tmp_path)
    before = inventory(board)
    fake = FakeEngines(board)
    rc = cmd.main([str(board), "--mfr", "jlcpcb"], engines=fake.bundle())
    assert rc != 0  # A missing finished package cannot earn READY.
    assert "export" not in fake.calls
    for name, data in before.items():
        assert (board / name).read_bytes() == data
    assert not (board / "output/manufacturing.zip").exists()
    assert not (board / "output/readiness.json").exists()


@pytest.mark.parametrize(
    "operation,diagnostic_name",
    [("--verify", "readiness-verification"), ("--generate", "readiness-attempt")],
)
def test_published_fill_snapshots_follow_the_current_run(tmp_path, operation, diagnostic_name):
    class WritesLocalSettings(FakeEngines):
        def native_drc(self, pcb, report_path):
            if pcb.parent.name == "fill-refilled":
                pcb.with_suffix(".kicad_prl").write_text("native local settings")
            return super().native_drc(pcb, report_path)

    board = make_board(tmp_path)
    output = board / "output"
    diagnostics = output / diagnostic_name
    notes = diagnostics / "refilled-native" / "operator"
    notes.mkdir(parents=True)
    note = notes / "review.txt"
    note.write_text("keep this note")
    for revision in ("rev_a", "rev_b"):
        pcb = output / f"{revision}.kicad_pcb"
        pcb.write_text("(kicad_pcb original)\n")
        pcb.with_suffix(".kicad_pro").write_text('{"board": {}}\n')
        pcb.with_suffix(".kicad_dru").write_text("(version 1)\n")

    for revision, include_rule in (("rev_a", True), ("rev_b", True), ("rev_b", False)):
        pcb = output / f"{revision}.kicad_pcb"
        if not include_rule:
            pcb.with_suffix(".kicad_dru").unlink()
        engines = WritesLocalSettings(board, native_errors=1).bundle()
        assert (
            cmd.main([str(pcb), operation, "--pcb-only", "--mfr", "jlcpcb"], engines=engines) == 1
        )
        receipt = json.loads((diagnostics / "fill-consistency.json").read_text())
        expected = {pcb.name, pcb.with_suffix(".kicad_pro").name}
        if include_rule:
            expected.add(pcb.with_suffix(".kicad_dru").name)
        assert set(receipt["context_sha256"]) == expected
        for name in ("fill-saved", "fill-refilled"):
            snapshot = diagnostics / name
            assert {p.name for p in snapshot.iterdir()} == expected
        assert note.read_text() == "keep this note"


def test_failed_fill_snapshot_install_restores_both_snapshots_and_receipts(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "diagnostics"
    for name in ("fill-saved", "fill-refilled"):
        snapshot = source / name
        snapshot.mkdir(parents=True)
        (snapshot / "board").write_text("first")
    (source / "fill-consistency.json").write_text('{"run": 1}')
    cmd._publish_diagnostics(source, destination, {"run": 1})
    before = inventory(destination)
    for name in ("fill-saved", "fill-refilled"):
        (source / name / "board").write_text("second")
    (source / "fill-consistency.json").write_text('{"run": 2}')

    real_replace = cmd.os.replace

    def fail_after_first_install(src, dst):
        if src.parent.name == "incoming" and dst == destination / "fill-refilled":
            raise OSError("injected snapshot installation failure")
        return real_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(cmd.os, "replace", fail_after_first_install)
        with pytest.raises(OSError, match="injected snapshot installation failure"):
            cmd._publish_diagnostics(source, destination, {"run": 2})
    assert inventory(destination) == before

    # A subsequent refusal before the fill gate has no replayable snapshots.
    cmd._publish_diagnostics(None, destination, {"run": 3})
    assert {p.name for p in destination.iterdir()} == {"readiness.json"}
    assert json.loads((destination / "readiness.json").read_text()) == {"run": 3}


def test_busy_diagnostic_publisher_reports_blocked_and_keeps_previous_run(tmp_path, capsys):
    board = make_board(tmp_path)
    diagnostics = board / "output/readiness-verification"
    diagnostics.mkdir(parents=True)
    previous = diagnostics / "readiness.json"
    previous.write_text('{"run": 1}')
    with cmd._publication_lock(diagnostics / ".readiness-publish.lock"):
        exit_code = cmd.main(
            [str(board), "--verify", "--mfr", "jlcpcb", "--format", "json"],
            engines=FakeEngines(board).bundle(),
        )
        assert exit_code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == cmd.STATUS_BLOCKED
        assert "publisher holds the release lock" in payload["error"]
        assert "report_path" not in payload
        assert previous.read_text() == '{"run": 1}'


def test_generation_failure_before_fill_keeps_prior_release_evidence_out_of_attempt(
    tmp_path, monkeypatch
):
    board = make_board(tmp_path)
    release = board / "output/readiness"
    for name in ("fill-saved", "fill-refilled"):
        (release / name).mkdir(parents=True)
        (release / name / "retired.kicad_pcb").write_text("old board")
    (release / "fill-consistency.json").write_text('{"revision": "retired"}')
    previous = inventory(release)

    def fail_before_fill(options, engines):
        raise OSError("failed before the fill gate")

    monkeypatch.setattr(cmd, "_run_readiness_candidate", fail_before_fill)
    engines = FakeEngines(board).bundle()
    assert cmd.main([str(board), "--generate", "--mfr", "jlcpcb"], engines=engines) == 1
    attempt = board / "output/readiness-attempt"
    assert {p.name for p in attempt.iterdir()} == {"readiness.json"}
    assert "failed before the fill gate" in (attempt / "readiness.json").read_text()
    assert inventory(release) == previous


def test_failed_generation_preserves_previous_release(tmp_path):
    board = make_board(tmp_path)
    old = board / "output/manufacturing"
    old.mkdir()
    (old / "manifest.json").write_text(json.dumps({"producer": "kct readiness"}))
    (old / "README.txt").write_text("prior release")
    (board / "output/manufacturing.zip").write_bytes(b"old archive")
    (board / "output/readiness.json").write_text("prior evidence")
    before = inventory(board)
    fake = FakeEngines(board, native_errors=1)
    rc = cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=fake.bundle())
    assert rc != 0
    for name, data in before.items():
        assert (board / name).read_bytes() == data


def test_generation_refuses_to_replace_recipe_package(tmp_path):
    board = make_board(tmp_path)
    old = board / "output/manufacturing"
    old.mkdir()
    (old / "README.txt").write_text("Epoxy-filled & Capped (POFV); J1 manual assembly")
    before = inventory(board)
    fake = FakeEngines(board)
    assert cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=fake.bundle()) != 0
    assert "export" not in fake.calls
    for name, data in before.items():
        assert (board / name).read_bytes() == data


def test_refill_divergence_is_real_and_preserved(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)
    fake.fill_areas = lambda p: {"F.Cu": 2.0 if "refilled" in p.read_text() else 1.0}
    before = (board / "output/demo_routed.kicad_pcb").read_bytes()
    assert cmd.main([str(board), "--mfr", "jlcpcb"], engines=fake.bundle()) != 0
    evidence = json.loads(
        (board / "output/readiness-verification/fill-consistency.json").read_text()
    )
    assert evidence["deltas_mm2"] == {"F.Cu": 1.0}
    assert (board / "output/demo_routed.kicad_pcb").read_bytes() == before


def test_generated_package_can_be_verified_without_changing_shipped_bytes(tmp_path):
    board = make_board(tmp_path)
    assert (
        cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=FakeEngines(board).bundle())
        == 0
    )
    before = inventory(board)
    fake = FakeEngines(board)
    assert cmd.main([str(board), "--mfr", "jlcpcb"], engines=fake.bundle()) == 0
    assert "export" not in fake.calls
    for name, data in before.items():
        assert (board / name).read_bytes() == data


def test_consistent_manifest_cannot_hide_missing_project_rules(tmp_path):
    import zipfile

    board = make_board(tmp_path)
    assert (
        cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=FakeEngines(board).bundle())
        == 0
    )
    args = cmd.build_parser().parse_args([str(board), "--mfr", "jlcpcb"])
    opts, _ = cmd.resolve_options(args)
    archive = opts.output_dir / "kicad_project.zip"
    with zipfile.ZipFile(archive) as zf:
        files = {name: zf.read(name) for name in zf.namelist() if not name.endswith(".kicad_dru")}
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    cmd._write_full_manifest(opts, {})
    cmd._build_archive(opts)
    before = inventory(board)
    assert cmd.main([str(board), "--mfr", "jlcpcb"], engines=FakeEngines(board).bundle()) != 0
    report = json.loads((board / "output/readiness-verification/readiness.json").read_text())
    assert any(".kicad_dru is absent" in problem for problem in report["blockers"])
    for name, data in before.items():
        assert (board / name).read_bytes() == data


def test_publication_failure_rolls_back_all_changed_files(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    for name in ("a", "b", "c"):
        (old / name).write_text("old " + name)
        (new / name).write_text("new " + name)
    before = inventory(old)
    real_replace = cmd.os.replace
    calls = []

    def fail_second(source, destination):
        calls.append(destination)
        if len(calls) == 2:
            raise OSError("injected publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(cmd.os, "replace", fail_second)
    import pytest

    with pytest.raises(OSError, match="injected"):
        cmd._publish_candidate(new, old, cmd._snapshot_inventory(old))
    assert inventory(old) == before


def test_export_cannot_change_checked_project_rules(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)
    original_export = fake.export

    def mutate_rules(pcb, mfr, output, assembly):
        pcb.with_suffix(".kicad_pro").write_text('{"changed":true}')
        return original_export(pcb, mfr, output, assembly)

    fake.export = mutate_rules
    before = inventory(board)
    assert cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=fake.bundle()) != 0
    for name, data in before.items():
        assert (board / name).read_bytes() == data


def test_flat_recipe_archive_is_verified_with_exact_bytes(tmp_path):
    import zipfile

    board = make_board(tmp_path)
    assert (
        cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=FakeEngines(board).bundle())
        == 0
    )
    bundle = board / "output/manufacturing"
    with zipfile.ZipFile(board / "output/manufacturing.zip", "w") as zf:
        for path in bundle.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(bundle).as_posix())
    assert cmd.main([str(board), "--mfr", "jlcpcb"], engines=FakeEngines(board).bundle()) == 0


def test_manual_and_smt_bom_overlap_is_rejected(tmp_path):
    board = make_board(tmp_path)
    assert (
        cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=FakeEngines(board).bundle())
        == 0
    )
    args = cmd.build_parser().parse_args([str(board), "--mfr", "jlcpcb"])
    opts, _ = cmd.resolve_options(args)
    (opts.output_dir / "manual-assembly-bom.csv").write_text("Designator,LCSC Part #\nD1,C87271\n")
    cmd._write_full_manifest(opts, {})
    cmd._build_archive(opts)
    before = inventory(board)
    assert cmd.main([str(board), "--mfr", "jlcpcb"], engines=FakeEngines(board).bundle()) != 0
    report = json.loads((board / "output/readiness-verification/readiness.json").read_text())
    assert any("manual assembly parts also occur" in message for message in report["blockers"])
    for name, data in before.items():
        assert (board / name).read_bytes() == data


@pytest.mark.parametrize("atomic_replacement", [False, True])
def test_edit_to_later_file_survives_publication_conflict(
    tmp_path, monkeypatch, atomic_replacement
):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    for name in ("a", "b"):
        (old / name).write_text("old " + name)
        (new / name).write_text("new " + name)
    real_replace = cmd.os.replace
    injected = False

    def concurrent_edit(source, destination):
        nonlocal injected
        real_replace(source, destination)
        if not injected:
            injected = True
            if atomic_replacement:
                # Same bytes, different actual file: identity must still detect it.
                other = tmp_path / "editor-save"
                other.write_text("old b")
                real_replace(other, old / "b")
            else:
                (old / "b").write_text("human edit")

    monkeypatch.setattr(cmd.os, "replace", concurrent_edit)
    with pytest.raises(RuntimeError, match="Concurrent edit"):
        cmd._publish_candidate(new, old, cmd._snapshot_inventory(old))
    assert (old / "a").read_text() == "old a"
    assert (old / "b").read_text() == ("old b" if atomic_replacement else "human edit")
    assert not list(old.glob(".readiness-*"))


@pytest.mark.parametrize("deleted", [False, True])
def test_rollback_preserves_editor_change_to_published_file(tmp_path, monkeypatch, deleted):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    for name in ("a", "b"):
        (old / name).write_text("old " + name)
        (new / name).write_text("new " + name)
    real_link = cmd.os.link
    calls = 0

    def edit_then_fail(source, destination):
        nonlocal calls
        if destination not in (old / "a", old / "b"):
            return real_link(source, destination)
        calls += 1
        if calls == 2:
            raise OSError("injected second replacement failure")
        real_link(source, destination)
        if calls == 1:
            if deleted:
                (old / "a").unlink()
            else:
                (old / "a").write_text("human edit")

    monkeypatch.setattr(cmd.os, "link", edit_then_fail)
    with pytest.raises(RuntimeError, match="rollback preserved concurrent edits"):
        cmd._publish_candidate(new, old, cmd._snapshot_inventory(old))
    if deleted:
        assert not (old / "a").exists()
    else:
        assert (old / "a").read_text() == "human edit"
    assert (old / "b").read_text() == "old b"
    assert not list(old.glob(".readiness-*"))


def test_publishers_for_same_release_are_serialized(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "a").write_text("old")
    (new / "a").write_text("new")
    before = cmd._snapshot_inventory(old)
    real_replace = cmd.os.replace

    nested = False

    def second_publisher(source, destination):
        nonlocal nested
        if nested:
            real_replace(source, destination)
            return
        nested = True
        with pytest.raises(RuntimeError, match="publisher holds the release lock"):
            cmd._publish_candidate(new, old, before)
        assert (old / "a").read_text() == "old"
        real_replace(source, destination)

    monkeypatch.setattr(cmd.os, "replace", second_publisher)
    cmd._publish_candidate(new, old, before)
    assert (old / "a").read_text() == "new"
    assert not (tmp_path / ".old.readiness-publish.lock").exists()


def test_edit_after_inventory_does_not_become_expected_state(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "a").write_text("old")
    (new / "a").write_text("candidate")
    before = cmd._snapshot_inventory(old)
    real_inventory = cmd._snapshot_inventory

    def edit_after_inventory(root, *args):
        result = real_inventory(root, *args)
        if root == old:
            (old / "a").write_text("human edit")
        return result

    monkeypatch.setattr(cmd, "_snapshot_inventory", edit_after_inventory)
    with pytest.raises(RuntimeError, match="Concurrent edit while identifying"):
        cmd._publish_candidate(new, old, before)
    assert (old / "a").read_text() == "human edit"
