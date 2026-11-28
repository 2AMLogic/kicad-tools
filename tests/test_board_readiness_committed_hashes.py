"""Committed ``boards/*/output/readiness.json`` must match the committed tree (Issue #6945).

``read_readiness()`` fails closed with "Evidence is stale" when a recorded input
hash no longer matches its file.  Nothing ran it against the real boards, so
#6876 changed board 03's ``net_class_map.json`` and routed ``.kicad_dru``
without refreshing the readiness record and CI stayed green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.board_readiness import read_readiness

BOARDS_DIR = Path(__file__).resolve().parent.parent / "boards"

# Boards whose committed readiness evidence is known to be stale, keyed by board
# directory name.  Each entry cites the issue that owns the refresh; remove the
# entry when that lands (strict xfail turns an unexpected pass into a failure).
EXPECTED_STALE: dict[str, str] = {
    # PR #9177 (issue #9143) landed the fiducials + board-ID silk on board 03
    # and refreshed its evidence, but deliberately did NOT re-issue the
    # readiness receipt: every fresh board-03 receipt is blocked (385
    # same-net mask_to_copper errors, #8900/#9144), and whether that demotes
    # board 03 from the ready set is the operator decision parked in #9144
    # (recommended: keep ready).  Until #9144 lands, main's 2026-10-21 ready
    # receipt stays committed and goes stale against the #9143 tree change.
    "03-usb-joystick": (
        "Issue #9144: board 03's tree moved in #9143 (fiducials, board-ID "
        "silk, refreshed evidence) while every fresh receipt is blocked; the "
        "pending ready/demote decision owns the re-pin. Remove when #9144 "
        "lands."
    ),
    # Issue #9097 (PR #9131) added recipe passes to board 04's
    # ``generate_design.py``; #9455 landed the via slide + silk-hole gate, and
    # #9536 regenerated the committed copper/evidence from that recipe on the
    # pinned toolchain (readiness re-pinned ``ready``).  Removed from this
    # dict by #9536.
    # Board 09's entry (issue #9495 / #9519) was removed when #9519
    # regenerated board 09's output and receipt via the #9469 pinned
    # Docker toolchain workflow after #9502 moved both producers under
    # run_on_evidence_toolchain.
    #
    # Board 02's entry (issue #9800 / #10663) was removed when #10663 gave the
    # routed project its reviewed track widths and the registered publisher
    # re-issued the package on the pinned toolchain (11 checks, ``ready``).
}

BOARDS = sorted(p.parent.parent for p in BOARDS_DIR.glob("*/output/readiness.json"))


@pytest.mark.parametrize("board", BOARDS, ids=lambda p: p.name)
def test_committed_readiness_inputs_match_tree(board: Path, request) -> None:
    if board.name in EXPECTED_STALE:
        request.applymarker(pytest.mark.xfail(reason=EXPECTED_STALE[board.name], strict=True))
    result = read_readiness(board)
    stale = [b for b in result["blockers"] if "stale" in b or "missing" in b]
    assert result["status"] != "unverified", result["blockers"]
    assert not stale, stale
