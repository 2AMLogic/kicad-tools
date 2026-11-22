"""A gateway-first decline still earns the one-ended hybrid retry (issue #7858).

#6883 (gateway-first for a class preferring a non-pad layer) and #6446 (the
hybrid retry) merged in parallel.  The hybrid was wired only behind the
deferred gateway *retry*, which a gateway-first pair never takes, so a
gateway-first segment whose planar run declined went straight to the plain
uncoupled fallback: board 03's ``USB_D[0]`` (USB_D prefers In2.Cu) lost its
hybrid this way.  The ring fixture of ``test_pair_hybrid_6446`` with a B.Cu
preference reproduces it.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from kicad_tools.router.lattice import pathfinder
from kicad_tools.router.lattice.coupled import CoupledConnection

from .test_pair_hybrid_6446 import _HS, _board, _coupled_trunk_mm, _route


@pytest.mark.parametrize(
    "via_counts,lengths,before,expected",
    [
        ((1, 1), (4.1, 4.1), (3.5, 5.2), True),
        # Restoring the missing barrel can increase the sum while both the
        # longest electrical leg and the skew improve (board 03 connector).
        ((1, 1), (5.64, 5.39), (3.47, 6.82), True),
        ((1, 2), (4.0, 4.0), (3.5, 5.2), False),
        ((1, 1), (3.1, 5.5), (3.5, 5.2), False),
        ((1, 1), (2.0, 4.5), (3.5, 5.2), False),
        ((4, 4), (4.0, 4.0), (3.5, 5.2), False),
    ],
)
def test_unequal_hybrid_has_a_checked_fallback(monkeypatch, via_counts, lengths, before, expected):
    pf, pc, _ = _board()
    pf.pair_gateway_limits[pc.key] = (6.0, 3)
    pf.pair_length_measure = lambda route: route.length

    def route(count, length):
        return SimpleNamespace(vias=[None] * count, length=length)

    hybrid = SimpleNamespace(routes=[route(0, before[0]), route(1, before[1])])
    legs = {
        pol: (SimpleNamespace(route=route(count, length)), "ok")
        for pol, count, length in zip(("P", "N"), via_counts, lengths, strict=True)
    }
    monkeypatch.setattr(pf, "_partner_aware_legs", lambda *_args, **_kwargs: (legs, False))
    monkeypatch.setattr(pf, "_legs_shortfalls", lambda *_args: 0)
    result = pf._balanced_hybrid_fallback(
        pc,
        hybrid,
        committed=pf._fresh_committed(),
        history={},
        present=0.0,
        escape_plan={},
        vias_used=0,
    )
    assert (result is not None) is expected


@pytest.mark.parametrize(
    "deadline, expected_count", [(0.0, 0), (0.5, 1), (1.5, 2), (5.5, 6), (None, None)]
)
def test_balanced_hybrid_fallback_respects_deadline(monkeypatch, deadline, expected_count):
    pf, pc, _ = _board()
    pf.pair_gateway_limits[pc.key] = (100.0, 3)
    pf.pair_length_measure = lambda _route: 10.0
    hybrid = SimpleNamespace(routes=[SimpleNamespace(vias=[]), SimpleNamespace(vias=[None])])
    committed = pf._fresh_committed()
    committed.add_run(0, [(0.5, 0.5), (1.5, 0.5)], 99, 0.1)

    def segments():
        return {
            segment
            for layer in committed.copper
            for bucket in layer.buckets.values()
            for segment in bucket
        }

    original_copper = segments()
    original_memo = pf._shortfall_memo
    now = 0.0
    starts = []
    search = pf._search_single

    def timed_search(*args, **kwargs):
        nonlocal now
        starts.append(now)
        result = search(*args, **kwargs)
        now += 1.0
        return result

    monkeypatch.setattr(pathfinder, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(pf, "_search_single", timed_search)
    result = pf._balanced_hybrid_fallback(
        pc,
        hybrid,
        committed=committed,
        history={},
        present=0.0,
        escape_plan={},
        vias_used=0,
        deadline=deadline,
    )

    if deadline is None:
        assert result is not None
        assert len(result[0].route.vias) == len(result[1].route.vias)
    else:
        assert starts == list(range(expected_count))
        assert result is None
    assert segments() == original_copper
    assert not committed.vias
    assert committed._trial_depth == 0
    assert pf._shortfall_memo is original_memo


def _prefer_bcu(pc: CoupledConnection) -> CoupledConnection:
    return dataclasses.replace(pc, net_class=dataclasses.replace(_HS, preferred_layers=[1]))


def test_gateway_first_decline_is_offered_the_hybrid() -> None:
    pf, pc, _balls = _board()
    pc = _prefer_bcu(pc)
    routes, stats = _route(pf, pc)
    # The gateway-first attempt ran (confined to B.Cu) and declined ...
    assert "B.Cu" in pf.pair_gateway_log[pc.key]
    assert pc.key not in pf.pair_gateways
    # ... and the segment was still offered the hybrid, which couples it.
    assert pc.key in pf.pair_hybrid_log, pf.pair_gateway_log[pc.key]
    assert pf.pair_outcomes[pc.key] == "hybrid:U1.1", pf.pair_hybrid_log[pc.key]
    assert stats.routed == stats.total == 2
    assert _coupled_trunk_mm(routes, pc.key) > 0.0


def test_gateway_first_hybrid_matches_the_retry_path_hybrid() -> None:
    """Same verdict, same hybrid: the gateway-first entry adds no new search."""
    base_pf, base_pc, _ = _board()
    _route(base_pf, base_pc)
    pf, pc, _ = _board()
    _route(pf, _prefer_bcu(pc))
    assert pf.pair_outcomes[pc.key] == base_pf.pair_outcomes[base_pc.key]
    assert pf.pair_hybrid_log[pc.key] == base_pf.pair_hybrid_log[base_pc.key]


def test_no_hybrid_without_the_uncoupled_fallback() -> None:
    pf, pc, _balls = _board()
    pc = _prefer_bcu(pc)
    pf.route_netset([], coupled=[pc], uncoupled_fallback=False, pair_gateways=True)
    assert pc.key not in pf.pair_hybrid_log
    assert not pf.pair_outcomes.get(pc.key, "").startswith("hybrid:")
