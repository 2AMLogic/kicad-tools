"""``kicad_project.zip`` delivers one KiCad project that opens (Issue #10108).

KiCad pairs a board with the root sheet of the same stem, and the project
manager opens ``<name>.kicad_pro`` with ``<name>.kicad_sch`` and
``<name>.kicad_pcb``. A revisioned board (``board_v07.kicad_pcb``) checked with
an unversioned root sheet therefore shipped an archive KiCad could not open.
The package now delivers the project, root sheet, board and rules under the
root sheet's basename, records the renaming in ``VERSION.md`` and keeps
provenance bound to the checked sources.

The fixture ``tests/fixtures/project_archive_10108`` is a KiCad-readable
design: a root sheet holding one ``Power`` sub-sheet with R1 and R2 from a
project-local symbol library, a board with matching ``local:R`` footprints,
and a ``board_v07`` project file carrying a stale ``template.kicad_pro``
filename, a ``Power`` net class for VCC and a custom rule that widens the
clearance of that net class.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from kicad_tools.cli import readiness_cmd
from kicad_tools.cli.check_cmd import _manifest_subcheck
from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.export.manufacturing import _create_project_zip
from kicad_tools.export.project_identity import (
    ProjectIdentityError,
    archive_layout,
    archived_pcb_member,
    project_identity,
    with_project_filename,
)
from tests.test_kct_readiness_cmd import FakeEngines, run
from tests.test_readiness_project_root_5813 import make_project

FIXTURE = Path(__file__).parent / "fixtures" / "project_archive_10108"
ROOT = "board.kicad_sch"
RULE = "Power_net_keeps_its_distance"
CHECKED = ("board_v07.kicad_pcb", "board_v07.kicad_pro", "board_v07.kicad_dru")
LIBRARIES = ("fp-lib-table", "local.kicad_sym", "local.pretty/R.kicad_mod", "sym-lib-table")
PROJECT_FILES = ("board.kicad_dru", "board.kicad_pcb", "board.kicad_pro", "board.kicad_sch")


def staged_design(tmp_path: Path, *, matched: bool = False) -> Path:
    """A readiness board tree whose ``output/`` holds the fixture design."""
    output = tmp_path / "board" / "output"
    shutil.copytree(FIXTURE, output)
    if matched:
        for checked in CHECKED:
            (output / checked).rename(output / checked.replace("board_v07", "board"))
    (output.parent / "project.kct").write_text(
        "kct_version: '1.0'\nproject:\n  name: t\n"
        "requirements:\n  manufacturing:\n    target_fab: jlcpcb\n"
    )
    return output.parent


def generate(board: Path, *extra: str, engines: FakeEngines | None = None) -> dict:
    code, report = run(board, engines or FakeEngines(board), "--pcb-only", *extra)
    assert code == 0, report.get("blockers")
    return report


def verify(board: Path, *extra: str) -> int:
    return readiness_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--verify", "--pcb-only", *extra],
        engines=FakeEngines(board).bundle(),
    )


def options_for(board: Path, *extra: str) -> readiness_cmd.ReadinessOptions:
    args = readiness_cmd.build_parser().parse_args(
        [str(board), "--mfr", "jlcpcb", "--pcb-only", *extra]
    )
    options, error = readiness_cmd.resolve_options(args)
    assert error is None, error
    return options


def archive_members(manufacturing: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(manufacturing / "kicad_project.zip") as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def reseal(options: readiness_cmd.ReadinessOptions, change) -> None:
    """Rewrite ``kicad_project.zip`` with *change*, then re-hash the bundle."""
    members = archive_members(options.output_dir)
    change(members)
    archive = options.output_dir / "kicad_project.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    readiness_cmd._write_full_manifest(options, {})
    readiness_cmd._build_archive(options)


def move_sheets(design: Path) -> Path:
    sheets = design / "sheets"
    sheets.mkdir()
    for name in (ROOT, "power.kicad_sch"):
        (design / name).rename(sheets / name)
    return sheets


def packaged(tmp_path: Path, *, separate_sheets: bool = False) -> Path:
    """The real exporter's archive, normalized and verified as readiness does."""
    board = staged_design(tmp_path)
    design = board / "output"
    sheets = move_sheets(design) if separate_sheets else design
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _create_project_zip(design / CHECKED[0], bundle, schematic_path=sheets / ROOT)
    options = options_for(board, "--sch", str(sheets / ROOT))
    options.output_dir = bundle
    options.operation = "generate"
    readiness_cmd._normalize_project_archive(options)
    assert readiness_cmd._verify_project_provenance(options) == []
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(bundle / "kicad_project.zip") as zf:
        zf.extractall(extracted)
    return extracted


