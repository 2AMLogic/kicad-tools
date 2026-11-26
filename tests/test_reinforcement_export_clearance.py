"""Nominal reinforcement-clearance measurements on the shipped Gerber archive (#10422).

A synthetic two-face X2 archive and a reinforcement sidecar supply reproducible
expectations: for every reinforced opening the export measures one edge-to-edge
minimum to any other net's plotted copper and one to the part of that copper the
same face's mask leaves exposed. The controls mirror the issue's test plan: a
shifted strip that crosses foreign copper, a tangent contact, a cleared copper
region, an X2 attribute reset, and the incomplete-coverage cases surfaced through
the manufacturing package's preflight report.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from kicad_tools.export.reinforcement_clearance import measure_reinforcement_clearance

# --------------------------------------------------------------------------- Gerber writer

FORMAT = (
    "%FSLAX46Y46*%\n"
    "G04 Gerber Fmt 4.6, Leading zero omitted, Abs format (unit mm)*\n"
    "%MOMM*%\n"
    "%LPD*%\n"
    "G01*\n"
)
FILE_FUNCTION = {
    "F.Cu": "Copper,L1,Top",
    "B.Cu": "Copper,L4,Bot",
    "F.Mask": "Soldermask,Top",
    "B.Mask": "Soldermask,Bot",
}
POLARITY = {"F.Cu": "Positive", "B.Cu": "Positive", "F.Mask": "Negative", "B.Mask": "Negative"}
EXTENSION = {"F.Cu": "gtl", "B.Cu": "gbl", "F.Mask": "gts", "B.Mask": "gbs"}

A1, A2 = (10.0, 10.0), (22.0, 14.0)
# Three joined strokes, bent once: (10,10)-(16,10)-(20,10)-(22,14).
STRIP = (
    ((10.0, 10.0), (16.0, 10.0)),
    ((16.0, 10.0), (20.0, 10.0)),
    ((20.0, 10.0), (22.0, 14.0)),
)
# Only the interior stroke changes: it still joins both neighbours and its
# lower edge crosses the RELAY_DRV track.
SHIFTED_STRIP = (
    ((10.0, 10.0), (16.0, 10.0)),
    ((16.0, 10.0), (20.0, 11.2)),
    ((20.0, 10.0), (22.0, 14.0)),
)
# The interior stroke's lower edge is collinear with the track's top edge.
TANGENT_STRIP = (
    ((10.0, 10.0), (16.0, 10.0)),
    ((16.0, 10.8), (20.0, 10.8)),
    ((20.0, 10.0), (22.0, 14.0)),
)
SCAP_VIA = (14.0, 12.1)


def _at(x: float, y: float) -> str:
    """A y-down board coordinate as a Gerber (y-up) coordinate."""
    return f"X{int(round(x * 1_000_000))}Y{-int(round(y * 1_000_000))}"


class Gerber:
    """A minimal KiCad-flavoured Gerber writer: apertures, flashes, strokes, regions, X2."""

    def __init__(self, layer: str) -> None:
        self.layer = layer
        self.apertures: dict[str, int] = {}
        self.body: list[str] = []

    def aperture(self, template: str) -> str:
        if template not in self.apertures:
            self.apertures[template] = 10 + len(self.apertures)
        return f"D{self.apertures[template]}*"

    def flash(self, x: float, y: float, template: str) -> None:
        self.body.append(f"{self.aperture(template)}\n{_at(x, y)}D03*")

    def stroke(self, start: tuple[float, float], end: tuple[float, float], width: float) -> None:
        self.body.append(f"{self.aperture(f'C,{width:.6f}')}\n{_at(*start)}D02*\n{_at(*end)}D01*")

    def region(self, points: list[tuple[float, float]]) -> None:
        self.body.append("G36*")
        closed = [*points, points[0]]
        self.body.append(f"{_at(*closed[0])}D02*")
        for point in closed[1:]:
            self.body.append(f"{_at(*point)}D01*")
        self.body.append("G37*")

    def attribute(self, net: str | None = None, pad: tuple[str, str] | None = None, component=None):
        if component is not None:
            self.body.append(f"%TO.C,{component}*%")
        if pad is not None:
            self.body.append(f"%TO.P,{pad[0]},{pad[1]}*%")
        if net is not None:
            self.body.append(f"%TO.N,{net}*%")

    def reset(self) -> None:
        self.body.append("%TD*%")

    def clear(self) -> None:
        self.body.append("%LPC*%")

    def dark(self) -> None:
        self.body.append("%LPD*%")

    def text(self) -> str:
        header = (
            "%TF.GenerationSoftware,KiCad,Pcbnew,10.0.6*%\n"
            f"%TF.FileFunction,{FILE_FUNCTION[self.layer]}*%\n"
            f"%TF.FilePolarity,{POLARITY[self.layer]}*%\n"
        )
        apertures = "".join(
            f"%ADD{number}{template}*%\n" for template, number in self.apertures.items()
        )
        return header + FORMAT + apertures + "\n".join(self.body) + "\nM02*\n"


def _rect(x0: float, y0: float, x1: float, y1: float) -> str:
    return f"R,{x1 - x0:.6f}X{y1 - y0:.6f}"


def copper_back(
    strip=STRIP, *, layer="B.Cu", run_attribute=True, via_attribute=True, relay_reset=False
):
    """The run's own copper layer: the PGND run, its anchors and the foreign copper."""
    g = Gerber(layer)
    if run_attribute:
        g.attribute(net="PGND")
    for a, b in strip:
        g.stroke(a, b, 2.4)
    g.flash(*A1, "C,1.900000")
    g.flash(*A2, "C,1.900000")
    g.reset()
    if via_attribute:
        g.attribute(net="SCAP_POS")
    # Covered: a 0.8 mm via whose edge stands 0.50 mm below the strip's edge.
    g.flash(*SCAP_VIA, "C,0.800000")
    g.reset()
    if not relay_reset:
        g.attribute(net="RELAY_DRV")
    # Covered track 0.80 mm below the strip's edge, clear of the last stroke.
    g.flash(17.1, 12.2, _rect(15.6, 12.0, 18.6, 12.4))
    g.reset()
    g.attribute(net="PGOOD", pad=("J3", "1"))
    g.flash(15.0, 16.1, _rect(14.0, 14.2, 16.0, 18.0))
    # The same pad's covered tab reaches to 1.0 mm below the strip's edge.
    g.flash(15.0, 13.2, _rect(14.5, 12.2, 15.5, 14.2))
    g.reset()
    return g


