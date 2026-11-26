"""``mask_to_copper`` and a footprint's own pad stack (issue #9420).

``RF_Module:ESP32-C3-WROOM-02`` carries a rectangular ground pad with circular
ground pads on it: each circular opening exposes the rectangular pad. That is
the library pad stack, so same-net pads of **one** footprint measure as
``within_own_footprint`` at info; a different footprint, a different net, or a
track keeps the measured verdict.
"""

from shapely.geometry import LineString, Point, box

from kicad_tools.validate.mask_copper import (
    MaskCopperPolicy,
    assess_attributed_geometry,
    assessment_results,
)

POLICY = MaskCopperPolicy(
    clearance_mm=0.09,
    source="order",
    process="process",
    revision="rev",
    warning_clearance_mm=0.15,
)
OWNER = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
RECT = box(0, 0, 3, 3)
CIRCLE = Point(1.5, 1.5).buffer(0.5, quad_segs=64)


def _geometry(
    *,
    owner_parent="fp-1",
    conductor_parent="fp-1",
    owner_net="GND",
    net="GND",
    conductor_kind="pad:rect",
    conductor=RECT,
):
    from shapely.ops import unary_union

    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    exported = ExportedMaskGeometry(
        "a" * 64,
        None,
        None,
        "10.0.6",
        {},
        layers={"F.Mask": CIRCLE, "F.Cu": unary_union([CIRCLE, conductor])},
    )
    return AttributedMaskGeometry(
        exported,
        {
            OWNER: {
                "kind": "pad:circle",
                "net": owner_net,
                "pad_ref": "U1/2",
                "parent_uuid": owner_parent,
                "layers_geometry": {"F.Mask": CIRCLE, "F.Cu": CIRCLE},
                "margin_mm": {"F.Mask": 0.0},
            },
            OTHER: {
                "kind": conductor_kind,
                "net": net,
                "pad_ref": "U1/1",
                "parent_uuid": conductor_parent,
                "layers_geometry": {"F.Cu": conductor},
                "margin_mm": {},
            },
        },
    )


def _measure(geometry):
    result = assess_attributed_geometry(geometry, POLICY, ())
    assert result.coverage == "complete"
    found = [m for m in result.measurements if m.conductor_uuid == OTHER]
    assert len(found) == 1
    severity = [
        v.severity
        for v in assessment_results(result).violations
        if v.rule_id == "mask_to_copper" and OTHER in v.items
    ]
    return found[0], severity[0]


def test_same_net_pads_of_one_footprint_are_info_and_listed():
    m, severity = _measure(_geometry())
    assert m.disposition == "within_own_footprint"
    assert severity == "info"


def test_same_net_pads_of_different_footprints_still_error():
    m, severity = _measure(_geometry(conductor_parent="fp-2"))
    assert m.disposition == "violation"
    assert severity == "error"


def test_different_net_pads_of_one_footprint_still_error():
    m, severity = _measure(_geometry(net="VCC"))
    assert m.disposition == "violation"
    assert severity == "error"


def test_pads_without_a_footprint_identity_are_not_exempt():
    m, severity = _measure(_geometry(owner_parent="", conductor_parent=""))
    assert m.disposition == "violation"
    assert severity == "error"


def test_same_footprint_track_is_not_a_pad_pair():
    """Only pad-to-pad is the footprint exposure; a segment keeps its verdict."""
    track = LineString([(1.5, 1.5), (4, 1.5)]).buffer(0.1, quad_segs=64)
    m, severity = _measure(_geometry(conductor_kind="segment", conductor=track))
    assert m.disposition != "within_own_footprint"


def test_copper_beside_the_opening_is_not_exempt():
    """A same-footprint, same-net pad merely *beside* the opening keeps its verdict."""
    beside = box(2.05, 0, 4, 3)  # 0.05 mm clear of the circle's edge at x=2.0
    m, severity = _measure(_geometry(conductor=beside))
    assert m.relation == "disjoint"
    assert m.disposition != "within_own_footprint"
    assert severity != "info"


def test_merged_opening_with_a_different_net_owner_is_not_exempt():
    """A merged GND+VCC opening exposing a pad stays an error (no mask dam)."""
    from shapely.ops import unary_union

    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    circle_b = Point(2.3, 1.5).buffer(0.5, quad_segs=64)
    third = "00000000-0000-0000-0000-000000000003"
    exported = ExportedMaskGeometry(
        "a" * 64,
        None,
        None,
        "10.0.6",
        {},
        layers={
            "F.Mask": unary_union([CIRCLE, circle_b]),
            "F.Cu": unary_union([CIRCLE, circle_b, RECT]),
        },
    )

    def pad(kind, net, ref, shape, masked=True):
        return {
            "kind": kind,
            "net": net,
            "pad_ref": ref,
            "parent_uuid": "fp-1",
            # Mask shapes shrunk so the native region has a residual bridge
            # no single pad owns: that residual is the merged opening.
            "layers_geometry": (
                {"F.Mask": shape.buffer(-0.15), "F.Cu": shape} if masked else {"F.Cu": shape}
            ),
            "margin_mm": {"F.Mask": 0.0},
        }

    geometry = AttributedMaskGeometry(
        exported,
        {
            OWNER: pad("pad:circle", "GND", "U1/2", CIRCLE),
            OTHER: pad("pad:circle", "VCC", "U1/3", circle_b),
            third: pad("pad:rect", "GND", "U1/1", RECT, masked=False),
        },
    )
    result = assess_attributed_geometry(geometry, POLICY, ())
    merged = [m for m in result.measurements if m.opening_kind == "merged_region"]
    assert merged
    assert all(m.disposition != "within_own_footprint" for m in merged)


