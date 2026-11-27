"""Identify routed vias in KiCad's shipped Excellon round drill hits."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from operator import itemgetter
from zipfile import BadZipFile, ZipFile

from kicad_tools.core.layers import COPPER_LAYER_ORDER, is_declared_copper_layer
from kicad_tools.schema.pcb import PCB
from kicad_tools.schema.physical_identity import physical_pad_position

Point = tuple[float, float]
Span = tuple[str, str]
_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)"
_XY = re.compile(rf"(?:X({_NUMBER}))?(?:Y({_NUMBER}))?")
_TOOL = re.compile(rf"T(\d+)C({_NUMBER})")
_UNITS = re.compile(r"(METRIC|INCH)(?:,(LZ|TZ))?(?:,(0+)\.(0+))?")
_FILENAME_SPAN = re.compile(r"-(front|back|in\d+)-(front|back|in\d+)\.drl$")
_PLATING = {"PLATED": True, "NONPLATED": False, "MIXEDPLATING": None}


@dataclass(frozen=True)
class DrillHit:
    """One round hit in the exported plot frame (millimetres, Y up)."""

    center_mm: Point
    diameter_mm: float
    tolerance_mm: Point
    plated: bool | None
    component: bool


@dataclass(frozen=True)
class ParsedDrill:
    filename: str
    units: str | None
    layer_numbers: tuple[int, int] | None  # FileFunction's one-based copper ordinals
    hits: tuple[DrillHit, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ViaMatch:
    via_index: int
    via_uuid: str
    filename: str
    center_mm: Point
    diameter_mm: float
    faces: tuple[str, ...]
    tolerance_mm: Point = (0.0, 0.0)


@dataclass(frozen=True)
class DrillCoverage:
    expected: int
    matches: tuple[ViaMatch, ...]
    reasons: tuple[str, ...]

    @property
    def matched(self) -> int:
        return len(self.matches)


def _suffix_name(filename: str, board_name: str) -> str:
    """Lowercase member name with the board's own basename removed.

    KiCad names members ``<board><suffix>.drl``; a board literally called
    ``board-PTH`` must not read its own name as a plating or span suffix.
    """
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
    stem = board_name.lower()
    return name[len(stem) :] if stem and name.startswith(stem) else name


def _filename_span(filename: str, board_name: str = "") -> Span | None:
    name = _suffix_name(filename, board_name)
    if name.endswith(("-pth.drl", "-npth.drl")):
        return "F.Cu", "B.Cu"
    if match := _FILENAME_SPAN.search(name):

        def layer(token: str) -> str:
            return {"front": "F.Cu", "back": "B.Cu"}.get(token, token.capitalize() + ".Cu")

        return layer(match[1]), layer(match[2])
    return None


@dataclass
class _Header:
    units: str | None = None
    digits: tuple[int, int] | None = None
    zeros: str | None = None  # Excellon names the zeros retained
    plating: str | None = None
    span: tuple[int, int] | None = None
    tools: dict[int, tuple[float, str | None, bool]] = field(default_factory=dict)

    @property
    def scale(self) -> float:
        return 25.4 if self.units == "inch" else 1.0

    def coordinate(self, token: str) -> tuple[float, float]:
        """Decode one axis and its half-step quantization uncertainty in mm."""
        if "." in token:
            # KiCad strips trailing decimal zeros, even down to '10.0'. Its
            # decimal output resolution is 0.001 mm or 0.0001 inch.
            precision = self.digits[1] if self.digits else (4 if self.units == "inch" else 3)
            precision = max(precision, len(token.partition(".")[2]))
            value = float(token)
        else:
            if self.digits is None:
                raise ValueError("format_unknown")
            integer, precision = self.digits
            width = integer + precision
            digits = token.lstrip("+-")
            if len(digits) > width or (len(digits) != width and self.zeros not in ("LZ", "TZ")):
                raise ValueError("format_unknown")
            if self.zeros == "LZ":
                digits = digits.ljust(width, "0")
            value = int(digits) * 10.0**-precision
            if token.startswith("-"):
                value = -value
        return value * self.scale, 0.5 * self.scale * 10.0**-precision


def _read_header(lines: list[str], filename: str, board_name: str = "") -> _Header:
    header = _Header()
    tool_plating = None
    component = False
    declared_units = None
    declared_zeros = None
    for line in lines:
        if line.startswith(";"):
            if re.search(r"FORMAT\s*=\s*\{", line):
                if unit := re.search(r"/\s*(INCH|METRIC)\s*/", line):
                    declared_units = "inch" if unit[1] == "INCH" else "mm"
                for description, retained in (
                    ("SUPPRESS TRAILING ZEROS", "LZ"),
                    ("SUPPRESS LEADING ZEROS", "TZ"),
                    ("KEEP ZEROS", "KEEP"),
                ):
                    if description in line:
                        declared_zeros = retained
            if re.search(r"/\s*(?:INCREMENTAL|RELATIVE)\s*/", line):
                raise ValueError("registration_unknown")
            if fmt := re.search(r"(?:FILE_FORMAT\s*=\s*|FORMAT\s*=\s*\{)(\d+):(\d+)", line):
                digits = int(fmt[1]), int(fmt[2])
                if header.digits is not None and header.digits != digits:
                    raise ValueError("format_unknown")
                header.digits = digits
            if "TF.FILEFUNCTION," in line:
                parts = line.split("TF.FILEFUNCTION,", 1)[1].split(",")
                if parts[0] not in _PLATING or header.plating is not None:
                    raise ValueError("plating_unknown")
                header.plating = parts[0]
                if len(parts) >= 3:
                    if not (parts[1].isdigit() and parts[2].isdigit()):
                        raise ValueError("span_unknown")
                    header.span = int(parts[1]), int(parts[2])
            if "TA.APERFUNCTION," in line:
                attrs = line.split("TA.APERFUNCTION,", 1)[1].split(",")
                tool_plating = attrs[0] if attrs[0] in _PLATING else None
                component = "COMPONENTDRILL" in attrs or "MECHANICALDRILL" in attrs
            continue
        if units := _UNITS.fullmatch(line):
            if header.units is not None:
                raise ValueError("format_unknown")
            header.units = "inch" if units[1] == "INCH" else "mm"
            header.zeros = units[2]
            if units[3]:
                digits = len(units[3]), len(units[4])
                if header.digits is not None and header.digits != digits:
                    raise ValueError("format_unknown")
                header.digits = digits
        elif tool := _TOOL.fullmatch(line):
            number, diameter = int(tool[1]), float(tool[2])
            if diameter <= 0 or number in header.tools:
                raise ValueError("format_unknown")
            header.tools[number] = diameter, tool_plating, component
            tool_plating, component = None, False
        elif line not in ("FMAT,2", "ICI,OFF"):
            raise ValueError("registration_unknown" if line == "ICI,ON" else "format_unknown")
    if header.units is None or (header.digits and max(header.digits) > 12):
        raise ValueError("format_unknown")

    if declared_units and declared_units != header.units:
        raise ValueError("format_unknown")
    if declared_zeros and header.zeros and declared_zeros != header.zeros:
        raise ValueError("format_unknown")
    header.zeros = header.zeros or declared_zeros

    # KiCad splits through holes by plating and blind/buried files by span.
    name_plating = None
    if _filename_span(filename, board_name):
        suffixed = _suffix_name(filename, board_name)
        name_plating = "NONPLATED" if suffixed.endswith("-npth.drl") else "PLATED"
    if name_plating and header.plating and header.plating != name_plating:
        raise ValueError("plating_unknown")
    header.plating = header.plating or name_plating
    return header


def parse_excellon(filename: str, content: str, board_name: str = "") -> ParsedDrill:
    """Read a KiCad drill member. Unsupported input produces a coverage reason."""
    lines = [line.strip().upper() for line in content.splitlines() if line.strip()]
    try:
        if not lines or lines[0] != "M48":
            raise ValueError("format_unknown")
        end = next((i for i, line in enumerate(lines) if line in ("%", "M95")), None)
        if end is None:
            raise ValueError("format_unknown")
        header = _read_header(lines[1:end], filename, board_name)
    except ValueError as exc:
        return ParsedDrill(filename, None, None, (), (str(exc),))

    hits: list[DrillHit] = []
    reasons: set[str] = set()
    selected = None
    routing = False
    # Axis omission in drill mode repeats the last absolute coordinate.
    axes: list[tuple[float, float] | None] = [None, None]
    for line in lines[end + 1 :]:
        if line == "M30":
            break
        if line.startswith(";") or line in ("G90", "M15", "M16"):
            continue
        if line in ("G05", "G5"):
            routing = False
            continue
        if select := re.fullmatch(r"T(\d+)", line):
            selected = header.tools.get(int(select[1]))
            continue
        if line.startswith(("G00", "G01")) or "G85" in line:
            # Routed and canned slots have endpoints, not round drill hits.
            routing = "G85" not in line
            axes = [None, None]
            continue
        coords = _XY.fullmatch(line)
        if coords is None:
            reasons.add(
                "registration_unknown"
                if line.startswith(("G91", "ICI,ON", "G92"))
                else "format_unknown"
            )
            break  # Unknown modal command may change all subsequent coordinates.
        if routing:
            continue
        try:
            for i, token in enumerate(coords.groups()):
                if token is not None:
                    axes[i] = header.coordinate(token)
        except ValueError:
            axes = [None, None]
            reasons.add("format_unknown")
            continue
        x, y = axes
        if x is None or y is None or selected is None:
            reasons.add("format_unknown")
            continue
        diameter, tool_plating, component = selected
        plating = _PLATING.get(tool_plating or header.plating or "")
        if (
            tool_plating
            and header.plating in ("PLATED", "NONPLATED")
            and tool_plating != header.plating
        ):
            plating = None
        if plating is None:
            reasons.add("plating_unknown")
        hits.append(
            DrillHit((x[0], y[0]), diameter * header.scale, (x[1], y[1]), plating, component)
        )
    else:
        reasons.add("format_unknown")  # truncated program
    return ParsedDrill(filename, header.units, header.span, tuple(hits), tuple(sorted(reasons)))


def _members(files: Mapping[str, str] | ZipFile, board_name: str = "") -> Iterator[ParsedDrill]:
    if isinstance(files, ZipFile):
        for member in files.infolist():
            if member.filename.lower().endswith((".drl", "-drl.gbr")):
                try:
                    content = files.read(member).decode("utf-8")
                except (BadZipFile, UnicodeDecodeError):
                    yield ParsedDrill(member.filename, None, None, (), ("format_unknown",))
                    continue
                yield parse_excellon(member.filename, content, board_name)
    else:
        for name, content in files.items():
            yield parse_excellon(name, content, board_name)


class _PointIndex:
    """Index physical occurrences, including independent items at the same XY."""

    def __init__(self, positions: Iterable[Point]):
        self.points = sorted((x, y, i) for i, (x, y) in enumerate(positions))

    def near(self, center: Point, tolerance: Point) -> Iterator[int]:
        x, y = center
        # A nanometre fraction absorbs float error at the half-step boundary.
        tx, ty = tolerance[0] + 1e-9, tolerance[1] + 1e-9
        start = bisect_left(self.points, x - tx, key=itemgetter(0))
        stop = bisect_right(self.points, x + tx, key=itemgetter(0))
        for _, py, index in self.points[start:stop]:
            if abs(py - y) <= ty:
                yield index


def match_drill_vias(
    board: PCB, files: Mapping[str, str] | ZipFile, *, use_aux_origin: bool | None = None
) -> DrillCoverage:
    """Match a final KiCad ZIP or named drill texts to the PCB used for export.

    Pass the actual GerberConfig.use_aux_origin. With no known origin the
    geometry cannot be registered. Returned centers stay in the drill frame.
    """
    vias = board.vias
    if use_aux_origin is None or (use_aux_origin and board.setup is None):
        return DrillCoverage(len(vias), (), ("registration_unknown",))
    ox, oy = board.setup.aux_axis_origin if use_aux_origin and board.setup else (0.0, 0.0)
    via_index = _PointIndex(via.position for via in vias)
    pads = _PointIndex(
        physical_pad_position(fp, pad)
        for fp in board.footprints
        for pad in fp.pads
        if pad.type in ("thru_hole", "np_thru_hole") and (pad.drill > 0 or pad.drill_size)
    )
    enabled = {
        layer.name
        for layer in board.layers.values()
        if is_declared_copper_layer(layer.name, layer.type)
    }
    stack = [name for name in COPPER_LAYER_ORDER if name in enabled]
    reasons: set[str] = set()
    candidates: dict[int, ViaMatch] = {}
    ambiguous: set[int] = set()
    board_name = board.path.stem if board.path else ""
    bx, by = board.board_origin
    for file in _members(files, board_name):
        reasons.update(file.reasons)
        span = _filename_span(file.filename, board_name)
        if file.layer_numbers is not None:
            first, last = file.layer_numbers
            if not (1 <= first < last <= len(stack)):
                reasons.add("span_unknown")
                continue
            declared = stack[first - 1], stack[last - 1]
            if span and span != declared:
                reasons.add("span_unknown")
                continue
            span = declared
        for hit in file.hits:
            if hit.plated is not True or hit.component:
                continue
            if span is None:
                reasons.add("span_unknown")
                continue
            # Drill frame -> sheet frame -> the board-relative frame PCB.load()
            # applies to every via and pad position.
            center = ox + hit.center_mm[0] - bx, oy - hit.center_mm[1] - by
            if next(pads.near(center, hit.tolerance_mm), None) is not None:
                continue
            owners = [
                i
                for i in via_index.near(center, hit.tolerance_mm)
                if set(vias[i].layers) == set(span)
            ]
            if len(owners) > 1:
                ambiguous.update(owners)
            for i in owners:
                faces = tuple(face for face in ("F.Cu", "B.Cu") if face in vias[i].layers)
                if i in candidates:
                    ambiguous.add(i)
                else:
                    candidates[i] = ViaMatch(
                        i,
                        vias[i].uuid,
                        file.filename,
                        hit.center_mm,
                        hit.diameter_mm,
                        faces,
                        hit.tolerance_mm,
                    )
    if ambiguous:
        reasons.add("ambiguous_hit")
    if len(candidates) < len(vias):
        reasons.add("unmatched_via")
    matches = tuple(row for i, row in sorted(candidates.items()) if i not in ambiguous)
    return DrillCoverage(len(vias), matches, tuple(sorted(reasons)))
