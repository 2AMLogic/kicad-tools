"""Per-item printed geometry for silk text boxes, tables and footprint fields.

The native worker only uses the public pcbnew API and the standard library.
The public item and glyph geometry preserves blank space inside a text box.
KiCad's native table DRC measures table borders. The manufactured legend
also includes the printed text of each visible cell.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.sexp.parser import SExp


def _source_items(pcb: PCB, tree: SExp, eligible: set[tuple[str, str]]):
    from kicad_tools.schema.pcb import FootprintText
    from kicad_tools.validate.rules.silkscreen import _silk_side

    def candidates():
        for node in tree.children:
            if node.name in ("gr_text_box", "table"):
                yield node, None, "table" if node.name == "table" else "text box"
            elif node.name in ("footprint", "module"):
                for child in node.children:
                    if child.name == "fp_text_box":
                        yield child, node, "text box"
                    elif child.name == "property" and child.get_string(0) not in (
                        "Reference",
                        "Value",
                    ):
                        yield child, node, "text"

    for node, parent, kind in candidates():
        layer_node = node.find_child("layer")
        layer = layer_node.get_string(0) if layer_node is not None else ""
        side = _silk_side(layer or "")
        if (side, kind) not in eligible:
            continue
        label = str(node.name)
        if parent is not None:
            if node.name == "property":
                # Reference and Value already participate in the normal silk text model.
                label = node.get_string(0) or "field"
                if FootprintText._from_property_sexp(node, label).hidden:
                    continue
            owner = pcb._get_footprint_reference(parent) or parent.get_string(0) or "footprint"
            label = f"{owner} ({label})"
        yield node, side, kind, label, layer


def text_keepout_shapes(pcb: PCB, eligible: set[tuple[str, str]], board_path: Path):
    """Yield (side, type, label, layer, shape, location, failure) for eligible text."""
    from uuid import NAMESPACE_URL, uuid5

    from shapely.affinity import translate  # type: ignore[import-untyped]

    from kicad_tools.sexp.parser import SExp, parse_string, serialize_sexp

    tree = getattr(pcb, "_sexp", None)
    if tree is None or not eligible:
        return
    items = list(_source_items(pcb, tree, eligible))
    if not items:
        return

    def cells(node):
        container = node.find_child("cells") if node.name == "table" else None
        if container is not None:
            for cell in container.find_children("table_cell"):
                span = cell.find_child("span")
                if span is None or all((span.get_int(i) or 0) > 0 for i in (0, 1)):
                    yield cell

    # Old boards may omit UUIDs. Assign only the scratch copy reproducible
    # lookup keys; never publish invented identities or mutate the caller's tree.
    if any(n.find_child("uuid") is None for node, *_ in items for n in (node, *cells(node))):
        tree = parse_string(serialize_sexp(tree))
        items = list(_source_items(pcb, tree, eligible))

    def lookup_id(node, scope):
        identity = node.find_child("uuid")
        if identity is not None:
            return identity.get_string(0) or ""
        uid = str(uuid5(NAMESPACE_URL, f"kicad-tools/silk-keepout/{scope}/{node.name}"))
        node.append(SExp.list("uuid", uid))
        return uid

    requests = []
    labels = []
    for index, (node, _side, _kind, label, _layer) in enumerate(items):
        authored = node.find_child("uuid") is not None
        uid = lookup_id(node, index)
        cell_ids = tuple(lookup_id(cell, f"{index}/{i}") for i, cell in enumerate(cells(node)))
        requests.append((uid, cell_ids))
        labels.append(f"{label} {{{uid}}}" if authored else label)
    project_path = board_path.with_suffix(".kicad_pro")
    ox, oy = pcb._board_origin
    try:
        results = _native_shapes(
            serialize_sexp(tree),
            board_path.name,
            project_path.read_bytes() if project_path.is_file() else None,
            tuple(requests),
        )
        error = ""
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        results, error = {}, str(exc)
    for (_node, side, kind, _label, layer), (uid, _cells), label in zip(
        items, requests, labels, strict=True
    ):
        if uid not in results:
            yield (
                side,
                kind,
                label,
                layer,
                None,
                None,
                error or "KiCad could not identify this item",
            )
            continue
        geometry, (x, y) = results[uid]
        yield side, kind, label, layer, translate(geometry, -ox, -oy), (x - ox, y - oy), ""


@lru_cache(maxsize=4)
def _native_shapes(
    board: str,
    filename: str,
    project: bytes | None,
    requests: tuple[tuple[str, tuple[str, ...]], ...],
):
    """Measure one immutable board snapshot; check and repair can share identical snapshots."""
    from shapely.geometry import Polygon  # type: ignore[import-untyped]
    from shapely.ops import unary_union  # type: ignore[import-untyped]

    from kicad_tools.validate.mask_copper_geometry import _resolve_native_python_command

    with tempfile.TemporaryDirectory(prefix="kct-silk-text-") as tmp:
        stage = Path(tmp)
        command, _ = _resolve_native_python_command(None, stage)
        if command is None:
            raise RuntimeError(
                "KiCad Python is unavailable. Install KiCad or set KCT_KICAD_DOCKER=1."
            )
        source = stage / filename
        source.write_text(board)
        if project is not None:
            source.with_suffix(".kicad_pro").write_bytes(project)
        request = stage / "request.json"
        output = stage / "shapes.json"
        request.write_text(json.dumps(dict(requests)))
        worker = Path(shutil.copy(__file__, stage))
        result = subprocess.run(
            [*command, str(worker), str(source), str(request), str(output)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode or not output.is_file():
            detail = result.stderr.strip().splitlines()
            raise RuntimeError(detail[-1] if detail else f"KiCad worker exited {result.returncode}")
        raw = json.loads(output.read_text())
        return {
            uid: (
                unary_union([Polygon(shell, holes).buffer(0) for shell, holes in outlines]),
                position,
            )
            for uid, (outlines, position) in raw.items()
        }


def _worker(board_path: Path, request_path: Path, output_path: Path) -> None:
    import pcbnew  # type: ignore[import-not-found]  # Provided by the selected KiCad runtime.

    board = pcbnew.LoadBoard(str(board_path))
    project = board_path.with_suffix(".kicad_pro")
    if project.is_file():
        manager = pcbnew.GetSettingsManager()
        if not manager.LoadProject(str(project)):
            raise ValueError("KiCad could not load the project for silk text measurement")
        board.SetProject(manager.GetProject(str(project)))
    requests = json.loads(request_path.read_text())
    objects = list(board.GetDrawings())
    for footprint in board.GetFootprints():
        objects.extend(footprint.GraphicalItems())
        objects.extend(footprint.GetFields())
    shapes: dict[str, Any] = {}

    def points(chain):
        return [
            (chain.CPoint(i).x / 1e6, chain.CPoint(i).y / 1e6) for i in range(chain.PointCount())
        ]

    for item in objects:
        uid = item.m_Uuid.AsString()
        if uid not in requests:
            continue
        if uid in shapes:
            raise ValueError(f"Ambiguous silk text UUID {uid}")
        polygons = [pcbnew.SHAPE_POLY_SET()]
        if isinstance(item, pcbnew.PCB_TEXTBOX) and not item.IsBorderEnabled():
            # KiCad's effective shape includes the frame even when it doesn't plot.
            item.TransformTextToPolySet(polygons[0], 0, 100, pcbnew.ERROR_INSIDE)
        else:
            item.GetEffectiveShape(item.GetLayer()).TransformToPolygon(
                polygons[0], 100, pcbnew.ERROR_INSIDE
            )
        for cell_uid in requests[uid]:
            # KiCad 10 exposes the cell's text-box API through BOARD_ITEM. Resolve
            # it on its own board so merged-cell placement and text variables survive.
            cell = pcbnew.Cast_to_PCB_TEXTBOX(board.ResolveItem(pcbnew.KIID(cell_uid), True))
            if cell is None:
                raise ValueError(f"KiCad could not resolve table cell {cell_uid}")
            glyphs = pcbnew.SHAPE_POLY_SET()
            cell.TransformTextToPolySet(glyphs, 0, 100, pcbnew.ERROR_INSIDE)
            polygons.append(glyphs)
        outlines = [
            (
                points(polygon.COutline(i)),
                [points(polygon.CHole(i, j)) for j in range(polygon.HoleCount(i))],
            )
            for polygon in polygons
            for i in range(polygon.OutlineCount())
        ]
        pos = item.GetPosition()
        shapes[uid] = (outlines, (pos.x / 1e6, pos.y / 1e6))
    output_path.write_text(json.dumps(shapes))


if __name__ == "__main__":
    _worker(*(Path(arg) for arg in sys.argv[1:]))
