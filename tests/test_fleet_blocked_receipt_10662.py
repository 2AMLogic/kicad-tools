"""``kct fleet status`` reports a board whose readiness receipt says ``blocked`` (Issue #10662).

The survey judges routing, artifacts and DRC for itself and used to read
``output/readiness.json`` only for its ``known_issues``.  So a board whose
producer had published a ``blocked`` receipt -- Board 04, blocked by the public
verifier's ``mask_to_copper`` findings -- still read ``YES*``.  An explicit
``blocked`` verdict is the board's own statement and is a blocker; a missing,
unreadable or merely ``unverified`` report still adds nothing (warn-only).
"""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.cli.fleet_cmd import _BLOCKED_RECEIPT_WIDTH, main
from tests.test_fleet_status import make_fake_board

BOARD_04 = Path(__file__).resolve().parent.parent / "boards" / "04-stm32-devboard"
BLOCKER = (
    "kct check reports 79 error(s); unaccepted error(s): "
    "mask_to_copper at 123.35/75.20 (not recorded)"
)


def _board(boards: Path, name: str, report: str | None) -> None:
    make_fake_board(boards, name, has_gerbers=True, has_bom=True, has_cpl=True, has_manifest=True)
    if report is not None:
        (boards / name / "output" / "readiness.json").write_text(report)


def _fleet(boards: Path) -> Path:
    _board(boards, "a-ready", json.dumps({"schema_version": 1, "status": "ready", "blockers": []}))
    _board(
        boards,
        "b-blocked",
        json.dumps(
            {"schema_version": 1, "status": "blocked", "blockers": [BLOCKER, "a second one"]}
        ),
    )
    _board(boards, "c-unverified", json.dumps({"status": "unverified", "blockers": ["stale"]}))
    _board(boards, "d-garbled", "{not json")
    _board(boards, "e-none", None)
    return boards


def test_blocked_receipt_is_not_ship_ready(tmp_path, capsys):
    boards = _fleet(tmp_path / "boards")
    code = main(["status", "--boards-dir", str(boards), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    by_name = {b["name"]: b for b in data["boards"]}

    assert code == 2  # b-blocked is not ship-ready
    assert by_name["b-blocked"]["ship_ready"] is False
    assert by_name["b-blocked"]["blockers"] == [f"readiness blocked: {BLOCKER}"]
    for name in ("a-ready", "c-unverified", "d-garbled", "e-none"):
        assert by_name[name]["ship_ready"] is True, name
        assert by_name[name]["blockers"] == [], name
    assert data["summary"]["ship_ready"] == 4


def test_table_quotes_the_first_recorded_blocker(tmp_path, capsys):
    boards = _fleet(tmp_path / "boards")
    main(["status", "--boards-dir", str(boards)])
    out = capsys.readouterr().out
    rows = {line.split()[0]: line for line in out.splitlines() if line and line[0] in "abcde"}
    assert f"NO  (readiness blocked: {BLOCKER})" in rows["b-blocked"]
    assert rows["a-ready"].rstrip().endswith("YES")
    assert "5 boards surveyed, 4 ship-ready" in out


def test_long_blockers_are_shortened_and_empty_ones_still_block(tmp_path, capsys):
    boards = tmp_path / "boards"
    _board(
        boards,
        "a-long",
        json.dumps({"status": "blocked", "blockers": ["x" * (_BLOCKED_RECEIPT_WIDTH + 50)]}),
    )
    _board(boards, "b-empty", json.dumps({"status": "blocked", "blockers": []}))
    _board(boards, "c-shape", json.dumps({"status": "blocked", "blockers": "oops"}))
    main(["status", "--boards-dir", str(boards), "--format", "json"])
    by_name = {b["name"]: b for b in json.loads(capsys.readouterr().out)["boards"]}

    quoted = by_name["a-long"]["blockers"][0]
    assert quoted.startswith("readiness blocked: xxx")
    assert quoted.endswith("...")
    assert len(quoted) == len("readiness blocked: ") + _BLOCKED_RECEIPT_WIDTH
    assert by_name["b-empty"]["blockers"] == ["readiness blocked"]
    assert by_name["c-shape"]["blockers"] == ["readiness blocked"]
    assert not any(b["ship_ready"] for b in by_name.values())


def test_real_board_04_is_not_ship_ready(capsys):
    """The committed Board 04 receipt says blocked, so the fleet survey says so."""
    code = main(["status", "--boards-dir", str(BOARD_04), "--format", "json"])
    (board,) = json.loads(capsys.readouterr().out)["boards"]

    assert code == 2
    assert board["ship_ready"] is False
    receipt = json.loads((BOARD_04 / "output" / "readiness.json").read_text())
    first = receipt["blockers"][0][: _BLOCKED_RECEIPT_WIDTH - 3].rstrip() + "..."
    assert board["blockers"][0] == f"readiness blocked: {first}"
    assert len(board["blockers"]) == 2
    assert board["blockers"][1].startswith("known defect #")
