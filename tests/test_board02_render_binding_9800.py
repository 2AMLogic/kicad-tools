"""Board 02's publisher binds fresh renders to changed artwork (#9800).

``boards/02-charlieplex-led/package_release.py`` carries ``renders/``
forward only while the board content is unchanged from the PCB archived in
the previous package's ``kicad_project.zip``.  Since the shared silk pass
(#10001) the committed candidate PCB differs from the ready package's
archived PCB by front-silk artwork alone, so the publisher needs a second,
*changed-artwork* branch: fresh renders ship only when ``kct render`` bound
them to the checked PCB's bytes through ``renders/render-provenance.json``.

These tests replay that branch KiCad-free over fixture trees shaped like the
committed board (previous package + candidate PCB + fresh renders), plus the
committed tree itself.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "boards"))

from release_package import PIN_ANNOTATION_NODES, board_content  # noqa: E402

from kicad_tools.cli.readiness_cmd import ReadinessOptions  # noqa: E402

BOARD = REPO / "boards" / "02-charlieplex-led"
RENDERS = ("3d-back.png", "3d-front.png", "pcb-back.svg", "pcb-front.svg")
PROVENANCE = "render-provenance.json"


def _load_release_module():
    spec = importlib.util.spec_from_file_location(
        "package_release_02", BOARD / "package_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture_board(tmp_path: Path, *, silk_changed: bool, provenance: dict | None):
    """A board tree in the committed #10001 shape: a previous ready package
    archiving the pre-silk-pass PCB, a checked candidate PCB and fresh renders."""
    board = tmp_path / "02-charlieplex-led"
    output = board / "output"
    output.mkdir(parents=True)
    (board / "project.kct").write_text("kct_version: '1.0'\n")

    old_pcb = b"(kicad_pcb (fp_text reference C1 (at 1 1)))\n"
    new_pcb = old_pcb
    if silk_changed:
        new_pcb = b"(kicad_pcb (fp_text reference C1 (at 2 2)))\n"
    pcb = output / "charlieplex_3x3_routed.kicad_pcb"
    pcb.write_bytes(new_pcb)

    old_renders = {name: f"old {name}\n".encode() for name in RENDERS}
    fresh_renders = {name: f"fresh {name}\n".encode() for name in RENDERS}
    renders_dir = output / "renders"
    renders_dir.mkdir()
    for name, data in fresh_renders.items():
        (renders_dir / name).write_bytes(data)
    if provenance is not None:
        (renders_dir / PROVENANCE).write_text(json.dumps(provenance, indent=2) + "\n")

    package = output / "manufacturing"
    package.mkdir()
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(pcb.name, old_pcb)
        (package / "kicad_project.zip").write_bytes(buffer.getvalue())
    (package / "README.txt").write_text("previous ready package\n")
    for name, data in old_renders.items():
        target = package / "renders"
        target.mkdir(exist_ok=True)
        (target / name).write_bytes(data)

    options = ReadinessOptions(
        board_dir=board,
        pcb=pcb,
        schematic=None,
        project=None,
        manufacturer="jlcpcb",
        mode="generate",
        output_dir=output / "manufacturing-new",
        evidence_dir=output / "readiness",
    )
    return board, old_renders, fresh_renders, options


def _bind(options: ReadinessOptions, pcb_bytes: bytes, renders: dict[str, bytes]) -> dict:
    return {
        "generator": "kct render",
        "pcb": options.pcb.name,
        "pcb_sha256": _sha(pcb_bytes),
        "outputs": {name: _sha(data) for name, data in renders.items()},
    }


def _snapshot_old(module, options: ReadinessOptions) -> None:
    package = options.board_dir / "output" / "manufacturing"
    module.RELEASE._old = {
        p.relative_to(package).as_posix(): p.read_bytes()
        for p in sorted(package.rglob("*"))
        if p.is_file()
    }


# -- the carry-forward branch (unchanged board) ---------------------------


def test_unchanged_board_carries_renders_forward(tmp_path):
    module = _load_release_module()
    board, _old, fresh, options = _fixture_board(tmp_path, silk_changed=False, provenance=None)
    _snapshot_old(module, options)
    assert module._renders("pcb-front.svg")(options) == fresh["pcb-front.svg"]


def test_repeat_equivalent_saves_preserve_the_original_render_binding(tmp_path):
    module = _load_release_module()
    _, _, images, options = _fixture_board(tmp_path, silk_changed=False, provenance=None)
    renders = options.board_dir / "output" / "renders"
    package = options.board_dir / "output" / "manufacturing"
    binding = json.dumps(_bind(options, options.pcb.read_bytes(), images)).encode()
    (renders / PROVENANCE).write_bytes(binding)
    (package / "renders" / PROVENANCE).write_bytes(binding)
    for name, data in images.items():
        (package / "renders" / name).write_bytes(data)

    for _ in range(2):
        _snapshot_old(module, options)
        options.pcb.write_bytes(options.pcb.read_bytes() + b"\n")
        for name, data in images.items():
            assert module._renders(name)(options) == data
        assert module._render_provenance(options) == binding
        with zipfile.ZipFile(package / "kicad_project.zip", "w") as archive:
            archive.write(options.pcb, options.pcb.name)

    (renders / RENDERS[0]).write_bytes(b"different image")
    with pytest.raises(RuntimeError, match=RENDERS[0]):
        module._render_provenance(options)
    (renders / RENDERS[0]).write_bytes(images[RENDERS[0]])
    (renders / PROVENANCE).write_text(json.dumps(_bind(options, b"unrelated board", images)))
    with pytest.raises(RuntimeError, match="does not bind"):
        module._render_provenance(options)


# -- the changed-artwork branch ------------------------------------------


def test_changed_artwork_ships_provenance_bound_renders(tmp_path):
    module = _load_release_module()
    board, old, fresh, options = _fixture_board(
        tmp_path, silk_changed=True, provenance="placeholder"
    )
    options.pcb.write_bytes(options.pcb.read_bytes())  # fixture already current
    provenance = _bind(options, options.pcb.read_bytes(), fresh)
    (options.board_dir / "output" / "renders" / PROVENANCE).write_text(
        json.dumps(provenance, indent=2) + "\n"
    )
    _snapshot_old(module, options)
    for name in RENDERS:
        assert module._renders(name)(options) == fresh[name]
        assert module._renders(name)(options) != old[name]


def test_changed_artwork_refuses_without_provenance(tmp_path):
    module = _load_release_module()
    _board, _old, _fresh, options = _fixture_board(tmp_path, silk_changed=True, provenance=None)
    _snapshot_old(module, options)
    with pytest.raises(RuntimeError, match="board content changed since renders/ was made"):
        module._renders("pcb-front.svg")(options)


def test_changed_artwork_refuses_provenance_bound_to_older_pcb(tmp_path):
    module = _load_release_module()
    board, _old, fresh, options = _fixture_board(tmp_path, silk_changed=True, provenance=None)
    older = options.board_dir / "output" / "manufacturing" / "kicad_project.zip"
    with zipfile.ZipFile(older) as zf:
        old_pcb = zf.read(options.pcb.name)
    stale = _bind(options, old_pcb, fresh)
    (options.board_dir / "output" / "renders" / PROVENANCE).write_text(
        json.dumps(stale, indent=2) + "\n"
    )
    _snapshot_old(module, options)
    with pytest.raises(RuntimeError, match="board content changed since renders/ was made"):
        module._renders("pcb-front.svg")(options)


def test_changed_artwork_refuses_tampered_render(tmp_path):
    module = _load_release_module()
    board, _old, fresh, options = _fixture_board(tmp_path, silk_changed=True, provenance=None)
    provenance = _bind(options, options.pcb.read_bytes(), fresh)
    (options.board_dir / "output" / "renders" / PROVENANCE).write_text(
        json.dumps(provenance, indent=2) + "\n"
    )
    (options.board_dir / "output" / "renders" / "pcb-front.svg").write_bytes(b"hand-edited\n")
    _snapshot_old(module, options)
    with pytest.raises(RuntimeError, match="board content changed since renders/ was made"):
        module._renders("pcb-front.svg")(options)


def test_changed_artwork_refuses_missing_render(tmp_path):
    module = _load_release_module()
    board, _old, fresh, options = _fixture_board(tmp_path, silk_changed=True, provenance=None)
    provenance = _bind(options, options.pcb.read_bytes(), fresh)
    (options.board_dir / "output" / "renders" / PROVENANCE).write_text(
        json.dumps(provenance, indent=2) + "\n"
    )
    (options.board_dir / "output" / "renders" / "3d-back.png").unlink()
    _snapshot_old(module, options)
    with pytest.raises(RuntimeError, match="board content changed since renders/ was made"):
        module._renders("3d-back.png")(options)


# -- the packaged artifacts identify one build ----------------------------


def test_package_extras_ships_bound_renders_and_their_provenance(tmp_path):
    module = _load_release_module()
    board, _old, fresh, options = _fixture_board(tmp_path, silk_changed=True, provenance=None)
    provenance = _bind(options, options.pcb.read_bytes(), fresh)
    provenance_file = options.board_dir / "output" / "renders" / PROVENANCE
    provenance_file.write_text(json.dumps(provenance, indent=2) + "\n")
    _snapshot_old(module, options)

    evidence = options.evidence_dir
    evidence.mkdir(parents=True)
    options.output_dir.mkdir(parents=True)
    (evidence / "kct-check.json").write_text(json.dumps({"summary": {"errors": 0}}))
    (evidence / "native-drc.json").write_text(json.dumps({"error_count": 0}))
    # The generic producer's output (export archive, README) is already in
    # place when _package_extras runs in a real generate.
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(options.pcb.name, options.pcb.read_bytes())
        (options.output_dir / "kicad_project.zip").write_bytes(buffer.getvalue())
    (options.output_dir / "README.txt").write_text("bundle\n")

    release = module.RELEASE
    release._outcomes = []  # board-specific checks are not under test here
    release.generated = {
        **{f"renders/{n}": module._renders(n) for n in module.RENDERS},
        f"renders/{module.RENDER_PROVENANCE}": module._render_provenance,
    }
    try:
        release._package_extras(options)
    finally:
        release._outcomes = None

    shipped = options.output_dir
    for name in RENDERS:
        assert (shipped / "renders" / name).read_bytes() == fresh[name]
    # The provenance travels with the package: a fab upload carries its binding.
    assert (shipped / "renders" / PROVENANCE).read_bytes() == provenance_file.read_bytes()
    assert provenance_file in release._extra_inputs(options)
    # The gate-evidence copies the shared producer always adds are in place.
    assert (shipped / "check-report.json").is_file()
    assert (shipped / "native-drc.json").is_file()


# -- a refused run keeps the previous package reviewable ------------------


def test_refused_render_binding_keeps_the_previous_package(tmp_path):
    """A generate whose render binding refuses must fail the run and leave
    the committed package exactly as it was (#9800)."""
    import dataclasses

    from kicad_tools.cli import readiness_cmd as cmd
    from tests.test_kct_readiness_cmd import FakeEngines, make_board

    board = make_board(tmp_path)
    package = board / "output" / "manufacturing"
    package.mkdir()
    (package / "README.txt").write_text("previous ready package\n")
    (package / "manifest.json").write_text(json.dumps({"producer": "kct readiness"}))
    sentinel = package / "renders"
    sentinel.mkdir()
    (sentinel / "pcb-front.svg").write_text("previous render\n")

    def refuse_renders(options):
        raise RuntimeError("board content changed since renders/ was made; re-render it")

    engines = dataclasses.replace(
        FakeEngines(board).bundle(),
        package_extras=refuse_renders,
    )
    rc = cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=engines)
    assert rc == 1
    # Nothing was published over the previous package, and the refusal is
    # recorded in the attempt report the reviewer reads.
    assert (package / "README.txt").read_text() == "previous ready package\n"
    assert (sentinel / "pcb-front.svg").read_text() == "previous render\n"
    assert not (board / "output" / "readiness.json").exists()
    report = json.loads((board / "output" / "readiness-attempt" / "readiness.json").read_text())
    assert any("re-render it" in blocker for blocker in report["blockers"]), report["blockers"]


