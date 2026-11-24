"""Keep added copper and previously octilinear tracks aligned during mask repair."""

from __future__ import annotations

import math
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4

from kicad_tools.router.quantize import _is_quantum_aligned, dogleg_points, is_45_aligned
from kicad_tools.sexp import SExp

Point = tuple[float, float]


class MaskTraceAngleError(ValueError):
    """A proposed mask move cannot preserve trace angles on the saved board."""


def _child(node: SExp, tag: str) -> SExp:
    field = node.find_child(tag)
    if field is None:
        raise MaskTraceAngleError(f"track is missing its {tag}")
    return field


def _point(node: SExp, tag: str) -> Point:
    field = _child(node, tag)
    x, y = field.get_float(0), field.get_float(1)
    if x is None or y is None:
        raise MaskTraceAngleError(f"invalid track {tag} coordinates")
    return x, y


def _set_point(node: SExp, tag: str, point: Point) -> None:
    field = _child(node, tag)
    field.set_value(0, point[0])
    field.set_value(1, point[1])


def off_angle_tracks(doc: SExp) -> Counter[str]:
    """Use the file census tolerances with every parsed track, including inline records."""
    off: Counter[str] = Counter()
    for track in doc.find_children("segment"):
        start, end = _point(track, "start"), _point(track, "end")
        dx, dy = end[0] - start[0], end[1] - start[1]
        if not (_is_quantum_aligned(dx, dy) or is_45_aligned(dx, dy)):
            field = track.find_child("uuid") or track.find_child("tstamp")
            uid = field.get_string() if field is not None else None
            off[uid or track.to_string()] += 1
    return off


@dataclass(frozen=True)
class MaskTraceBend:
    """Split a track at an elbow, keeping its original UUID at the moved junction."""

    uuid: str
    added_uuid: str
    fixed_tag: str
    fixed: Point
    elbow: Point

    @classmethod
    def candidates(cls, uid: str, before: SExp, after: SExp) -> list[MaskTraceBend]:
        old_start, old_end = _point(before, "start"), _point(before, "end")
        start, end = _point(after, "start"), _point(after, "end")
        if old_start == start and old_end != end:
            tag, fixed, old, moved = "start", start, old_end, end
        elif old_end == end and old_start != start:
            tag, fixed, old, moved = "end", end, old_start, start
        else:
            raise MaskTraceAngleError(f"track {uid} has no single fixed end")

        # Keep the original approach at the fixed end. Either ordering of the
        # diagonal and axial legs connects exactly the same two endpoints.
        def departure_error(elbow: Point) -> float:
            dx, dy = elbow[0] - fixed[0], elbow[1] - fixed[1]
            return abs((old[0] - fixed[0]) * dy - (old[1] - fixed[1]) * dx) / math.hypot(dx, dy)

        elbows = [dogleg_points(*fixed, *moved, axis_first=first)[1] for first in (False, True)]
        extra = str(uuid4())
        return [
            cls(uid, extra, tag, fixed, (round(x, 6), round(y, 6)))
            for x, y in sorted(elbows, key=departure_error)
        ]

    @property
    def moved_tag(self) -> str:
        return "start" if self.fixed_tag == "end" else "end"

    def _member_lists(self, doc: SExp) -> list[SExp]:
        return [
            members
            for group in doc.find_children("group")
            for members in group.find_children("members")
            if self.uuid.lower() in {str(u).lower() for u in members.get_atoms()}
        ]

    def split(self, doc: SExp, track: SExp) -> SExp:
        # Clone all authored track attributes, including named nets and width.
        added = deepcopy(track)
        _child(added, "uuid").set_value(0, self.added_uuid)
        _set_point(added, self.moved_tag, self.elbow)
        _set_point(track, self.fixed_tag, self.elbow)
        for members in self._member_lists(doc):
            members.append(SExp.quoted_atom(self.added_uuid))
        return added

    def intact(self, tracks: dict[str, SExp]) -> bool:
        parent, extra = tracks.get(self.uuid), tracks.get(self.added_uuid)
        return (
            parent is not None
            and extra is not None
            and _point(parent, self.fixed_tag) == self.elbow
            and _point(extra, self.fixed_tag) == self.fixed
            and _point(extra, self.moved_tag) == self.elbow
        )

    def restore(self, doc: SExp, tracks: dict[str, SExp]) -> None:
        _set_point(tracks[self.uuid], self.fixed_tag, self.fixed)
        doc.children.remove(tracks[self.added_uuid])
        for members in self._member_lists(doc):
            members.children[:] = [
                atom for atom in members.children if atom.value != self.added_uuid
            ]
