"""Public JLCPCB findings for the copper-edge controls measured with KiCad 10.0.6."""

import pytest

from scripts.research.probe_via_mask_clearance import (
    CLEARANCES,
    drc_board_text,
    kct_silk_over_copper,
)


@pytest.mark.parametrize(
    "clearance", [pytest.param(value, id=label) for label, value in CLEARANCES]
)
def test_jlcpcb_silk_at_copper_edge(tmp_path, clearance):
    board = tmp_path / "edge.kicad_pcb"
    board.write_text(drc_board_text(clearance))
    hits = kct_silk_over_copper(board)
    assert {probe: count for probe, count in hits.items() if count} == {
        "pad, thick line across centre": 1,
        "pad, thin line across centre": 1,
        "pad, thin line 0.45 mm off centre": 1,
        "pad, thin line 0.5 mm off centre": 1,
        "via, thick line across centre": 1,
        "via, thin line across centre": 1,
        "via, thin line 0.25 mm off centre": 1,
        "via, thin line 0.3 mm off centre": 1,
    }
