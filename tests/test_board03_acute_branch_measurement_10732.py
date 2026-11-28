"""Board 03's acute-branch measurement record (Issue #10732).

``boards/03-usb-joystick/engineering/acute_branch_10732.py`` measured the
repair on the pinned KiCad toolchain and committed its JSON record and the
markdown note it renders from that record.  These tests keep the two
consistent and the record complete, without KiCad: the run itself is the
release owner's to repeat on the coordinated reissue input (#8900).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

BOARD = Path(__file__).resolve().parent.parent / "boards" / "03-usb-joystick"
ENGINEERING = BOARD / "engineering"
SCRIPT = ENGINEERING / "acute_branch_10732.py"
RECORD = ENGINEERING / "acute-branch-10732.json"
NOTE = ENGINEERING / "acute-branch-10732.md"


def _script():
    spec = importlib.util.spec_from_file_location("board03_acute_branch_10732", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_note_is_what_the_script_renders_from_the_committed_record():
    record = json.loads(RECORD.read_text())
    assert _script().render_note(record) == NOTE.read_text()


def test_record_names_its_input_toolchain_and_producer():
    record = json.loads(RECORD.read_text())
    # The published routed PCB the issue reproduces on.
    assert (
        record["input"]["sha256"]
        == "4d3ad6dad097f7722c0587649156ad504bd158510d712461ccd8c6a35b8cde7d"
    )
    assert record["toolchain"]["kicad_version"] == "10.0.6"  # the pinned evidence toolchain
    assert record["producer"]["script_sha256"]
    assert list(record["stages"]) == ["input", "dedupe", "redundant-copper", "acute-branch"]


def test_acute_branch_stage_carries_the_criterion_and_each_residuals_reason():
    """Every measurement the acceptance names is on the result, and each site
    the rule still reports has a reason."""
    record = json.loads(RECORD.read_text())
    last = record["stages"]["acute-branch"]
    for key in (
        "native_saved",
        "native_refilled",
        "pad_groups",
        "lvs",
        "usb_geometry",
        "kct_check",
    ):
        assert key in last, key
    assert last["kct_check"]["mask_assessment_coverage"] == ["complete"]
    assert last["native_saved"]["ran"] and last["native_refilled"]["ran"]
    residuals = {site["net"] for site in last["sharp_trace_corner"]}
    assert residuals == {item["net"] for item in last["unrepaired"]}
    assert all(item["reason"] for item in last["unrepaired"])
    # The tool clears most of what the redundant-copper cleanup leaves.
    before = len(record["stages"]["redundant-copper"]["sharp_trace_corner"])
    assert before == 22 and len(residuals) <= 3
