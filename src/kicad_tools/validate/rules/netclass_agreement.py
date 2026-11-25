"""Net-class agreement: sidecar vs project vs routed copper (Issue #10139).

``kct route`` writes the classes it routed with to two places that can drift
apart once a human touches either: the ``<stem>.net_class_map.json`` sidecar
(the router's view) and the sibling ``.kicad_pro`` ``net_settings`` (KiCad's
view, #10133).  A third fact -- the width of the copper actually on the board
-- can also disagree with the class a net resolves to.  Nothing reported any
of it.  This module audits all three with two rule ids, both ``warning``:

* ``netclass_sidecar_mismatch`` -- a routed net's class in the sidecar differs
  from the class the project's ``net_settings`` resolves for that net, or the
  sidecar's ``trace_width`` differs from that class's project ``track_width``.
* ``netclass_track_width_mismatch`` -- copper segments (arcs included) of a net
  are narrower than the net's resolved project class ``track_width``.

Why a new rule and not an extension of ``width_consistency``: that rule is a
geometric width-step audit over the board alone; this one needs the PCB *path*
(for the project), the sidecar map and the project's class resolution.

Resolution is :func:`kicad_tools.core.project_clearance.authored_netclass_widths`
-- native priority/inheritance semantics, exact assignments and patterns, and
non-``Default`` classes only.  ``Default``'s width is the board-wide base owned
by the fab tier / ``--trace-width``, so it is out of this rule's reach: a net
that resolves to ``Default`` is never reported.

Severity is ``warning`` (and the ids are advisory in
``DRCChecker.RULE_CATEGORY``): a hand-edited class is legitimate until the
user reroutes, so this is a consistency audit, waivable through
``.kct_waivers.json`` like ``width_consistency``.  It never blocks fab.

Pad-escape neck-downs: the router may legally narrow a pad-escape leg to the
class's ``neck_trace_width`` (Issue #4293).  When the sidecar declares one for
a net, copper down to that width is not reported, so a freshly routed board
does not warn on its own escapes.

Carve-outs, all silent passes (never a crash): no ``pcb_path``, no sidecar map,
no ``.kicad_pro`` (or unreadable / non-object JSON), no non-``Default`` class
with a ``track_width``, a project the resolver cannot model, a net the sidecar
does not cover (sidecar comparison only), and non-numeric fields.  Findings are
sorted by (rule id, net) so repeated runs are byte-identical for CI.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

RULE_SIDECAR_MISMATCH = "netclass_sidecar_mismatch"
RULE_TRACK_WIDTH_MISMATCH = "netclass_track_width_mismatch"

#: Every rule id this module can emit, in declaration order.
NETCLASS_AGREEMENT_RULE_IDS: tuple[str, ...] = (
    RULE_SIDECAR_MISMATCH,
    RULE_TRACK_WIDTH_MISMATCH,
)


def _number(value: Any) -> float | None:
    """``value`` as a float, or ``None`` for anything that is not a real number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def check_netclass_agreement(
    pcb_path: Path | None,
    pcb: PCB,
    net_class_map: Mapping[str, Any] | None,
) -> DRCResults:
    """Audit sidecar / project / copper agreement on net classes.

    Args:
        pcb_path: Path of the ``.kicad_pcb`` under check (the sibling
            ``.kicad_pro`` is derived from it).  ``None`` is a silent no-op.
        pcb: The parsed board.
        net_class_map: The loaded sidecar, ``{board_net_name: NetClassRouting}``
            (the map ``kct check`` already discovered and resolved onto board
            net names).  ``None`` or empty is a silent no-op, like the other
            sidecar-gated rules.

    Returns:
        DRCResults with ``warning`` findings only.
    """
    results = DRCResults()
    for rule_id in NETCLASS_AGREEMENT_RULE_IDS:
        results.rules_checked_by_rule[rule_id] = 1
    results.rules_checked = len(NETCLASS_AGREEMENT_RULE_IDS)

    if pcb_path is None or not net_class_map:
        return results

    from kicad_tools.core.project_clearance import authored_netclass_widths

    from .netclass_clearance_copper import load_project

    project = load_project(pcb_path)
    if project is None:
        return results

    net_names = {num: net.name for num, net in pcb.nets.items() if net.name}
    try:
        authored = authored_netclass_widths(project, list(net_names.values()))
    except Exception:  # noqa: BLE001 - a project the resolver can't model is a silent pass
        return results
    if not authored:
        return results

    findings: list[tuple[tuple[str, str], DRCViolation]] = []

    # --- sidecar vs project -------------------------------------------------
    for net, resolved in authored.items():
        entry = net_class_map.get(net)
        if entry is None:
            continue
        side_class = getattr(entry, "name", None)
        side_width = _number(getattr(entry, "trace_width", None))
        problems: list[str] = []
        if isinstance(side_class, str) and side_class and side_class != resolved.source_class:
            problems.append(
                f"sidecar class {side_class!r} but project resolves {resolved.source_class!r}"
            )
        if side_width is not None and abs(side_width - resolved.width) > DRC_TOLERANCE:
            problems.append(
                f"sidecar trace_width {side_width:.3f}mm but project class "
                f"{resolved.source_class!r} track_width {resolved.width:.3f}mm"
            )
        if not problems:
            continue
        findings.append(
            (
                (RULE_SIDECAR_MISMATCH, net),
                DRCViolation(
                    rule_id=RULE_SIDECAR_MISMATCH,
                    severity="warning",
                    message=f"Net {net!r}: " + "; ".join(problems),
                    actual_value=side_width,
                    required_value=resolved.width,
                    items=(f"net:{net}", f"netclass:{resolved.source_class}"),
                    nets=(net,),
                ),
            )
        )

    # --- copper vs project class width -------------------------------------
    by_net: dict[str, list[Any]] = {}
    for seg in (*pcb.segments, *pcb.arcs):
        name = net_names.get(seg.net_number)
        if name is not None and name in authored:
            by_net.setdefault(name, []).append(seg)

    for net, segs in by_net.items():
        resolved = authored[net]
        required = resolved.width
        entry = net_class_map.get(net)
        neck = _number(getattr(entry, "neck_trace_width", None)) if entry is not None else None
        if neck is not None and neck < required:
            required = neck
        narrow = [s for s in segs if s.width + DRC_TOLERANCE < required]
        if not narrow:
            continue
        worst = min(narrow, key=lambda s: (s.width, s.layer, s.start))
        neck_note = f" (sidecar neck-down floor {neck:.3f}mm)" if required != resolved.width else ""
        findings.append(
            (
                (RULE_TRACK_WIDTH_MISMATCH, net),
                DRCViolation(
                    rule_id=RULE_TRACK_WIDTH_MISMATCH,
                    severity="warning",
                    message=(
                        f"Net {net!r}: {len(narrow)} copper segment(s) narrower than class "
                        f"{resolved.source_class!r} track_width {resolved.width:.3f}mm"
                        f"{neck_note}; narrowest {worst.width:.3f}mm"
                    ),
                    location=worst.start,
                    layer=worst.layer,
                    actual_value=worst.width,
                    required_value=required,
                    items=(f"net:{net}", f"netclass:{resolved.source_class}"),
                    nets=(net,),
                ),
            )
        )

    for _key, violation in sorted(findings, key=lambda entry: entry[0]):
        results.add(violation)
    return results
