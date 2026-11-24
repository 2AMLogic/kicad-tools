"""Group 14 -- the pairwise (HV-isolation) net-pair clearance gate.

``router/pairwise_clearance.py`` resolves a requirement **per net pair**
(``max(dru, creepage(|Va - Vb|))``) instead of from one scalar, and then
applies it to routed copper.  The public board-level replay
``board_pairwise_violations`` (``:1995``) is the entry point this row drives:
it reads the traces, vias, pads and #4506 attach zones out of the *same*
``.kicad_pcb`` the oracle measured, in the same frame, and runs the identical
checks the in-run audit uses (``find_pairwise_violations``, ``:1736``, which
``PairwiseClearanceTable.path_is_clear`` at ``:1722`` is the per-path form
of).  Driving the board replay rather than re-plumbing routes in memory is
deliberate: it is the one supported implementation, so the row cannot drift
from what a real audit would score.

**Every net sits at 0 V, and that is the point.**  The table is built by
``build_pairwise_clearance_table`` (``:364``) from a voltage map that assigns
``0.0`` to every net in the case, so no pair clears the 30 V HV threshold and
no creepage widening applies.  What remains is the ``dru`` floor, applied to
every pair -- i.e. this row measures the pairwise gate's *scalar* behaviour,
which is the behaviour that runs on the overwhelming majority of boards.  The
HV widening is a strictly-widening ``max()`` on top of it (``grid.cpp``
``pairwise_required_clearance`` applies
``max(effective_scalar, matrix[a][b])``), so a pair this row calls clean can
only get *tighter* with voltages, never looser: the measurement is a lower
bound on the gate's strictness, not a cherry-picked easy case.

``dru`` is the router's own ``trace_clearance``, per this harness's rule that
rule values come from the case and are never "fixed" towards the project
netclass -- ``DesignRules.trace_clearance`` is what production passes.

**The C++ threshold sub-row is dormant on the generated corpus and is recorded as
such.**  The eight-pair ``hv-pair-matrix-coupon`` fixture (Issue #10048,
acceptance item P2c of #5509) is the named corpus that exercises it:
:meth:`PairwiseAdapter.cpp_validator_verdicts` installs the domain matrix built
from the coupon's voltage map -- or leaves it dormant -- and drives the
production ``Grid3D::validate_route`` in both insertion orders.
``Grid3D::pairwise_required_clearance`` (``grid.cpp:751``, bound at
``bindings.cpp:337``) returns ``0.0`` until ``set_pairwise_domains`` installs
a per-net domain array and a domain-pair matrix. With every net at 0 V there
are no domains to install, so calling it would measure the dormant path -- a
guaranteed ``0.0``, indistinguishable from agreement. It is a ``not measured``
sub-note on this row rather than a fake zero; measuring it needs an HV corpus,
which Phase 2's resolver work is the natural home for.

**Pair kinds: all five routing kinds.**  ``board_pairwise_violations``
covers trace-vs-trace, trace-vs-via, via-vs-via and trace/via-vs-pad
(``:2026``), which is exactly the harness's routing pair set.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    ALL_PAIR_KINDS,
    cpp_grid_for,
    cpp_segment,
    layer_indexer,
    net_ids,
    project_rules,
    router_cpp_module,
    router_rules,
    router_segment,
)
from tests.conformance.generator import CopperCase

__all__ = ["INSERTION_ORDERS", "PairwiseAdapter"]

INSERTION_ORDERS = ("a-then-b", "b-then-a")
"""The two insertion orders of a pair: which net's track is existing copper.