# -- the committed tree ----------------------------------------------------


def test_committed_renders_are_bound_to_the_candidate_pcb():
    renders_dir = BOARD / "output" / "renders"
    provenance = json.loads((renders_dir / PROVENANCE).read_text())
    pcb = BOARD / "output" / "charlieplex_3x3_routed.kicad_pcb"
    assert provenance["pcb_sha256"] == _sha(pcb.read_bytes())
    assert set(provenance["outputs"]) == set(RENDERS)
    for name in RENDERS:
        assert provenance["outputs"][name] == _sha((renders_dir / name).read_bytes())


def test_committed_state_exercises_the_changed_artwork_branch():
    pcb = BOARD / "output" / "charlieplex_3x3_routed.kicad_pcb"
    with zipfile.ZipFile(BOARD / "output" / "manufacturing" / "kicad_project.zip") as zf:
        archived = zf.read(pcb.name)
    assert board_content(archived, ignore=PIN_ANNOTATION_NODES) != board_content(
        pcb.read_bytes(), ignore=PIN_ANNOTATION_NODES
    )
    # The previous ready package stays complete and reviewable on every
    # refused run (#9800: publication waits on the warning review); its
    # renders predate the provenance sidecar, which is exactly why the
    # changed-artwork branch needs a fresh binding.
    for name in RENDERS:
        assert (BOARD / "output" / "manufacturing" / "renders" / name).is_file(), name
    for name in ("README.txt", "manifest.json", "kicad_project.zip"):
        assert (BOARD / "output" / "manufacturing" / name).is_file(), name
    assert (BOARD / "output" / "readiness.json").is_file()
    assert (BOARD / "output" / "manufacturing.zip").is_file()
