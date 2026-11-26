"""Operating-case current accumulation for ``path_ampacity`` (Issue #10361).

Declared current paths are checked independently by
:class:`~kicad_tools.validate.rules.path_ampacity.PathAmpacityRule`. When
several of them run **at the same time** they share copper, and that copper
carries the *signed sum* of their currents. An
:class:`~kicad_tools.router.current_paths.OperatingCase` names the paths that
are active together and their signed contributions; this module turns the
resolver's traversal evidence into the combined current on each piece of
shared copper.

Nothing here decides a verdict. It produces :class:`ElementResult` records --
the combined current (or the range it can fall in), which paths contribute,
and what is *not* determined -- and the rule turns them into findings.

Three rules keep the sums honest:

* **Direction comes from the traversal**, not from how the board stores a
  track: a span's sign is relative to the track's own start->end, so two
  paths crossing the same track in opposite directions cancel there however
  the endpoints are written.
* **Unknown direction is a range, not a guess.** Copper a path covers but
  whose traversal direction the resolver did not record (parallel groups,
  pad-internal stubs, cap joints, via arrays) contributes with either sign;
  the result is the interval of totals that remain possible.
* **Pulses combine only on declared timing.** Two pulsed members of one case
  have no determinable combined RMS or peak unless the case states how their
  pulses overlap.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

# NOTE: ``kicad_tools.router.current_paths`` is imported lazily (inside
# :func:`combine_waves`), never at module scope: ``kicad_tools.router`` imports
# from ``kicad_tools.validate`` at import time, so a module-level import here
# would be circular -- the same constraint documented in ``path_ampacity``.
if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from kicad_tools.router.current_paths import (
        CurrentPathSpec,
        PathResolution,
    )

#: Elementary intervals shorter than this (mm) are numerical noise.
_MIN_INTERVAL_MM = 1e-6
#: Unknown-direction members enumerated exhaustively (2**n sign choices).
_MAX_ENUMERATED_UNKNOWN = 10
_EPS = 1e-9


@dataclass(frozen=True)
class Wave:
    """One member's current on a piece of copper, signed along the copper.

    ``base`` is the steady (between-pulse) current; ``peak`` the pulse
    current, or ``None`` for a member that does not pulse.
    """

    path: str
    base: float
    peak: float | None = None
    duty: float | None = None

    def flipped(self) -> Wave:
        return Wave(
            self.path,
            -self.base,
            None if self.peak is None else -self.peak,
            self.duty,
        )


@dataclass(frozen=True)
class CombinedCurrent:
    """The thermal current of a set of simultaneous members on one copper.

    Attributes:
        current_a: Current the width / capacity check runs at. When RMS is
            undetermined this is the steady (DC) sum only -- never a guess at
            the pulses.
        basis: ``"continuous"``, ``"rms"`` or ``"peak-as-continuous"``.
        undetermined: Quantities the supplied data do not determine
            (``"RMS"``, ``"peak"``); empty when everything is determined.
        missing: What the author would have to state to determine them.
    """

    current_a: float
    basis: str
    undetermined: tuple[str, ...] = ()
    missing: str = ""


def combine_waves(waves: Sequence[Wave], overlap: str | None) -> CombinedCurrent:
    """Combine signed member waveforms on one piece of copper.

    Members that do not pulse are constant, so only the relative timing of
    *pulsed* members matters; a pulse riding a constant offset is fully
    determined.
    """
    from kicad_tools.router.current_paths import (
        PULSE_OVERLAP_COINCIDENT,
        PULSE_OVERLAP_DISJOINT,
    )

    base_sum = sum(w.base for w in waves)
    pulsed = [w for w in waves if w.peak is not None]
    if not pulsed:
        return CombinedCurrent(abs(base_sum), "continuous")

    def level(index: int | None) -> float:
        # Total with only pulsed member ``index`` at its peak (or none).
        if index is None:
            return base_sum
        w = pulsed[index]
        assert w.peak is not None
        return base_sum - w.base + w.peak

    if len(pulsed) == 1:
        w = pulsed[0]
        if w.duty is None:
            return CombinedCurrent(abs(level(0)), "peak-as-continuous")
        rms = math.sqrt(w.duty * level(0) ** 2 + (1 - w.duty) * base_sum**2)
        return CombinedCurrent(max(rms, abs(base_sum)), "rms")

    names = ", ".join(sorted({w.path for w in pulsed}))
    if overlap is None:
        return CombinedCurrent(
            abs(base_sum),
            "continuous",
            ("RMS", "peak"),
            f"the relative phase/overlap of the pulses of {names} ('pulse_overlap': "
            f"{PULSE_OVERLAP_COINCIDENT!r} or {PULSE_OVERLAP_DISJOINT!r})",
        )

    duties = [w.duty for w in pulsed]
    if overlap == PULSE_OVERLAP_COINCIDENT:
        # All pulses overlap, so the peaks add -- peak is determined. RMS
        # additionally needs one common pulse fraction.
        if all(d is not None for d in duties) and len({round(d, 9) for d in duties if d}) == 1:
            duty = duties[0]
            assert duty is not None
            peak_total = base_sum + sum(w.peak - w.base for w in pulsed if w.peak is not None)
            rms = math.sqrt(duty * peak_total**2 + (1 - duty) * base_sum**2)
            return CombinedCurrent(max(rms, abs(base_sum)), "rms")
        return CombinedCurrent(
            abs(base_sum),
            "continuous",
            ("RMS",),
            f"a common 'duty_cycle' on every coincident pulsed path ({names})",
        )

    # Disjoint: at most one pulse is up at a time.
    if all(d is not None for d in duties) and sum(d for d in duties if d) <= 1 + _EPS:
        total = sum(d for d in duties if d)
        mean_square = (1 - total) * base_sum**2 + sum(
            (w.duty or 0.0) * level(i) ** 2 for i, w in enumerate(pulsed)
        )
        return CombinedCurrent(max(math.sqrt(mean_square), abs(base_sum)), "rms")
    return CombinedCurrent(
        abs(base_sum),
        "continuous",
        ("RMS",),
        f"'duty_cycle' on every disjoint pulsed path ({names}), summing to at most 1",
    )


@dataclass(frozen=True)
class Cover:
    """One resolved path's coverage of one piece of copper.

    Attributes:
        kind: ``"track"``, ``"joint"``, ``"group"`` or ``"transfer"``.
        key: Identity of the physical element, shared by every path that
            covers it.
        lo, hi: Interval along the element (arc mm from a track's start;
            ``0..0`` for the others).
        sign: Traversal direction relative to the element's own orientation:
            ``+1`` / ``-1``, or ``0`` when the resolver did not establish it.
        ref: The segment / joint / group / layer change, for reporting.
        spec: The declared path covering it.
    """

    kind: str
    key: tuple
    lo: float
    hi: float
    sign: int
    ref: object
    spec: CurrentPathSpec


def build_covers(resolution: PathResolution, authored_ids: frozenset[int]) -> list[Cover]:
    """Everything a resolved path covers, with traversal direction where known.

    Directed spans come from the resolver's route traversal. Copper the path
    covers without a recorded direction (the arms of a doubled group, pad
    stubs, via-array members, cap joints) is listed with ``sign == 0``.
    Pieces that are clones of an authored track (partial intervals of an
    overlap) are covered by the traversal itself and are not repeated.
    """
    if not resolution.ok:
        return []
    spec = resolution.spec
    covers: list[Cover] = []
    group_ids = {id(seg) for group in resolution.parallel_groups for seg in group.segments}

    directed_ids: set[int] = set()
    for span in resolution.traversal:
        if id(span.segment) in group_ids:
            continue
        directed_ids.add(id(span.segment))
        covers.append(
            Cover(
                "track",
                ("track", id(span.segment)),
                span.lo,
                span.hi,
                span.sign,
                span.segment,
                spec,
            )
        )

    for group in resolution.parallel_groups:
        group_key: tuple = ("group", frozenset(id(seg) for seg in group.segments))
        covers.append(Cover("group", group_key, 0.0, 0.0, 0, group, spec))

    for seg in resolution.segments:
        if id(seg) in directed_ids or id(seg) in group_ids or id(seg) not in authored_ids:
            continue
        length = math.dist(seg.start, seg.end)
        covers.append(Cover("track", ("track", id(seg)), 0.0, length, 0, seg, spec))

    for joint in resolution.cap_joints:
        covers.append(
            Cover("joint", ("joint", id(joint.first), id(joint.second)), 0.0, 0.0, 0, joint, spec)
        )

    series = {id(change) for change in resolution.series_layer_changes}
    for change, direction in zip(
        resolution.layer_changes,
        resolution.layer_change_directions or (0,) * len(resolution.layer_changes),
        strict=False,
    ):
        if id(change) not in series:
            continue
        transfer_key: tuple = (
            "transfer",
            frozenset((change.from_layer, change.to_layer)),
            round(change.position[0], 2),
            round(change.position[1], 2),
        )
        covers.append(Cover("transfer", transfer_key, 0.0, 0.0, direction, change, spec))
    return covers


@dataclass(frozen=True)
class Member:
    """An active case member: the path, its signed current and waveform."""

    spec: CurrentPathSpec
    current_a: float

    def wave(self, sign: int) -> Wave:
        """The member's waveform seen along copper it crosses with ``sign``."""
        spec = self.spec
        peak = None
        if spec.pulsed_a is not None:
            peak = math.copysign(spec.pulsed_a, self.current_a)
        return Wave(
            spec.name, sign * self.current_a, None if peak is None else sign * peak, spec.duty_cycle
        )