def copper_front(*, layer="F.Cu", foreign=True):
    """The other face: the anchors again (no strip) and face-only foreign copper."""
    g = Gerber(layer)
    g.attribute(net="PGND")
    g.flash(*A1, "C,1.900000")
    g.flash(*A2, "C,1.900000")
    g.reset()
    if foreign:
        g.attribute(net="SCAP_POS")
        g.flash(*SCAP_VIA, "C,0.800000")
        g.reset()
        # Front-only covered track 0.60 mm above anchor 1's opening.
        g.attribute(net="FX")
        g.flash(10.0, 8.15, _rect(9.0, 7.9, 11.0, 8.4))
        g.reset()
        # Front-only pad 1.0 mm from anchor 1's opening; only its right-hand
        # part (1.5 mm away) is inside the mask opening.
        g.attribute(net="SENSE", pad=("U1", "2"))
        g.flash(13.0, 10.0, _rect(12.0, 9.25, 14.0, 10.75))
        g.reset()
    return g


def mask_back(strip=STRIP, *, layer="B.Mask", shift_x=0.0):
    """The run's own mask: the strip strokes, the anchor openings and J3 pad 1's opening."""
    g = Gerber(layer)
    for a, b in strip:
        g.stroke((a[0] + shift_x, a[1]), (b[0] + shift_x, b[1]), 2.4)
    g.flash(A1[0] + shift_x, A1[1], "C,2.000000")
    g.flash(A2[0] + shift_x, A2[1], "C,2.000000")
    g.attribute(component="J3")
    g.flash(15.0 + shift_x, 16.1, _rect(14.0, 14.2, 16.0, 18.0))
    g.reset()
    return g


def mask_front(*, layer="F.Mask"):
    g = Gerber(layer)
    g.flash(*A1, "C,2.000000")
    g.flash(*A2, "C,2.000000")
    g.attribute(component="U1")
    g.flash(13.2, 10.0, _rect(12.5, 9.35, 13.9, 10.65))
    g.reset()
    return g


def member_name(layer: str) -> str:
    return f"board-{layer.replace('.', '_')}.{EXTENSION[layer]}"


def members(overrides: dict | None = None) -> dict[str, str]:
    layers = {
        "F.Cu": copper_front(),
        "B.Cu": copper_back(),
        "F.Mask": mask_front(),
        "B.Mask": mask_back(),
    }
    layers.update(overrides or {})
    return {member_name(layer): g.text() for layer, g in layers.items()}


