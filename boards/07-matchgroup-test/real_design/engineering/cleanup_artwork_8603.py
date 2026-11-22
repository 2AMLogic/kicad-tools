"""Finish the staged Board 07 mask-side artwork with the six-layer fab rules."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from kicad_tools.cli.pcb_add_fiducials import stamp_child_uuids
from kicad_tools.router.optimizer.pcb import _extract_balanced_blocks
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string

# Board-relative millimetres. The sixth mark clears the board's UART header
# by the 3.05 mm through-hole to SMD requirement on the assembled back side.
FIDUCIALS = {
    "F.Cu": [(3.5, 3.5), (96.5, 76.5), (8.0, 76.5)],
    "B.Cu": [(6.5, 5.0), (93.0, 76.5), (10.5, 73.0)],
}


def silk_item(child) -> bool:
    if not hasattr(child, "find_child"):
        return False
    if child.name == "property":
        return child.get_string(0) == "Reference"
    layer = child.find_child("layer")
    return layer is not None and layer.get_string(0) in {"F.SilkS", "B.SilkS"}


def silk_geometry(footprint):
    return [child for child in footprint.children if silk_item(child)]


def stock_fiducials(board: PCB) -> dict:
    """Place genuine, unnetted board-only marks from the pinned footprint library."""
    refs = []
    for layer, sites in FIDUCIALS.items():
        for x, y in sites:
            ref = f"FID{len(refs) + 1}"
            refs.append(ref)
            fp = board.add_footprint(
                "Fiducial:Fiducial_1mm_Mask2mm", ref, x, y, layer=layer, value="Fiducial"
            )
            fp.exclude_from_bom = True
            fp.exclude_from_pos_files = True
            if "board_only" not in fp._attr_unknown_tokens:
                fp._attr_unknown_tokens = [*fp._attr_unknown_tokens, "board_only"]
            if len(fp.pads) != 1 or fp.pads[0].net_name:
                raise ValueError(f"{ref} must have one unnetted physical land")
    stamp_child_uuids(board, refs)
    return {
        fp.reference: fp._sexp_node.to_string() for fp in board.footprints if fp.reference in refs
    }


def finish_board(text: str, artwork: dict, fiducials: dict) -> str:
    """Apply only legend geometry, the six mechanical marks, and the board revision."""
    parts, cursor, tail, seen = [], 0, 0, set()
    for lo, hi, raw in _extract_balanced_blocks(text, "footprint"):
        tail = hi
        fp = parse_string(raw)
        uid = fp.find_child("uuid").get_string()
        if uid not in artwork:
            continue
        if uid in seen:
            raise ValueError(f"duplicate footprint {uid}")
        seen.add(uid)
        for child in silk_geometry(fp):
            fp.children.remove(child)
        for child in artwork[uid]:
            fp.append(child)
        parts.extend([text[cursor:lo], fp.to_string()])
        cursor = hi
    if seen != set(artwork) or not tail:
        raise ValueError("expected Board 07 footprints were missing")
    parts.extend([text[cursor:tail], "\n\t", "\n\t".join(fiducials.values()), text[tail:]])
    text = "".join(parts)
    # The reviewed project.kct and physical operator legend both identify Rev B.
    ((lo, hi, raw),) = _extract_balanced_blocks(text, "title_block")
    title = parse_string(raw)
    rev = title.find_child("rev")
    if rev.get_string(0) != "1.0":
        raise ValueError(f"unexpected incoming PCB revision {rev.get_string(0)}")
    rev.set_value(0, "B")
    return "".join([text[:lo], title.to_string(), text[hi:]])


def finish(staged: Path, scratch: Path) -> None:
    candidate, source = staged / "candidate.kicad_pcb", staged / "source"
    original = candidate.read_text(encoding="utf-8")
    if "STM32F429 + IS42S16400J | Rev B" not in original:
        raise ValueError("missing reviewed Board 07 revision legend")
    proof = scratch / "artwork.kicad_pcb"
    shutil.copyfile(candidate, proof)
    for command, flags in (
        ("fix-silkscreen", ["--clip-to-holes"]),
        ("place-silk-refs", ["--over-holes", "--allow-rotate"]),
        ("fix-silkscreen", ["--all"]),
    ):
        subprocess.run(
            ["kct", command, proof, "--mfr", "jlcpcb", "--layers", "6", *flags], check=True
        )

    before = PCB.load(candidate)
    after = PCB.load(proof)
    old = {
        fp.uuid: [node.to_string() for node in silk_geometry(fp._sexp_node)]
        for fp in before.footprints
    }
    artwork = {}
    for fp in after.footprints:
        ink = silk_geometry(fp._sexp_node)
        if [node.to_string() for node in ink] != old[fp.uuid]:
            artwork[fp.uuid] = ink
    marks = stock_fiducials(after)
    for path in (candidate, source / "sdram_demo.kicad_pcb"):
        path.write_text(
            finish_board(path.read_text(encoding="utf-8"), artwork, marks), encoding="utf-8"
        )
    path = source / "circuit.json"
    circuit = json.loads(path.read_bytes())
    if circuit["pin_nets"].keys() & marks.keys():
        raise ValueError("fiducial references already exist in the original circuit")
    # The nonschematic, board-only marks each have an empty electrical map.
    circuit["pin_nets"].update({ref: {} for ref in marks})
    path.write_text(json.dumps(circuit, indent=2) + "\n", encoding="utf-8")