@dataclass(frozen=True)
class ElementResult:
    """The combined current on one piece of copper in one case.

    Attributes:
        cover: A representative cover (kind, reference, interval).
        lo, hi: The combined current's lower and upper bound. Identical when
            every contributing direction is known.
        covers: The active members' covers of this piece.
        pulsed_undetermined: Combined quantities left undetermined.
        missing: What would determine them.
        independent_a: The highest thermal current the independent
            per-path checks already judged this copper at.
    """

    cover: Cover
    x0: float
    x1: float
    lo: CombinedCurrent
    hi: CombinedCurrent
    covers: tuple[Cover, ...]
    independent_a: float

    @property
    def direction_known(self) -> bool:
        return abs(self.lo.current_a - self.hi.current_a) <= 1e-9

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(c.spec.name for c in self.covers))


def _extremes(
    members: Sequence[tuple[Member, int]], overlap: str | None
) -> tuple[CombinedCurrent, CombinedCurrent]:
    """Lowest and highest combined current over the unknown-direction signs."""
    known = [m.wave(s) for m, s in members if s != 0]
    unknown = [m for m, s in members if s == 0]
    if not unknown:
        result = combine_waves(known, overlap)
        return result, result
    if len(unknown) > _MAX_ENUMERATED_UNKNOWN:
        choices: Iterable[tuple[int, ...]] = [(1,) * len(unknown), (-1,) * len(unknown)]
    else:
        choices = itertools.product((1, -1), repeat=len(unknown))
    results = [
        combine_waves([*known, *(m.wave(s) for m, s in zip(unknown, signs, strict=True))], overlap)
        for signs in choices
    ]
    lo = min(results, key=lambda r: r.current_a)
    hi = max(results, key=lambda r: r.current_a)
    if len(unknown) > _MAX_ENUMERATED_UNKNOWN:
        lo = CombinedCurrent(0.0, lo.basis, lo.undetermined, lo.missing)
    return lo, hi