def native_bill_of_materials(directory: Path) -> dict[str, tuple[str, str]]:
    target = directory.parent / "bom.csv"
    process = subprocess.run(
        [
            str(find_kicad_cli()),
            "sch",
            "export",
            "bom",
            "--fields",
            "Reference,Value,Footprint",
            "-o",
            str(target),
            str(directory / ROOT),
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert process.returncode == 0, process.stderr
    rows = list(csv.reader(target.read_text().splitlines()))
    return {row[0]: (row[1], row[2]) for row in rows[1:]}


def native_parity(directory: Path, board: str) -> tuple[dict, str]:
    """kicad-cli parity on *board*, with no schematic named on the command line."""
    report = directory.parent / "parity.json"
    process = subprocess.run(
        [
            str(find_kicad_cli()),
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "--schematic-parity",
            "-o",
            str(report),
            str(directory / board),
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(report.read_text()), process.stdout


def custom_rule_findings(report: dict) -> list[str]:
    return [
        v["description"]
        for v in report["violations"]
        if v["type"] == "clearance" and RULE in v["description"]
    ]


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def test_versioned_board_is_delivered_under_the_root_sheet_basename(tmp_path):
    board = staged_design(tmp_path)
    output = board / "output"
    editable = {name: (output / name).read_bytes() for name in (*CHECKED[1:], ROOT, *LIBRARIES)}

    generate(board, "--sch", str(output / ROOT))

    members = archive_members(output / "manufacturing")
    assert sorted(members) == sorted(["VERSION.md", "power.kicad_sch", *PROJECT_FILES, *LIBRARIES])
    project = json.loads(members["board.kicad_pro"])
    assert project["meta"]["filename"] == "board.kicad_pro"
    assert project["net_settings"]["netclass_assignments"] == {"VCC": "Power"}
    assert members["board.kicad_dru"] == editable["board_v07.kicad_dru"]
    version = members["VERSION.md"].decode()
    assert "Working revision: `v07`" in version
    assert "| `board.kicad_pcb` | `board_v07.kicad_pcb` |" in version
    assert "| `board.kicad_sch` | `board.kicad_sch` |" in version
    # Packaging renames in the archive only.
    assert {name: (output / name).read_bytes() for name in editable} == editable
    assert not (output / "board.kicad_pro").exists()
    assert not (output / "board.kicad_pcb").exists()
    assert not (output / "VERSION.md").exists()


def test_already_matched_inputs_keep_their_names(tmp_path):
    board = staged_design(tmp_path, matched=True)
    output = board / "output"
    (output / "VERSION.md").write_text("# Revisions\n\n- v07: first board\n")

    generate(board)

    members = archive_members(output / "manufacturing")
    assert sorted(members) == sorted(["VERSION.md", "power.kicad_sch", *PROJECT_FILES, *LIBRARIES])
    assert members["VERSION.md"] == b"# Revisions\n\n- v07: first board\n"
    assert json.loads(members["board.kicad_pro"])["meta"]["filename"] == "board.kicad_pro"
    assert json.loads((output / "board.kicad_pro").read_text())["meta"]["filename"] == (
        "template.kicad_pro"
    )
    for name in ("board.kicad_dru", "board.kicad_sch", "power.kicad_sch"):
        assert members[name] == (output / name).read_bytes()


def test_record_kept_beside_a_versioned_board_travels_with_the_renaming(tmp_path):
    board = staged_design(tmp_path)
    (board / "output" / "VERSION.md").write_text("# Revisions\n\n- v07: first board\n")

    generate(board, "--sch", str(board / "output" / ROOT))

    version = archive_members(board / "output" / "manufacturing")["VERSION.md"].decode()
    assert "## Record kept beside the checked board\n\n# Revisions\n\n- v07: first board" in version
    assert "Working revision: `v07`" in version


def test_export_that_picked_another_project_identity_is_corrected(tmp_path):
    """An explicit ``--sch`` wins over the root the exporter discovered."""
    board = staged_design(tmp_path, matched=True)
    output = board / "output"
    (output / "VERSION.md").write_text("# Revisions\n\n- v07: first board\n")

    class OtherIdentity(FakeEngines):
        def export(self, pcb, mfr, output_dir, assembly, **kwargs):
            result = super().export(pcb, mfr, output_dir, assembly, **kwargs)
            archive = output_dir / "kicad_project.zip"
            with zipfile.ZipFile(archive) as zf:
                members = {name: zf.read(name) for name in zf.namelist()}
            for name in PROJECT_FILES[:3]:
                del members[name]
            members["widget.kicad_pcb"] = b"(kicad_pcb stale)\n"
            members["widget.kicad_pro"] = b'{"meta": {"filename": "widget.kicad_pro"}}'
            members["VERSION.md"] = (pcb.parent / "VERSION.md").read_bytes()
            with zipfile.ZipFile(archive, "w") as zf:
                for name, data in members.items():
                    zf.writestr(name, data)
            return result

    generate(board, engines=OtherIdentity(board))

    members = archive_members(output / "manufacturing")
    assert not any(name.startswith("widget.") for name in members)
    assert members["VERSION.md"] == b"# Revisions\n\n- v07: first board\n"
    assert members["board.kicad_pcb"] == (output / "board.kicad_pcb").read_bytes()
    assert members["board.kicad_pro"] == with_project_filename(
        (output / "board.kicad_pro").read_bytes(), "board.kicad_pro"
    )


@pytest.mark.parametrize("separate_sheets", [False, True])
def test_exporter_alone_delivers_the_same_project(tmp_path, separate_sheets):
    design = tmp_path / "design"
    shutil.copytree(FIXTURE, design)
    sheets = move_sheets(design) if separate_sheets else design
    record = b"## Recorded revision\nv07\n\n"
    (design / "VERSION.md").write_bytes(record)
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    with zipfile.ZipFile(
        _create_project_zip(design / CHECKED[0], bundle, schematic_path=sheets / ROOT)
    ) as zf:
        names = sorted(zf.namelist())
        members = {name: zf.read(name) for name in names}

    assert names == sorted(["VERSION.md", "power.kicad_sch", *PROJECT_FILES, *LIBRARIES])
    assert json.loads(members["board.kicad_pro"])["meta"]["filename"] == "board.kicad_pro"
    assert members["board.kicad_pcb"] == (design / CHECKED[0]).read_bytes()
    assert members["power.kicad_sch"] == (sheets / "power.kicad_sch").read_bytes()
    assert members["VERSION.md"].endswith(record)
    assert json.loads((design / CHECKED[1]).read_text())["meta"]["filename"] == "template.kicad_pro"


def test_root_sheet_basename_outranks_stale_instance_records(tmp_path):
    """Board 04's real schematic files its instances under ``project``."""
    design = tmp_path / "design"
    design.mkdir()
    (design / "widget_v2.kicad_pcb").write_text("(kicad_pcb)")
    (design / "widget_v2.kicad_pro").write_text("{}")
    (design / "widget_v2.kicad_dru").write_text("(version 1)")
    (design / "widget.kicad_sch").write_text('(kicad_sch (project "project" (path "/")))')
    (design / "main.kicad_sch").write_text('(kicad_sch (project "template" (path "/")))')

    paired = project_identity(
        design / "widget_v2.kicad_pcb",
        design / "widget_v2.kicad_pro",
        design / "widget.kicad_sch",
    )
    assert paired.name == "widget"
    assert sorted(p.name for p, _ in paired.renamed) == sorted(
        ["widget_v2.kicad_dru", "widget_v2.kicad_pcb", "widget_v2.kicad_pro"]
    )
    assert paired.revision == "v2"

    unpaired = project_identity(
        design / "widget_v2.kicad_pcb", design / "widget_v2.kicad_pro", design / "main.kicad_sch"
    )
    assert unpaired.name == "main"
    assert project_identity(design / "widget_v2.kicad_pcb").name == "widget_v2"


def test_collision_names_both_files():
    design = Path("/design")
    identity = project_identity(
        design / "board_v07.kicad_pcb", design / "board_v07.kicad_pro", design / ROOT
    )
    with pytest.raises(
        ProjectIdentityError,
        match=(
            r"board_v07\.kicad_pro and board\.kicad_pro would both be archived as board\.kicad_pro\."
            r".*rename one of the two files$"
        ),
    ):
        archive_layout(
            identity,
            [design / "board_v07.kicad_pro", design / "board.kicad_pro"],
            lambda real: ".",
        )


def test_another_file_under_the_delivered_name_blocks_generation(tmp_path):
    board = staged_design(tmp_path)
    output = board / "output"
    generate(board, "--sch", str(output / ROOT))
    published = {
        p.relative_to(output).as_posix(): p.read_bytes()
        for p in (output / "manufacturing").rglob("*")
        if p.is_file()
    }

    class AlsoArchivesTheUnroutedProject(FakeEngines):
        def export(self, pcb, mfr, output_dir, assembly, **kwargs):
            result = super().export(pcb, mfr, output_dir, assembly, **kwargs)
            with zipfile.ZipFile(output_dir / "kicad_project.zip", "a") as zf:
                zf.writestr("board.kicad_pro", b'{"board": {"unrouted": true}}')
            return result

    code, report = run(
        board, AlsoArchivesTheUnroutedProject(board), "--pcb-only", "--sch", str(output / ROOT)
    )

    assert code != 0
    assert report["blockers"] == [
        "Project dependencies could not be collected (kicad_project.zip cannot deliver "
        "board_v07.kicad_pro as board.kicad_pro: another file is already archived under that "
        "name. Keep only the selected board's project beside it, or rename one of the two "
        "files)."
    ]
    assert {
        p.relative_to(output).as_posix(): p.read_bytes()
        for p in (output / "manufacturing").rglob("*")
        if p.is_file()
    } == published
    assert report["candidate_not_published"] is True


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_verify_accepts_a_package_that_keeps_checked_file_names(tmp_path):
    """Bundles written before Issue #10108 stay verifiable."""
    board = staged_design(tmp_path)
    output = board / "output"
    generate(board, "--sch", str(output / ROOT))
    options = options_for(board, "--sch", str(output / ROOT))

    def to_checked_names(members):
        for checked in CHECKED:
            members[checked] = (output / checked).read_bytes()
            del members[checked.replace("board_v07", "board")]
        del members["VERSION.md"]

    reseal(options, to_checked_names)
    assert readiness_cmd._verify_project_provenance(options) == []
    assert verify(board, "--sch", str(output / ROOT)) == 0
    assert _manifest_subcheck(output / CHECKED[0]).status == "PASSED"


def test_verify_accepts_another_file_under_the_delivered_project_name(tmp_path):
    """Boards 05-07 archive the unrouted original project beside the routed one."""
    board = staged_design(tmp_path)
    output = board / "output"
    generate(board, "--sch", str(output / ROOT))
    options = options_for(board, "--sch", str(output / ROOT))

    def to_checked_names_keeping_the_original(members):
        for checked in CHECKED:
            members[checked] = (output / checked).read_bytes()
            del members[checked.replace("board_v07", "board")]
        del members["VERSION.md"]
        members["board.kicad_pro"] = b'{"board": {"unrouted": true}}'

    reseal(options, to_checked_names_keeping_the_original)
    assert readiness_cmd._verify_project_provenance(options) == []
    assert verify(board, "--sch", str(output / ROOT)) == 0
    assert _manifest_subcheck(output / CHECKED[0]).status == "PASSED"


def test_verify_rejects_a_package_that_mixes_the_two_layouts(tmp_path):
    board = staged_design(tmp_path)
    output = board / "output"
    generate(board, "--sch", str(output / ROOT))
    options = options_for(board, "--sch", str(output / ROOT))

    def keep_the_project_under_its_checked_name(members):
        members[CHECKED[1]] = (output / CHECKED[1]).read_bytes()
        del members["board.kicad_pro"]

    reseal(options, keep_the_project_under_its_checked_name)
    assert readiness_cmd._verify_project_provenance(options) == [
        "kicad_project.zip keeps checked file names instead of forming the board project "
        "(board_v07.kicad_pro); regenerate the package"
    ]
    assert verify(board, "--sch", str(output / ROOT)) != 0


def test_verify_rejects_an_extra_checked_project_in_the_delivered_layout(tmp_path):
    board = staged_design(tmp_path)
    output = board / "output"
    generate(board, "--sch", str(output / ROOT))
    options = options_for(board, "--sch", str(output / ROOT))
    reseal(options, lambda members: members.update({CHECKED[1]: b'{"board": "stale"}'}))

    assert any(
        CHECKED[1] in problem for problem in readiness_cmd._verify_project_provenance(options)
    )
    assert verify(board, "--sch", str(output / ROOT)) != 0


@pytest.mark.parametrize("current_last", [False, True])
def test_normalization_rejects_duplicate_checked_members_in_both_orders(tmp_path, current_last):
    board = staged_design(tmp_path)
    output = board / "output"
    generate(board, "--sch", str(output / ROOT))
    options = options_for(board, "--sch", str(output / ROOT))
    archive = options.output_dir / "kicad_project.zip"
    current = (output / CHECKED[0]).read_bytes()
    records = (
        [b"(kicad_pcb older)\n", current] if current_last else [current, b"(kicad_pcb older)\n"]
    )
    with zipfile.ZipFile(archive, "w") as zf, pytest.warns(UserWarning, match="Duplicate name"):
        for data in records:
            zf.writestr(CHECKED[0], data)
    before = archive.read_bytes()

    with pytest.raises(
        ProjectIdentityError, match="duplicate file identities.*board_v07.kicad_pcb"
    ):
        readiness_cmd._normalize_project_archive(options)
    assert archive.read_bytes() == before


@pytest.mark.parametrize("matched", [False, True])
def test_normalization_rejects_stale_authored_revision_before_rewriting(tmp_path, matched):
    board = staged_design(tmp_path, matched=matched)
    output = board / "output"
    (output / "VERSION.md").write_text("# Working revision\nv08\n")
    generate(board, "--sch", str(output / ROOT))
    options = options_for(board, "--sch", str(output / ROOT))
    reseal(options, lambda members: members.update({"VERSION.md": b"# Working revision\nv07\n"}))
    archive = options.output_dir / "kicad_project.zip"
    before = archive.read_bytes()

    with pytest.raises(ProjectIdentityError, match="VERSION.md.*checked"):
        readiness_cmd._normalize_project_archive(options)
    assert archive.read_bytes() == before


@pytest.mark.parametrize(
    "tamper, problem",
    [
        (
            lambda members: members.__setitem__(
                "board.kicad_pcb", members["board.kicad_pcb"] + b"\n"
            ),
            "board.kicad_pcb inside kicad_project.zip differs from the checked source",
        ),
        (
            lambda members: members.__setitem__(
                "board.kicad_pro", members["board.kicad_pro"] + b"\n"
            ),
            "board.kicad_pro inside kicad_project.zip differs from the checked source",
        ),
        (
            lambda members: members.__setitem__("VERSION.md", members["VERSION.md"][:-4]),
            "VERSION.md inside kicad_project.zip does not match the checked sources",
        ),
        (
            lambda members: members.pop("VERSION.md"),
            "VERSION.md is absent from kicad_project.zip",
        ),
        (
            lambda members: members.__setitem__(
                "power.kicad_sch", members["power.kicad_sch"].replace(b"SENSE_1K", b"SENSE_9K")
            ),
            "power.kicad_sch inside kicad_project.zip differs from the checked source",
        ),
    ],
    ids=["board", "project", "record", "no-record", "child-sheet"],
)
def test_tampering_with_a_delivered_member_is_detected(tmp_path, tamper, problem):
    board = staged_design(tmp_path)
    output = board / "output"
    generate(board, "--sch", str(output / ROOT))
    options = options_for(board, "--sch", str(output / ROOT))
    assert readiness_cmd._verify_project_provenance(options) == []

    reseal(options, tamper)

    assert readiness_cmd._verify_project_provenance(options) == [problem]
    assert verify(board, "--sch", str(output / ROOT)) != 0


@pytest.mark.parametrize("name", CHECKED[1:])
def test_changing_a_checked_source_is_detected(tmp_path, name):
    board = staged_design(tmp_path)
    output = board / "output"
    generate(board, "--sch", str(output / ROOT))
    options = options_for(board, "--sch", str(output / ROOT))
    assert readiness_cmd._verify_project_provenance(options) == []

    source = output / name
    source.write_bytes(source.read_bytes() + b"\n")

    assert readiness_cmd._verify_project_provenance(options) == [
        f"{name.replace('board_v07', 'board')} inside kicad_project.zip differs from the checked source"
    ]


def test_stale_bytes_under_a_checked_name_are_moved_not_replaced(tmp_path):
    board = staged_design(tmp_path)
    manufacturing = board / "output" / "manufacturing"
    manufacturing.mkdir()
    stale = b'{"meta": {"filename": "older.kicad_pro"}}'
    with zipfile.ZipFile(manufacturing / "kicad_project.zip", "w") as zf:
        zf.writestr(CHECKED[0], b"(kicad_pcb stale)\n")
        zf.writestr(CHECKED[1], stale)
    options = options_for(board, "--sch", str(board / "output" / ROOT))

    readiness_cmd._normalize_project_archive(options)

    members = archive_members(manufacturing)
    assert CHECKED[0] not in members and CHECKED[1] not in members
    assert members["board.kicad_pcb"] == b"(kicad_pcb stale)\n"
    assert members["board.kicad_pro"] == stale.replace(b"older", b"board")
    assert readiness_cmd._verify_project_provenance(options) == [
        "board.kicad_pcb inside kicad_project.zip differs from the checked source",
        "board.kicad_pro inside kicad_project.zip differs from the checked source",
    ]


def test_kct_check_finds_the_board_under_the_delivered_name(tmp_path):
    board = staged_design(tmp_path)
    output = board / "output"
    assert _manifest_subcheck(output / CHECKED[0]).status == "NOT RUN"
    generate(board, "--sch", str(output / ROOT))

    assert _manifest_subcheck(output / CHECKED[0]).status == "PASSED"
    (output / CHECKED[0]).write_bytes(b"(kicad_pcb newer)\n")
    assert _manifest_subcheck(output / CHECKED[0]).status == "FAILED"


def test_archived_pcb_member_requires_the_project_beside_the_board():
    assert archived_pcb_member(["board.kicad_pcb", "board.kicad_pro"], Path("x.kicad_pcb")) == (
        "board.kicad_pcb"
    )
    assert archived_pcb_member(["x.kicad_pcb"], Path("x.kicad_pcb")) == "x.kicad_pcb"
    for names in (
        ["board.kicad_pcb"],
        ["board.kicad_pcb", "other.kicad_pcb"],
        ["sub/board.kicad_pcb"],
        ["board.kicad_pcb", "board.kicad_pro", "board.kicad_pcb"],
        [],
    ):
        assert archived_pcb_member(names, Path("x.kicad_pcb")) is None


# ---------------------------------------------------------------------------
# With a --project-root the renaming follows the re-rooted layout
# ---------------------------------------------------------------------------


def test_project_root_layout_renames_inside_the_re_rooted_archive(tmp_path):
    project = make_project(tmp_path)
    out = project / "pcb" / "output"
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        (out / f"board{suffix}").rename(out / f"board_v07{suffix}")
    (project / "schematics" / "board.kicad_sch").write_text(
        '(kicad_sch (sheet (property "Sheetfile" "sub.kicad_sch")))\n'
    )
    (project / "schematics" / "sub.kicad_sch").write_text("(kicad_sch sub)\n")

    rc = readiness_cmd.main(
        [
            str(out / "board_v07.kicad_pcb"),
            "--mfr",
            "jlcpcb",
            "--sch",
            str(project / "schematics" / "board.kicad_sch"),
            "--project-root",
            str(project),
            "--generate",
        ],
        engines=FakeEngines(project / "pcb").bundle(),
    )

    assert rc == 0
    with zipfile.ZipFile(project / "pcb/output/manufacturing/kicad_project.zip") as zf:
        names = set(zf.namelist())
        version = zf.read("pcb/output/VERSION.md").decode()
    assert {
        "pcb/output/board.kicad_pcb",
        "pcb/output/board.kicad_pro",
        "pcb/output/board.kicad_dru",
        "pcb/output/VERSION.md",
        "pcb/output/board.kicad_sch",
        "pcb/output/sub.kicad_sch",
        "symbols/custom.kicad_sym",
    } <= names
    assert not any("board_v07" in name for name in names)
    assert "| `board.kicad_pcb` | `board_v07.kicad_pcb` |" in version
    options = options_for(
        out / CHECKED[0],
        "--sch",
        str(project / "schematics" / ROOT),
        "--project-root",
        str(project),
    )
    archive = options.output_dir / "kicad_project.zip"
    before = archive.read_bytes()
    readiness_cmd._normalize_project_archive(options)
    assert archive.read_bytes() == before


# ---------------------------------------------------------------------------
# with_project_filename
# ---------------------------------------------------------------------------


def test_with_project_filename_changes_only_the_filename_literal():
    original = (
        b'{\n  "board": {},\n  "meta": {\n    "filename": "template.kicad_pro",\n'
        b'    "version": 3\n  },\n  "text_variables": {"note": "\\"filename\\": \\"x\\""}\n}\n'
    )
    expected = original.replace(b'"template.kicad_pro"', b'"board.kicad_pro"')

    assert with_project_filename(original, "board.kicad_pro") == expected
    assert with_project_filename(expected, "board.kicad_pro") == expected
    assert with_project_filename(original, "template.kicad_pro") == original


@pytest.mark.parametrize(
    "data",
    [
        b'{"board": {}}\n',
        b"{}\n",
        b'{"meta": {"version": 3}}',
        b'{"meta": {"filename": null}}',
        b'{"meta": "board"}',
        b"[]",
        b'{"meta": {"filename": "a.kicad_pro"',
        b"\xef\xbb\xbf",
        b"\xff\xfe",
    ],
    ids=[
        "no-meta",
        "empty",
        "no-filename",
        "null",
        "meta-not-object",
        "list",
        "invalid-json",
        "bom",
        "not-utf8",
    ],
)
def test_with_project_filename_leaves_what_it_cannot_parse(data):
    assert with_project_filename(data, "board.kicad_pro") == data


def test_with_project_filename_reserializes_when_no_literal_matches():
    escaped_key = b'{"meta": {"\\u0066ilename": "a.kicad_pro"}}'

    rewritten = with_project_filename(escaped_key, "c.kicad_pro")

    assert rewritten == b'{\n  "meta": {\n    "filename": "c.kicad_pro"\n  }\n}\n'


# ---------------------------------------------------------------------------
# Native: the extracted project is the one KiCad opens
# ---------------------------------------------------------------------------


@pytest.mark.skipif(find_kicad_cli() is None, reason="Native KiCad CLI is not installed")
class TestExtractedProjectOpensNatively:
    @pytest.mark.parametrize("separate_sheets", [False, True])
    def test_paired_names_resolve_the_child_sheet_and_its_rules(self, tmp_path, separate_sheets):
        extracted = packaged(tmp_path, separate_sheets=separate_sheets)

        report, stdout = native_parity(extracted, "board.kicad_pcb")

        assert "Found 0 schematic parity issues" in stdout
        assert report["schematic_parity"] == []
        assert native_bill_of_materials(extracted) == {
            "R1": ("SENSE_1K", "local:R"),
            "R2": ("PULL_10K", "local:R"),
        }
        assert custom_rule_findings(report)
        assert all(
            v["type"] != "clearance" or RULE in v["description"] for v in report["violations"]
        )

    def test_rules_and_net_classes_come_from_the_delivered_trio(self, tmp_path):
        extracted = packaged(tmp_path)
        assert custom_rule_findings(native_parity(extracted, "board.kicad_pcb")[0])

        rules = extracted / "board.kicad_dru"
        kept = rules.read_bytes()
        rules.unlink()
        assert custom_rule_findings(native_parity(extracted, "board.kicad_pcb")[0]) == []
        rules.write_bytes(kept)
        (extracted / "board.kicad_pro").rename(extracted / "elsewhere.kicad_pro")
        assert custom_rule_findings(native_parity(extracted, "board.kicad_pcb")[0]) == []

    @pytest.mark.parametrize("separate_sheets", [False, True])
    def test_child_sheet_mismatch_is_reported_by_parity(self, tmp_path, separate_sheets):
        extracted = packaged(tmp_path, separate_sheets=separate_sheets)
        child = extracted / "power.kicad_sch"
        child.write_text(child.read_text().replace('"R2"', '"R3"'))

        report, stdout = native_parity(extracted, "board.kicad_pcb")

        assert "Found 2 schematic parity issues" in stdout
        assert sorted(v["type"] for v in report["schematic_parity"]) == [
            "extra_footprint",
            "missing_footprint",
        ]
        assert "Missing footprint R3 (PULL_10K)" in {
            v["description"] for v in report["schematic_parity"]
        }

    def test_references_survive_instances_recorded_under_another_project_name(self, tmp_path):
        """Board 04's real schematic files its instances under a stale project name."""
        board = staged_design(tmp_path)
        child = board / "output" / "power.kicad_sch"
        child.write_text(child.read_text().replace('(project "board"', '(project "template"'))
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        _create_project_zip(
            board / "output" / CHECKED[0], bundle, schematic_path=board / "output" / ROOT
        )
        extracted = tmp_path / "extracted"
        with zipfile.ZipFile(bundle / "kicad_project.zip") as zf:
            zf.extractall(extracted)

        assert native_bill_of_materials(extracted) == {
            "R1": ("SENSE_1K", "local:R"),
            "R2": ("PULL_10K", "local:R"),
        }
        assert custom_rule_findings(native_parity(extracted, "board.kicad_pcb")[0])

    def test_checked_names_never_run_parity_so_an_empty_result_proves_nothing(self, tmp_path):
        mixed = tmp_path / "mixed"
        shutil.copytree(FIXTURE, mixed)

        report, stdout = native_parity(mixed, CHECKED[0])

        assert "schematic parity issues" not in stdout
        assert report["schematic_parity"] == []


@pytest.mark.parametrize("child_state", ["outside", "missing"])
def test_exporter_refuses_when_a_child_sheet_is_not_bundled(tmp_path, child_state):
    """Adjacent root + out-of-directory/missing child must not yield a partial ZIP."""
    design = tmp_path / "design"
    shutil.copytree(FIXTURE, design)
    child = design / "power.kicad_sch"
    if child_state == "outside":
        shared = tmp_path / "shared"
        shared.mkdir()
        child.rename(shared / "power.kicad_sch")
        root = design / ROOT
        root.write_text(root.read_text().replace("power.kicad_sch", "../shared/power.kicad_sch"))
    else:
        child.unlink()
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    with pytest.raises(ProjectIdentityError, match="--project-root"):
        _create_project_zip(design / CHECKED[0], bundle, schematic_path=design / ROOT)
    assert not (bundle / "kicad_project.zip").exists()