def _separate_aperture(**kwargs):
    geometry = _geometry(owner_net="", **kwargs)
    aperture = geometry.objects[OWNER]
    del aperture["layers_geometry"]["F.Cu"]
    aperture["layers"] = ["F.Mask", "F.Paste"]
    aperture["pad_ref"] = None
    return geometry


def test_separate_technical_pad_preserves_aperture_identity_and_names_land():
    m, severity = _measure(_separate_aperture())
    assert (m.opening_uuids, m.owner_ref, m.same_net) == ((OWNER,), "U1/1", True)
    assert m.disposition == "within_own_footprint" and severity == "info"


def test_separate_aperture_requires_a_numbered_local_land_and_full_containment():
    for geometry in (
        _separate_aperture(owner_parent=""),
        _separate_aperture(conductor_parent="another-footprint"),
        _separate_aperture(conductor=box(0, 0, 1.99, 3)),
        _separate_aperture(net=""),
        _separate_aperture(conductor_kind="zone"),
    ):
        m, severity = _measure(geometry)
        assert m.same_net is None and severity == "error"

    geometry = _separate_aperture()
    geometry.objects[OTHER]["pad_ref"] = None
    assert _measure(geometry)[1] == "error"

    geometry = _separate_aperture()
    geometry.objects[OWNER]["pad_ref"] = "U1/3"
    assert _measure(geometry)[1] == "error"


def test_technical_pad_with_opposite_side_copper_stays_unattributed():
    geometry = _separate_aperture()
    geometry.objects[OWNER]["layers_geometry"]["B.Cu"] = CIRCLE
    assert _measure(geometry)[1] == "error"


def test_authored_inner_copper_prevents_assignment_even_without_native_polygon():
    geometry = _separate_aperture()
    geometry.objects[OWNER]["layers"] = ["F.Mask", "In1.Cu"]
    m, severity = _measure(geometry)
    assert (m.owner_ref, m.owner_net, m.same_net) == (None, None, None)
    assert severity == "error"


def test_foreign_pad_clearance_names_both_nets_and_keeps_error():
    geometry = _separate_aperture()
    geometry.objects["foreign-pad"] = dict(
        geometry.objects[OTHER],
        net="SENSE",
        pad_ref="U2/1",
        parent_uuid="fp-2",
        layers_geometry={"F.Cu": box(2.07, 1, 2.5, 2)},
    )
    result = assess_attributed_geometry(geometry, POLICY, ())
    m = next(m for m in result.measurements if m.conductor_uuid == "foreign-pad")
    assert (m.opening_uuids, m.owner_ref, m.owner_net, m.same_net) == (
        (OWNER,),
        "U1/1",
        "GND",
        False,
    )
    assert (m.relation, m.disposition) == ("disjoint", "violation")
    finding = next(v for v in assessment_results(result).violations if "foreign-pad" in v.items)
    assert (finding.nets, finding.severity) == (("GND", "SENSE"), "error")


def test_second_land_under_only_part_of_aperture_prevents_assignment():
    geometry = _separate_aperture()
    third = "00000000-0000-0000-0000-000000000003"
    geometry.objects[third] = dict(
        geometry.objects[OTHER], pad_ref="U1/3", layers_geometry={"F.Cu": box(1.7, 1.4, 2.5, 1.6)}
    )
    assert _measure(geometry)[1] == "error"


def test_separate_aperture_keeps_foreign_copper_and_checks_track_entry():
    geometry = _separate_aperture()
    inside = LineString([(1.4, 1.5), (1.6, 1.5)]).buffer(0.05)
    trace = {
        "kind": "segment",
        "net": "GND",
        "layers_geometry": {"F.Cu": inside},
        "margin_mm": {},
        "endpoints_mm": [(1.4, 1.5), (1.6, 1.5)],
        "width_mm": 0.1,
    }
    geometry.objects["own-track"] = trace
    geometry.objects["foreign-track"] = dict(trace, net="OTHER")
    geometry.objects["foreign-pad"] = dict(
        geometry.objects[OTHER], parent_uuid="another-footprint", layers_geometry={"F.Cu": inside}
    )
    results = assess_attributed_geometry(geometry, POLICY, ())
    found = {m.conductor_uuid: m for m in results.measurements}
    assert found["own-track"].disposition == "within_own_pad"
    assert found["foreign-track"].disposition == found["foreign-pad"].disposition == "violation"