``a-then-b`` stores the pair's first-declared net first and validates the
second as the candidate; ``b-then-a`` swaps them.
"""


class PairwiseAdapter:
    """Drives ``board_pairwise_violations`` over the harness's own board."""

    name = "pairwise"
    group = 14
    pair_kinds = ALL_PAIR_KINDS

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        return self._verdicts(case, router_rules(case))

    def verdicts_at_project_rules(self, case: CopperCase) -> set[Verdict]:
        """The same board replay with the scalar floor at kicad-cli's clearance.

        Issue #9922: the same-rule reading.  ``dru`` is the one rule value the
        replay consults on an all-0 V board, so moving ``trace_clearance`` onto
        ``case.rules.project_clearance`` moves the whole requirement.
        """
        return self._verdicts(case, project_rules(case))

    def verdicts_at_voltages(
        self,
        case: CopperCase,
        net_voltages: dict[str, float],
        rules=None,
    ) -> set[Verdict]:
        """The same board replay under an explicit voltage map (Issue #9922).

        The matched-rule **scope control**.  This row's corpus keeps every net
        at 0 V, so no pair's requirement clears the scalar floor and
        ``find_pairwise_violations`` skips every pair -- which is the gate's
        design, not a fault in its arithmetic.  Raising one net's potential
        here widens that pair's requirement through the table's own public
        builder, and the same replay then decides it.  The voltage map is the
        consumer's documented input; this is not an oracle for the widened
        value, which Epic #5509's parent owns.

        Args:
            case: The case to replay.
            net_voltages: ``{net: volts}``; every net of the case should be
                present, since a missing net is a 0 V net.
            rules: Rule values; defaults to kicad-cli's own.
        """
        return self._verdicts(case, project_rules(case) if rules is None else rules, net_voltages)

    def _verdicts(
        self,
        case: CopperCase,
        rules,
        net_voltages: dict[str, float] | None = None,
    ) -> set[Verdict]:
        from kicad_tools.router.pairwise_clearance import (
            board_pairwise_violations,
            build_pairwise_clearance_table,
        )
        from tests.conformance.board import write_case

        table = build_pairwise_clearance_table(
            dict.fromkeys(case.nets, 0.0) if net_voltages is None else net_voltages,
            dru=rules.trace_clearance,
        )

        with tempfile.TemporaryDirectory(prefix="conformance_pairwise_") as tmp:
            board = write_case(case, Path(tmp))
            violations = board_pairwise_violations(board.pcb_path, table)

        found: set[Verdict] = set()
        for violation in violations:
            if not violation.net_a or not violation.net_b:
                continue
            if violation.net_a == violation.net_b:
                # Same-net shortfall: no two-net key in this harness.
                continue
            found.add(
                Verdict.pair(
                    KIND_CLEARANCE,
                    violation.net_a,
                    violation.net_b,
                    gap_mm=violation.actual_mm,
                    required_mm=violation.required_mm,
                )
            )
        return found

    def cpp_validator_verdicts(
        self,
        case: CopperCase,
        net_voltages: dict[str, float] | None,
        *,
        order: str,
    ) -> set[Verdict]:
        """The production C++ validator over a track-pair case (Issue #10048).

        Each pair gets a fresh bare ``Grid3D``: one track of the pair is stored
        as existing copper and the other is the ``validate_route`` candidate
        (the call shape of ``grid_cpp.py``, which mirrors production).  With a
        voltage map the same payload production installs -- the net-id domain
        array and matrix from ``build_cpp_domain_matrix`` -- is pushed through
        ``set_pairwise_domains`` first; with ``None`` the grid stays dormant.

        Args:
            case: A segment-pair case; every pair intent must be ``seg-seg``.
            net_voltages: ``{net: volts}`` for the matrix, or ``None`` to leave
                the matrix uninstalled (dormant).
            order: One of :data:`INSERTION_ORDERS`.

        Raises:
            RuntimeError: When the router extension is not built.  Never a
                silent empty result: an empty set would read as agreement.
        """
        from kicad_tools.router.pairwise_clearance import (
            build_cpp_domain_matrix,
            build_pairwise_clearance_table,
        )

        if order not in INSERTION_ORDERS:
            raise ValueError(f"order must be one of {INSERTION_ORDERS}, got {order!r}")
        router_cpp = router_cpp_module()
        if router_cpp is None:
            raise RuntimeError("C++ router backend not built: run uv run kct build-native")

        rules = router_rules(case)
        nets = net_ids(case)
        layer_index = layer_indexer(case)
        by_net = {segment.net: segment for segment in case.segments}

        domains = None
        if net_voltages is not None:
            table = build_pairwise_clearance_table(net_voltages, dru=rules.trace_clearance)
            domains = build_cpp_domain_matrix(table, nets)
            if domains is None:
                raise RuntimeError("the voltage map produced no domain matrix to install")

        found: set[Verdict] = set()
        for pair in case.pairs:
            existing_net, candidate_net = (
                (pair.net_a, pair.net_b) if order == "a-then-b" else (pair.net_b, pair.net_a)
            )
            existing = by_net[existing_net]
            candidate = by_net[candidate_net]

            grid = cpp_grid_for(router_cpp, case, rules)
            if domains is not None:
                grid.set_pairwise_domains(domains.net_to_domain, domains.matrix)
            stored = router_segment(existing, nets)
            grid.add_stored_segment(
                stored.x1,
                stored.y1,
                stored.x2,
                stored.y2,
                stored.width,
                layer_index(existing.layer),
                stored.net,
            )
            result = grid.validate_route(
                [cpp_segment(router_cpp, candidate, nets, layer_index)],
                [],
                nets[candidate_net],
                [],
                rules.trace_clearance,
                rules.via_clearance,
                rules.min_drill_clearance,
                -1,
                0.0,
                [],
                rules.min_hole_to_hole,
            )
            if not result.valid:
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        pair.net_a,
                        pair.net_b,
                        gap_mm=result.min_clearance if result.min_clearance < 1e9 else None,
                        required_mm=pair.required_mm,
                    )
                )
        return found