def evaluate_case(
    members: Sequence[Member],
    covers_by_path: dict[str, list[Cover]],
    independent: dict[tuple, list[tuple[float, float, float]]],
    overlap: str | None,
) -> list[ElementResult]:
    """Combined current on every piece of copper the active members cover.

    Args:
        members: The case's active members.
        covers_by_path: Every resolved declared path's covers, by path name.
        independent: For each element key, ``(lo, hi, current_a)`` intervals
            the independent per-path checks judged it at.
        overlap: The case's ``pulse_overlap``.
    """
    by_key: dict[tuple, list[tuple[Member, Cover]]] = defaultdict(list)
    for member in members:
        for cover in covers_by_path.get(member.spec.name, ()):
            by_key[cover.key].append((member, cover))

    results: list[ElementResult] = []
    for key, entries in by_key.items():
        first = entries[0][1]
        if first.kind == "track":
            cuts = sorted({x for _, c in entries for x in (c.lo, c.hi)})
            intervals = [
                (a, b) for a, b in zip(cuts, cuts[1:], strict=False) if b - a > _MIN_INTERVAL_MM
            ]
        else:
            intervals = [(0.0, 0.0)]
        for x0, x1 in intervals:
            here = [
                (m, c)
                for m, c in entries
                if first.kind != "track"
                or (c.lo <= x0 + _MIN_INTERVAL_MM and c.hi >= x1 - _MIN_INTERVAL_MM)
            ]
            if not here:
                continue
            lo, hi = _extremes([(m, c.sign) for m, c in here], overlap)
            judged = [
                a
                for ilo, ihi, a in independent.get(key, ())
                if first.kind != "track"
                or (ilo <= x0 + _MIN_INTERVAL_MM and ihi >= x1 - _MIN_INTERVAL_MM)
            ]
            results.append(
                ElementResult(
                    cover=first,
                    x0=x0,
                    x1=x1,
                    lo=lo,
                    hi=hi,
                    covers=tuple(c for _, c in here),
                    independent_a=max(judged, default=0.0),
                )
            )
    return results