def write_archive(path: Path, texts: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in texts.items():
            zf.writestr(name, text)
    return path


def with_members(archive: Path, name: str, **replacements: bytes) -> Path:
    """A copy of ``archive`` with some member payloads replaced."""
    with zipfile.ZipFile(archive) as zf:
        payloads = {n: zf.read(n) for n in zf.namelist()}
    payloads.update(replacements)
    target = archive.with_name(name)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, payload in payloads.items():
            zf.writestr(member, payload)
    return target


def member_sha(path: Path, layer: str) -> str:
    with zipfile.ZipFile(path) as zf:
        return hashlib.sha256(zf.read(member_name(layer))).hexdigest()


def wire(
    path="PGND_WIRE",
    anchors=(A1, A2),
    net="PGND",
    leg=((10.0, 10.0), (22.0, 14.0)),
    layer="B.Cu",
):
    return {
        "net": net,
        "layer": layer,
        "kind": "run",
        "path": path,
        "paths": [path],
        "leg_start": {"x": leg[0][0], "y": leg[0][1]},
        "leg_end": {"x": leg[1][0], "y": leg[1][1]},
        "anchors": [{"x": x, "y": y} for x, y in anchors],
        "length_mm": 14.47,
        "straight_line_mm": 12.65,
        "wire_gauge_awg": 16,
        "bare_diameter_mm": 1.29,
        "mask_opening": None,
        "status": "full",
        "reason": "",
    }


def shifted(wires, dx=100.0, dy=50.0):
    return [
        {**w, "anchors": [{"x": a["x"] + dx, "y": a["y"] + dy} for a in w["anchors"]]}
        for w in wires
    ]


def opening(report, label, kind, face, anchor=None):
    run = next(r for r in report["runs"] if r["label"] == label)
    return next(
        o
        for o in run["openings"]
        if o["opening"] == kind and o["face"] == face and o.get("anchor") == anchor
    )


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return write_archive(tmp_path / "gerbers" / "gerbers.zip", members())


# --------------------------------------------------------------------------- the nominal archive


def test_span_and_anchor_minima_name_their_own_nearest_copper(archive):
    report = measure_reinforcement_clearance(archive, [wire()]).to_dict()

    assert report["coverage"] == "complete"
    assert report["plot_origin_mm"] == [0.0, 0.0]
    (run,) = report["runs"]
    assert (run["net"], run["layer"]) == ("PGND", "B.Cu")
    assert run["anchors"] == [[10.0, 10.0], [22.0, 14.0]]
    assert sorted((o["opening"], o["face"], o["anchor"]) for o in run["openings"]) == [
        ("anchor", "B", 1),
        ("anchor", "B", 2),
        ("anchor", "F", 1),
        ("anchor", "F", 2),
        ("span", "B", None),
    ]

    span = opening(report, "PGND_WIRE", "span", "B")
    # First minimum: the covered foreign via, 0.50 mm from the strip's edge.
    assert span["all_copper"]["status"] == "complete"
    assert span["all_copper"]["distance_mm"] == pytest.approx(0.5, abs=1e-4)
    assert (span["all_copper"]["net"], span["all_copper"]["pad"]) == ("SCAP_POS", None)
    assert span["all_copper"]["overlap_mm2"] == 0.0
    # Second minimum: the pad's masked-open part 3.00 mm away, although the
    # same pad's covered tab reaches to 1.0 mm -- it is clipped first.
    assert span["exposed"]["status"] == "complete"
    assert span["exposed"]["distance_mm"] == pytest.approx(3.0, abs=1e-4)
    assert (span["exposed"]["net"], span["exposed"]["pad"]) == ("PGOOD", "J3/1")
    assert span["exposed"]["overlap_mm2"] == 0.0

    back1 = opening(report, "PGND_WIRE", "anchor", "B", 1)
    assert back1["anchor_mm"] == [10.0, 10.0]
    assert back1["all_copper"]["distance_mm"] == pytest.approx(3.1177, abs=1e-3)
    assert back1["all_copper"]["net"] == "SCAP_POS"
    assert back1["exposed"]["distance_mm"] == pytest.approx(4.8, abs=1e-4)
    assert (back1["exposed"]["net"], back1["exposed"]["pad"]) == ("PGOOD", "J3/1")
    back2 = opening(report, "PGND_WIRE", "anchor", "B", 2)
    assert back2["all_copper"]["distance_mm"] == pytest.approx(2.7577, abs=1e-3)
    assert back2["all_copper"]["net"] == "RELAY_DRV"


def test_front_anchors_use_only_front_copper_and_mask(archive):
    report = measure_reinforcement_clearance(archive, [wire()]).to_dict()

    front1 = opening(report, "PGND_WIRE", "anchor", "F", 1)
    # The front-only covered track is 0.60 mm away; the front-only pad is 1.0 mm
    # away but only its masked-open part, 1.50 mm, is an exposed candidate.
    assert front1["all_copper"]["distance_mm"] == pytest.approx(0.6, abs=1e-4)
    assert (front1["all_copper"]["net"], front1["all_copper"]["pad"]) == ("FX", None)
    assert front1["exposed"]["distance_mm"] == pytest.approx(1.5, abs=1e-4)
    assert (front1["exposed"]["net"], front1["exposed"]["pad"]) == ("SENSE", "U1/2")
    for face, copper, mask in (("B", "B.Cu", "B.Mask"), ("F", "F.Cu", "F.Mask")):
        measured = opening(report, "PGND_WIRE", "anchor", face, 1)
        for kind in ("all_copper", "exposed"):
            assert measured[kind]["members"]["copper"] == {
                "layer": copper,
                "member": member_name(copper),
                "sha256": member_sha(archive, copper),
            }
            assert measured[kind]["members"]["mask"] == {
                "layer": mask,
                "member": member_name(mask),
                "sha256": member_sha(archive, mask),
            }


def test_minima_name_the_artwork_they_measured(archive):
    report = measure_reinforcement_clearance(archive, [wire()]).to_dict()

    assert report["archive"] == {
        "member": "gerbers.zip",
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    assert {layer: m["status"] for layer, m in report["members"].items()} == {
        "F.Cu": "read",
        "B.Cu": "read",
        "F.Mask": "read",
        "B.Mask": "read",
    }
    span = opening(report, "PGND_WIRE", "span", "B")
    assert span["anchor_mm"] is None
    for kind in ("all_copper", "exposed"):
        minimum = span[kind]
        # Every minimum names its own run, face and opening, not only its
        # parent's.
        assert (minimum["run"], minimum["face"], minimum["opening"]) == (
            "PGND_WIRE",
            "B",
            "B span",
        )
        assert minimum["members"]["copper"]["sha256"] == member_sha(archive, "B.Cu")
        assert minimum["members"]["mask"]["sha256"] == member_sha(archive, "B.Mask")
    back1 = opening(report, "PGND_WIRE", "anchor", "B", 1)
    assert back1["exposed"]["opening"] == "B anchor 1"
    # The closest point lies on the strip's lower edge beside the pad.
    x, y = span["exposed"]["location_mm"]
    assert y == pytest.approx(11.2, abs=1e-6)
    assert 14.0 <= x <= 16.0


def test_exposed_minimum_never_beats_the_all_copper_minimum(archive):
    """The exposed candidates are the all-copper candidates clipped to the mask."""
    report = measure_reinforcement_clearance(archive, [wire()]).to_dict()

    measured = 0
    for run in report["runs"]:
        for o in run["openings"]:
            first, second = o["all_copper"], o["exposed"]
            assert (first["status"], second["status"]) == ("complete", "complete")
            if second["distance_mm"] is None:
                continue
            measured += 1
            assert first["distance_mm"] is not None
            assert second["distance_mm"] >= first["distance_mm"] - 1e-9
    assert measured >= 5


def test_face_without_foreign_copper_reports_no_candidate(tmp_path: Path):
    path = write_archive(tmp_path / "gerbers.zip", members({"F.Cu": copper_front(foreign=False)}))
    report = measure_reinforcement_clearance(path, [wire()]).to_dict()

    assert report["coverage"] == "complete"
    for anchor in (1, 2):
        front = opening(report, "PGND_WIRE", "anchor", "F", anchor)
        for kind in ("all_copper", "exposed"):
            assert front[kind]["status"] == "complete"
            assert front[kind]["distance_mm"] is None
            assert front[kind]["net"] is None
            assert front[kind]["reason"] is None


def test_a_front_run_is_measured_the_same_way(tmp_path: Path):
    """The same board mirrored: the strip and its run are now on the front face."""
    path = write_archive(
        tmp_path / "gerbers.zip",
        members(
            {
                "F.Cu": copper_back(layer="F.Cu"),
                "B.Cu": copper_front(layer="B.Cu"),
                "F.Mask": mask_back(layer="F.Mask"),
                "B.Mask": mask_front(layer="B.Mask"),
            }
        ),
    )
    report = measure_reinforcement_clearance(path, [wire(layer="F.Cu")]).to_dict()

    assert report["coverage"] == "complete"
    assert report["runs"][0]["layer"] == "F.Cu"
    span = opening(report, "PGND_WIRE", "span", "F")
    assert span["all_copper"]["distance_mm"] == pytest.approx(0.5, abs=1e-4)
    assert span["exposed"]["pad"] == "J3/1"
    # The run's own face carries the strip's copper; the far face only its own.
    own = opening(report, "PGND_WIRE", "anchor", "F", 1)
    assert (own["all_copper"]["net"], own["exposed"]["pad"]) == ("SCAP_POS", "J3/1")
    assert own["all_copper"]["members"]["copper"]["layer"] == "F.Cu"
    far = opening(report, "PGND_WIRE", "anchor", "B", 1)
    assert (far["all_copper"]["net"], far["exposed"]["pad"]) == ("FX", "U1/2")
    assert far["all_copper"]["members"]["copper"]["layer"] == "B.Cu"
    assert [o["opening"] for o in report["runs"][0]["openings"]].count("span") == 1


def test_sidecar_frame_is_shifted_by_the_plot_origin(archive):
    """Aux-origin Gerbers: the same archive read through a non-zero plot origin."""
    report = measure_reinforcement_clearance(
        archive, shifted([wire()]), plot_origin_mm=(100.0, 50.0)
    ).to_dict()

    assert report["coverage"] == "complete"
    assert report["plot_origin_mm"] == [100.0, 50.0]
    span = opening(report, "PGND_WIRE", "span", "B")
    assert span["all_copper"]["distance_mm"] == pytest.approx(0.5, abs=1e-4)
    # Reported positions are sheet-absolute, in the sidecar's own frame.
    assert report["runs"][0]["anchors"] == [[110.0, 60.0], [122.0, 64.0]]
    assert span["exposed"]["location_mm"][1] == pytest.approx(61.2, abs=1e-6)


# --------------------------------------------------------------------------- the shipped mask


def test_shifted_strip_crossing_foreign_copper_reports_exposure(archive, tmp_path):
    """Only an interior mask stroke changes; the copper member is byte-for-byte fixed."""
    shipped = with_members(
        archive, "shipped.zip", **{member_name("B.Mask"): mask_back(SHIFTED_STRIP).text().encode()}
    )
    assert member_sha(shipped, "B.Mask") != member_sha(archive, "B.Mask")
    assert member_sha(shipped, "B.Cu") == member_sha(archive, "B.Cu")

    report = measure_reinforcement_clearance(shipped, [wire()]).to_dict()

    assert report["coverage"] == "complete"
    span = opening(report, "PGND_WIRE", "span", "B")
    for kind in ("all_copper", "exposed"):
        assert span[kind]["distance_mm"] == 0.0
        assert (span[kind]["net"], span[kind]["pad"]) == ("RELAY_DRV", None)
        assert span[kind]["overlap_mm2"] > 0
        assert span[kind]["members"]["mask"]["sha256"] == member_sha(shipped, "B.Mask")
        assert span[kind]["members"]["copper"]["sha256"] == member_sha(archive, "B.Cu")
    assert span["exposed"]["exposed"] is True


def test_edge_contact_is_zero_gap_without_exposure(tmp_path: Path):
    path = write_archive(tmp_path / "gerbers.zip", members({"B.Mask": mask_back(TANGENT_STRIP)}))
    report = measure_reinforcement_clearance(path, [wire()]).to_dict()

    span = opening(report, "PGND_WIRE", "span", "B")
    assert span["all_copper"]["distance_mm"] == 0.0
    assert (span["all_copper"]["net"], span["all_copper"]["pad"]) == ("RELAY_DRV", None)
    assert span["all_copper"]["overlap_mm2"] == 0.0
    # The track is covered, and a tangent contact exposes no positive area:
    # it is no exposed candidate, and the pad stays the second minimum.
    assert span["exposed"]["distance_mm"] == pytest.approx(2.2, abs=1e-4)
    assert (span["exposed"]["net"], span["exposed"]["pad"]) == ("PGOOD", "J3/1")
    assert span["exposed"]["exposed"] is False


def test_cleared_copper_is_no_candidate(tmp_path: Path):
    g = copper_back()
    # A clear-polarity region plotted after the via erases it.
    g.clear()
    g.region([(13.0, 11.0), (15.0, 11.0), (15.0, 13.0), (13.0, 13.0)])
    g.dark()
    path = write_archive(tmp_path / "gerbers.zip", members({"B.Cu": g}))
    report = measure_reinforcement_clearance(path, [wire()]).to_dict()

    assert report["coverage"] == "complete"
    span = opening(report, "PGND_WIRE", "span", "B")
    assert span["all_copper"]["distance_mm"] == pytest.approx(0.8, abs=1e-4)
    assert span["all_copper"]["net"] == "RELAY_DRV"


def test_attribute_reset_leaves_no_stale_identity(tmp_path: Path):
    path = write_archive(
        tmp_path / "gerbers.zip",
        members(
            {
                "B.Mask": mask_back(SHIFTED_STRIP),
                "B.Cu": copper_back(relay_reset=True),
            }
        ),
    )
    report = measure_reinforcement_clearance(path, [wire()]).to_dict()

    assert report["coverage"] == "incomplete"
    span = opening(report, "PGND_WIRE", "span", "B")
    for kind in ("all_copper", "exposed"):
        # The crossing track was plotted after %TD*%: it inherits no net, so it
        # is neither excused as the run's own copper nor reported under a stale one.
        assert span[kind]["status"] == "incomplete"
        assert span[kind]["reason"] == "unattributed_copper"
        assert span[kind]["net"] is None
        assert span[kind]["distance_mm"] is None
    # The front face is untouched and keeps its measurements.
    front1 = opening(report, "PGND_WIRE", "anchor", "F", 1)
    assert front1["all_copper"]["distance_mm"] == pytest.approx(0.6, abs=1e-4)


# --------------------------------------------------------------------------- incomplete coverage


def test_missing_net_attribute_on_the_nearest_copper(archive):
    path = with_members(
        archive,
        "noattr.zip",
        **{member_name("B.Cu"): copper_back(via_attribute=False).text().encode()},
    )
    report = measure_reinforcement_clearance(path, [wire()]).to_dict()

    assert report["coverage"] == "incomplete"
    span = opening(report, "PGND_WIRE", "span", "B")
    assert span["all_copper"]["status"] == "incomplete"
    assert span["all_copper"]["reason"] == "unattributed_copper"
    assert span["all_copper"]["net"] is None
    # The covered via was never an exposed candidate, so the second minimum stands.
    assert span["exposed"]["status"] == "complete"
    assert span["exposed"]["distance_mm"] == pytest.approx(3.0, abs=1e-4)
    # A minimum that could not be taken says so; it never reads as "no copper".
    details = measure_reinforcement_clearance(path, [wire()]).preflight_result().details
    assert (
        "B span: incomplete (unattributed_copper); 3.000 mm exposed copper (PGOOD J3/1)" in details
    )


def test_missing_run_net_attribute_keeps_the_other_face(archive):
    path = with_members(
        archive,
        "norun.zip",
        **{member_name("B.Cu"): copper_back(run_attribute=False).text().encode()},
    )
    report = measure_reinforcement_clearance(path, [wire()]).to_dict()

    assert report["coverage"] == "incomplete"
    for kind, anchor in (("span", None), ("anchor", 1), ("anchor", 2)):
        back = opening(report, "PGND_WIRE", kind, "B", anchor)
        assert back["status"] == "incomplete"
        assert back["reason"] == "missing_net_attribute"
        assert back["all_copper"]["distance_mm"] is None
    front = opening(report, "PGND_WIRE", "anchor", "F", 1)
    assert front["status"] == "complete"
    assert front["all_copper"]["net"] == "FX"


def test_competing_run_claim_is_ambiguous(tmp_path: Path):
    path = write_archive(tmp_path / "gerbers.zip", members())
    rival = wire(path="PGND_OTHER_WIRE", leg=((9.0, 9.0), (23.0, 15.0)))
    report = measure_reinforcement_clearance(path, [wire(), rival]).to_dict()

    assert report["coverage"] == "incomplete"
    assert sorted(r["label"] for r in report["runs"]) == ["PGND_OTHER_WIRE", "PGND_WIRE"]
    for label in ("PGND_WIRE", "PGND_OTHER_WIRE"):
        span = opening(report, label, "span", "B")
        assert span["status"] == "incomplete"
        assert span["reason"] == "ambiguous_run_ownership"
        assert span["all_copper"]["distance_mm"] is None
        # Both records name one net, so the shared anchors stay measured.
        assert opening(report, label, "anchor", "F", 1)["status"] == "complete"


def test_offset_mask_member_is_unregistered_on_that_face(tmp_path: Path):
    path = write_archive(tmp_path / "gerbers.zip", members({"B.Mask": mask_back(shift_x=0.4)}))
    report = measure_reinforcement_clearance(path, [wire()]).to_dict()

    assert report["coverage"] == "incomplete"
    for kind, anchor in (("span", None), ("anchor", 1), ("anchor", 2)):
        back = opening(report, "PGND_WIRE", kind, "B", anchor)
        assert back["status"] == "incomplete"
        assert back["reason"] == "unverified_registration"
        assert back["all_copper"]["distance_mm"] is None
    front1 = opening(report, "PGND_WIRE", "anchor", "F", 1)
    assert front1["status"] == "complete"
    assert front1["all_copper"]["distance_mm"] == pytest.approx(0.6, abs=1e-4)


def test_anchor_opening_smaller_than_its_copper_is_still_registered(tmp_path: Path):
    """A negative mask expansion shrinks the opening; its centre still matches."""
    masks = (member_name("F.Mask"), member_name("B.Mask"))
    texts = {
        name: text.replace("C,2.000000", "C,1.600000") if name in masks else text
        for name, text in members().items()
    }
    path = write_archive(tmp_path / "gerbers.zip", texts)
    report = measure_reinforcement_clearance(path, [wire()]).to_dict()

    assert report["coverage"] == "complete"
    for kind, face, anchor in (("span", "B", None), ("anchor", "B", 1), ("anchor", "F", 1)):
        assert opening(report, "PGND_WIRE", kind, face, anchor)["status"] == "complete"


def test_mismatched_plot_origin_is_unverified(archive):
    report = measure_reinforcement_clearance(
        archive, [wire()], plot_origin_mm=(100.0, 50.0)
    ).to_dict()

    assert report["coverage"] == "incomplete"
    openings = [o for run in report["runs"] for o in run["openings"]]
    assert len(openings) == 5
    for o in openings:
        assert o["status"] == "incomplete"
        assert o["reason"] == "unverified_registration"
        assert o["all_copper"]["distance_mm"] is None


def test_missing_artwork_keeps_the_other_face(archive):
    with zipfile.ZipFile(archive) as zf:
        texts = {name: zf.read(name) for name in zf.namelist() if name != member_name("F.Mask")}
    path = write_archive(archive.with_name("nofront.zip"), texts)
    report = measure_reinforcement_clearance(path, [wire()]).to_dict()

    assert report["coverage"] == "incomplete"
    assert report["members"]["F.Mask"]["status"] == "missing_artwork"
    for anchor in (1, 2):
        front = opening(report, "PGND_WIRE", "anchor", "F", anchor)
        assert front["reason"] == "missing_artwork"
        assert front["all_copper"]["status"] == "incomplete"
    span = opening(report, "PGND_WIRE", "span", "B")
    assert span["status"] == "complete"
    assert span["all_copper"]["distance_mm"] == pytest.approx(0.5, abs=1e-4)


def test_unreadable_member_is_missing_artwork(archive):
    path = with_members(archive, "broken.zip", **{member_name("B.Mask"): b"%FSLAX46Y46*%\nG04 x\n"})
    report = measure_reinforcement_clearance(path, [wire()]).to_dict()

    assert report["coverage"] == "incomplete"
    assert report["members"]["B.Mask"]["status"] == "missing_artwork"
    assert opening(report, "PGND_WIRE", "span", "B")["reason"] == "missing_artwork"
    assert opening(report, "PGND_WIRE", "anchor", "F", 1)["status"] == "complete"


def test_mislabelled_member_is_missing_artwork(archive):
    relabelled = mask_back().text().replace("Soldermask,Bot", "Soldermask,Top")
    path = with_members(archive, "mislabelled.zip", **{member_name("B.Mask"): relabelled.encode()})
    report = measure_reinforcement_clearance(path, [wire()]).to_dict()

    assert report["members"]["B.Mask"]["status"] == "missing_artwork"
    assert opening(report, "PGND_WIRE", "span", "B")["reason"] == "missing_artwork"


def test_ambiguous_member_is_missing_artwork(archive):
    twin = archive.with_name("twin.zip")
    with zipfile.ZipFile(twin, "w", zipfile.ZIP_DEFLATED) as zf:
        with zipfile.ZipFile(archive) as source:
            for name in source.namelist():
                zf.writestr(name, source.read(name))
        zf.writestr("copy-B_Mask.gbs", mask_back(shift_x=0.4).text())
    report = measure_reinforcement_clearance(twin, [wire()]).to_dict()

    assert report["members"]["B.Mask"]["status"] == "missing_artwork"
    assert opening(report, "PGND_WIRE", "span", "B")["reason"] == "missing_artwork"


def test_missing_archive(tmp_path: Path):
    report = measure_reinforcement_clearance(tmp_path / "absent.zip", [wire()]).to_dict()
    assert report["coverage"] == "incomplete"
    assert {m["status"] for m in report["members"].values()} == {"missing_artwork"}
    assert opening(report, "PGND_WIRE", "span", "B")["reason"] == "missing_artwork"


def test_no_records_is_not_run(archive):
    report = measure_reinforcement_clearance(archive, []).to_dict()
    assert report["coverage"] == "not_run"
    assert report["runs"] == []


def test_loose_member_directory_is_read_like_the_zip(archive, tmp_path: Path):
    loose = tmp_path / "loose"
    loose.mkdir()
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(loose)
    zipped = measure_reinforcement_clearance(archive, [wire()]).to_dict()
    directory = measure_reinforcement_clearance(loose, [wire()]).to_dict()

    assert directory["coverage"] == "complete"
    assert directory["runs"] == zipped["runs"]
    assert directory["archive"]["member"] == "loose"


def test_report_is_json_and_deterministic(archive):
    first = measure_reinforcement_clearance(archive, [wire()]).to_dict()
    second = measure_reinforcement_clearance(archive, [wire()]).to_dict()
    assert json.loads(json.dumps(first)) == second
    assert first["schema"] == "kct.reinforcement-clearance.v1"
    assert "no spacing verdict" in first["scope"]


# --------------------------------------------------------------------------- package entry point


def _board_text(*, aux_origin=None) -> str:
    setup = "(setup (pad_to_mask_clearance 0)"
    if aux_origin is not None:
        setup += f" (aux_axis_origin {aux_origin[0]} {aux_origin[1]})"
    setup += ")"
    return f"""(kicad_pcb
  (version 20250114)
  (generator "pcbnew")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
  )
  {setup}
  (net 0 "")
  (net 1 "PGND")
  (gr_rect (start 100 100) (end 140 120) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
)
"""


def _package(tmp_path: Path, texts: dict[str, str], wires, *, aux_origin=None, config=None):
    """Drive ``ManufacturingPackage.export`` with the Gerber stage replaced by ``texts``."""
    from kicad_tools.export.assembly import AssemblyPackageResult
    from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
    from kicad_tools.export.preflight import PreflightConfig

    proj = tmp_path / "project"
    proj.mkdir(parents=True)
    pcb = proj / "board.kicad_pcb"
    pcb.write_text(_board_text(aux_origin=aux_origin))
    sidecar = json.dumps({"version": 1, "wires": wires}, indent=2, sort_keys=True) + "\n"
    (proj / "board.kicad_pcb.reinforcement.json").write_text(sidecar)
    config = config or ManufacturingConfig(
        include_report=False,
        include_project_zip=False,
        latest_report_only=False,
        emit_drc_constraints=False,
        bom_source="pcb",
        preflight=PreflightConfig(skip_all=True),
    )
    pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="generic", config=config)
    staged = write_archive(tmp_path / "staged.zip", texts)

    def fake_assembly(out_dir: Path, result):
        target = out_dir / "gerbers" / "gerbers.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(target)
        result.assembly_result = AssemblyPackageResult(output_dir=out_dir, gerber_path=target)
        return True

    pkg._generate_assembly = fake_assembly
    result = pkg.export(tmp_path / "out")
    assert result.success, result.errors
    return result


def _preflight_entry(result):
    return next(pr for pr in result.preflight_results if pr.name == "reinforcement_clearance")


def test_package_report_carries_the_measurements(tmp_path: Path):
    result = _package(tmp_path, members(), [wire()])

    entry = _preflight_entry(result)
    assert entry.status == "OK"
    data = json.loads(json.dumps(entry.to_dict()))["data"]
    assert data["coverage"] == "complete"
    span = opening(data, "PGND_WIRE", "span", "B")
    assert span["all_copper"]["distance_mm"] == pytest.approx(0.5, abs=1e-4)
    assert span["exposed"]["pad"] == "J3/1"
    assert result.reinforcement_clearance == data

    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    shipped = next(p for p in manifest["preflight"] if p["name"] == "reinforcement_clearance")
    assert shipped == entry.to_dict()
    shipped_zip = tmp_path / "out" / "gerbers" / "gerbers.zip"
    assert data["archive"]["sha256"] == hashlib.sha256(shipped_zip.read_bytes()).hexdigest()
    assert data["archive"]["sha256"] == manifest["files"]["gerbers/gerbers.zip"]["sha256"]

    readme = (tmp_path / "out" / "README.txt").read_text()
    assert "Reinforcement clearance" in readme
    assert "0.500 mm" in readme and "J3/1" in readme
    assert "no spacing verdict" in readme


@pytest.mark.parametrize(
    ("texts", "wires", "reason"),
    [
        pytest.param(
            lambda: members({"B.Mask": mask_back(shift_x=0.4)}),
            lambda: [wire()],
            "unverified_registration",
            id="offset-mask-member",
        ),
        pytest.param(
            lambda: members(),
            lambda: [wire(), wire(path="PGND_OTHER_WIRE", leg=((9.0, 9.0), (23.0, 15.0)))],
            "ambiguous_run_ownership",
            id="competing-run-claim",
        ),
        pytest.param(
            lambda: members({"B.Cu": copper_back(via_attribute=False)}),
            lambda: [wire()],
            "unattributed_copper",
            id="missing-net-attribute",
        ),
    ],
)
def test_package_report_names_incomplete_coverage(tmp_path, texts, wires, reason):
    result = _package(tmp_path, texts(), wires())

    entry = _preflight_entry(result)
    assert entry.status == "WARN"
    assert reason in entry.details
    assert entry.data["coverage"] == "incomplete"
    # Whichever control it is, the measurements it does not touch survive.
    front = opening(entry.data, "PGND_WIRE", "anchor", "F", 1)
    assert front["status"] == "complete"
    assert front["all_copper"]["distance_mm"] == pytest.approx(0.6, abs=1e-4)
    # A nominal measurement is never an export error.
    assert result.errors == []
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    shipped = next(p for p in manifest["preflight"] if p["name"] == "reinforcement_clearance")
    assert shipped["status"] == "WARN"
    assert reason in shipped["details"]
    assert "incomplete (" + reason + ")" in (tmp_path / "out" / "README.txt").read_text()


def test_package_report_uses_the_aux_origin_of_the_board(tmp_path: Path):
    from kicad_tools.export.gerber import GerberConfig
    from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage

    result = _package(tmp_path, members(), shifted([wire()]), aux_origin=(100.0, 50.0))
    entry = _preflight_entry(result)
    assert entry.status == "OK", entry.details
    assert entry.data["plot_origin_mm"] == [100.0, 50.0]

    pcb = tmp_path / "project" / "board.kicad_pcb"
    no_aux = ManufacturingPackage(
        pcb_path=pcb,
        manufacturer="generic",
        config=ManufacturingConfig(gerber_config=GerberConfig(use_aux_origin=False)),
    )
    assert no_aux._gerber_plot_origin() == (0.0, 0.0)
    assert ManufacturingPackage(pcb_path=pcb, manufacturer="generic")._gerber_plot_origin() == (
        100.0,
        50.0,
    )


def test_package_without_sidecar_is_unchanged(tmp_path: Path):
    from kicad_tools.export.manufacturing import ManufacturingPackage

    proj = tmp_path / "project"
    proj.mkdir(parents=True)
    pcb = proj / "board.kicad_pcb"
    pcb.write_text(_board_text())
    pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="generic")
    result = pkg.export(tmp_path / "out", dry_run=True)
    assert result.preflight_results == []
    assert result.reinforcement_clearance is None
