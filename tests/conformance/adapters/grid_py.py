"""Group 12 covers Python trace-retry commit clearance.

The pad/via reading submits the candidate through the production trace
transaction, including the component clearance policy and the drilled span.
Other routing pairs drive the grid validators used by that transaction.
The published reading uses the router's configured floors. The merge gate
runs the same consumer at the project's clearance.
"""

from __future__ import annotations

import math

from kicad_tools.router.clearance_resolver import trace_via_clearance_mm
from kicad_tools.router.core import Autorouter, _TraceResolverTransaction
from kicad_tools.router.rules import DesignRules
from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    ALL_PAIR_KINDS,
    PairContext,
    layer_stack_for,
    net_ids,
    project_rules,
    router_grid,
    router_pad,
    router_rules,
    router_segment,
    router_via,
    single_object_route,
)
from tests.conformance.generator import CopperCase, PadSpec, SegmentSpec, ViaSpec

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
        return self._verdicts(case, project_rules(case))

    def _verdicts(self, case: CopperCase, rules: DesignRules) -> set[Verdict]:
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
        rules: DesignRules | None = None,
    ) -> tuple[bool, float | None, float]:
        """``(rejected, measured_gap_mm, required_mm)`` for one pair."""
        rules = router_rules(case) if rules is None else rules
        existing, candidate = context.existing, context.candidate
        if isinstance(existing, PadSpec) and isinstance(candidate, ViaSpec):
            router = Autorouter(
                case.width,
                case.height,
                rules=rules,
                layer_stack=layer_stack_for(case),
                force_python=True,
                adaptive_grid=False,
            )
            pad = router_pad(existing, nets)
            router.pads[pad.key] = pad
            router.nets.setdefault(pad.net, []).append(pad.key)
            router.grid.add_pad(pad)
            transaction = _TraceResolverTransaction(router)
            transaction.begin()
            route = single_object_route(candidate, nets)
            router.routes.append(route)
            router._mark_route(route)
            if transaction.validate_committed_geometry():
                return False, None, rules.trace_clearance
            # This two-object coupon has a single foreign SMD pad and no
            # component overrides, so the deficit measures its copper gap.
            deficit, _ = router.grid.worst_via_pad_deficit(route.vias[0], route.net)
            return True, rules.trace_clearance - deficit, rules.trace_clearance

        grid = router_grid(case, rules)
        if isinstance(existing, PadSpec):
            grid.add_pad(router_pad(existing, nets))
        else:
            grid.mark_route(single_object_route(existing, nets))

        exclude_net = nets[candidate.net]

        if isinstance(candidate, SegmentSpec):
            ok, actual, _ = grid.validate_segment_clearance(
                router_segment(candidate, nets), exclude_net
            )
            required = rules.trace_clearance
            if isinstance(existing, ViaSpec):
                required = trace_via_clearance_mm(rules.trace_clearance, rules.via_clearance)
            return not ok, _finite(actual), required

        via = router_via(candidate, nets)
        ok_seg, actual_seg, _ = grid.validate_via_clearance(via, exclude_net)
        ok_via, actual_via, _ = grid.validate_via_to_via_clearance(via, exclude_net)
        actual = min(actual_seg, actual_via)
        required = rules.via_clearance
        if isinstance(existing, SegmentSpec):
            required = trace_via_clearance_mm(rules.trace_clearance, rules.via_clearance)
        return not (ok_seg and ok_via), _finite(actual), required


def _contexts(case: CopperCase) -> list[PairContext]:
    from tests.conformance.adapters._support import pair_contexts

    return [c for c in pair_contexts(case) if c.kind in GridPyAdapter.pair_kinds]


def _finite(value: float) -> float | None:
    """``inf`` means "nothing to measure against", which is not a gap."""
    return None if not math.isfinite(value) else value
