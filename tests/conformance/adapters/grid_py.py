"""Group 12 -- the original Python trace-resolver retry checks (#9924).

This row measures the three ``RoutingGrid`` copper validators used by
``_TraceResolverTransaction.validate_committed_geometry`::

    validate_segment_clearance(seg, exclude_net)      -> (ok, min_actual, loc)
    validate_via_clearance(via, exclude_net)          -> (ok, min_actual, loc)
    validate_via_to_via_clearance(via, exclude_net)   -> (ok, min_actual, loc)

This adapter registers the pair's *counterpart* as existing copper -- pads via
``add_pad``, segments and vias via ``mark_route`` -- and then asks the gate
about the candidate, exactly as ``Autorouter`` does after a path is found.
Nothing is patched, relaxed or re-implemented.

Two properties of this consumer are visible in its row and are worth naming,
because they are findings rather than adapter artefacts:

**It applies two different requirements depending on which object is the
candidate.**  A segment candidate is compared against ``rules.trace_clearance``;
a via candidate against ``rules.via_clearance``.  On a board where those differ
(0.15 vs 0.20 by default -- see ``fixtures.py``) the same pair of objects gets
two different answers depending on insertion order.  That is #5398, and it is
why :func:`tests.conformance.adapters._support.pair_contexts` pins one order
(via-first) and says so.  (Issue #6272 removed the asymmetry for trace/via
pairs: both validators now resolve ``max(trace_clearance, via_clearance)``
through ``clearance_resolver.trace_via_clearance_mm``, so the pinned order no
longer changes this row's verdict.)

**Foreign pads.** For a candidate via these checks visit committed segments
and vias. The original retry transaction omitted the foreign-pad check that
the ordinary Python pathfinder performs with ``RoutingGrid.worst_via_pad_deficit``
(#6117). The C++ commit validator also checks foreign pads (violation type 8).
Group 12 includes pad/via pairs to measure the retry omission owned by #9924.
"""

from __future__ import annotations

import math

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    ALL_PAIR_KINDS,
    PairContext,
    net_ids,
    project_rules,
    router_grid,
    router_pad,
    router_rules,
    router_segment,
    router_via,
    single_object_route,
)
from tests.conformance.generator import CopperCase, PadSpec, SegmentSpec

__all__ = ["GridPyAdapter"]


class GridPyAdapter:
    """Drives ``RoutingGrid``'s commit-time clearance validators."""

    name = "grid_py"
    group = 12
    pair_kinds = ALL_PAIR_KINDS

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        return self._verdicts(case, router_rules(case))

    def verdicts_at_project_rules(self, case: CopperCase) -> set[Verdict]:
        """The same three validators on a grid armed at kicad-cli's clearance.

        Issue #9922: the same-rule reading.  ``router_grid(case, rules)`` is
        what keeps the grid the validators read their requirement from and the
        project value from disagreeing, exactly as every migrated adapter's
        gated reading does it.
        """
        return self._verdicts(case, project_rules(case))

    def _verdicts(self, case: CopperCase, rules) -> set[Verdict]:
        nets = net_ids(case)
        found: set[Verdict] = set()
        for context in _contexts(case):
            rejected, actual, required = self._judge(case, context, nets, rules)
            if rejected:
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=actual,
                        required_mm=required,
                    )
                )
        return found

    def _judge(
        self,
        case: CopperCase,
        context: PairContext,
        nets: dict[str, int],
        rules,
    ) -> tuple[bool, float | None, float]:
        """``(rejected, measured_gap_mm, required_mm)`` for one pair."""
        grid = router_grid(case, rules)
        existing = context.existing
        if isinstance(existing, PadSpec):
            grid.add_pad(router_pad(existing, nets))
        else:
            grid.mark_route(single_object_route(existing, nets))

        candidate = context.candidate
        exclude_net = nets[candidate.net]

        if isinstance(candidate, SegmentSpec):
            ok, actual, _ = grid.validate_segment_clearance(
                router_segment(candidate, nets), exclude_net
            )
            return (not ok, _finite(actual), grid.rules.trace_clearance)

        via = router_via(candidate, nets)
        ok_seg, actual_seg, _ = grid.validate_via_clearance(via, exclude_net)
        ok_via, actual_via, _ = grid.validate_via_to_via_clearance(via, exclude_net)
        actual = min(actual_seg, actual_via)
        return (not (ok_seg and ok_via), _finite(actual), grid.rules.via_clearance)


def _contexts(case: CopperCase) -> list[PairContext]:
    from tests.conformance.adapters._support import pair_contexts

    return [c for c in pair_contexts(case) if c.kind in GridPyAdapter.pair_kinds]


def _finite(value: float) -> float | None:
    """``inf`` means "nothing to measure against", which is not a gap."""
    return None if not math.isfinite(value) else value
