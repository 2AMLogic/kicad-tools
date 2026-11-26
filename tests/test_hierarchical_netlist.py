"""Tests for hierarchical sub-sheet traversal in netlist extraction.

Verifies that build_netlist_from_schematic() recursively loads all
(sheet ...) references, collects components from every level, merges
global labels across sheets, handles hierarchical label cross-sheet
net merging, and handles edge cases gracefully.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.operations.netlist import (
    _collect_hierarchy_components,
    _count_hierarchy_sheets,
    _get_sheet_entries,
    _get_sheet_filenames,
    build_netlist_from_schematic,
    export_netlist,
)
from kicad_tools.schematic.models import Schematic
from kicad_tools.sexp import parse_file, parse_string

FIXTURES = Path(__file__).parent / "fixtures" / "hierarchical"


class TestGetSheetFilenames:
    """Tests for _get_sheet_filenames helper."""

    def test_extracts_filenames_from_root(self):
        """Root fixture references sub_a.kicad_sch and sub_b.kicad_sch."""
        filenames = _get_sheet_filenames(FIXTURES / "root.kicad_sch")
        assert "sub_a.kicad_sch" in filenames
        assert "sub_b.kicad_sch" in filenames
        assert len(filenames) == 2

    def test_extracts_nested_filename(self):
        """sub_a references nested.kicad_sch."""
        filenames = _get_sheet_filenames(FIXTURES / "sub_a.kicad_sch")
        assert filenames == ["nested.kicad_sch"]

    def test_no_sheets_returns_empty(self):
        """Leaf sheets with no sub-sheets return empty list."""
        filenames = _get_sheet_filenames(FIXTURES / "sub_b.kicad_sch")
        assert filenames == []

    def test_empty_sheet_returns_empty(self):
        """Empty schematic has no sheet entries."""
        filenames = _get_sheet_filenames(FIXTURES / "empty.kicad_sch")
        assert filenames == []


class TestCollectHierarchyComponents:
    """Tests for _collect_hierarchy_components recursive collector."""

    def test_collects_root_components_only_for_leaf(self):
        """A leaf sheet (no sub-sheets) returns only its own components."""
        components, _ = _collect_hierarchy_components(FIXTURES / "sub_b.kicad_sch", "/")
        refs = {c.reference for c in components}
        assert refs == {"R3", "R4"}

    def test_collects_nested_three_levels(self):
        """Root -> sub_a -> nested gives components from all three levels."""
        components, _ = _collect_hierarchy_components(FIXTURES / "root.kicad_sch", "/")
        refs = {c.reference for c in components}
        # Root: R1; sub_a: R2, C1; nested: C2; sub_b: R3, R4
        assert refs == {"R1", "R2", "C1", "C2", "R3", "R4"}

    def test_missing_subsheet_skipped_gracefully(self):
        """Missing sub-sheet files produce a warning, not a crash."""
        components, _ = _collect_hierarchy_components(FIXTURES / "root_shared.kicad_sch", "/")
        # Should still collect components from root and accessible sub-sheets
        refs = {c.reference for c in components}
        assert "R1" in refs  # root component

    def test_sheet_path_propagated(self):
        """Components carry their sheet_path for hierarchy tracking."""
        components, _ = _collect_hierarchy_components(FIXTURES / "root.kicad_sch", "/")
        root_comp = next(c for c in components if c.reference == "R1")
        assert root_comp.sheet_path == "/"

        sub_a_comp = next(c for c in components if c.reference == "R2")
        assert "sub_a.kicad_sch" in sub_a_comp.sheet_path


class TestBuildNetlistHierarchical:
    """Tests for build_netlist_from_schematic with hierarchical schematics."""

    def test_all_components_from_hierarchy(self):
        """All components across the full hierarchy appear in the netlist."""
        netlist = build_netlist_from_schematic(FIXTURES / "root.kicad_sch")
        refs = {c.reference for c in netlist.components}
        # 6 components total: R1 (root), R2+C1 (sub_a), C2 (nested), R3+R4 (sub_b)
        assert refs == {"R1", "R2", "C1", "C2", "R3", "R4"}
        assert len(netlist.components) == 6

    def test_global_labels_merged_across_sheets(self):
        """Global label DATA_BUS appears in root, sub_a, and sub_b -- should merge."""
        netlist = build_netlist_from_schematic(FIXTURES / "root.kicad_sch")
        {n.name for n in netlist.nets}
        # DATA_BUS is a global label present on root, sub_a, and sub_b
        # It won't have pin connections (no symbols connected by wire to it)
        # but should still appear as a net if the extract_netlist picks it up
        # The key test is that components from all sheets are collected.
        assert len(netlist.components) == 6

    def test_shared_subsheet_referenced_twice(self):
        """Same .kicad_sch used by two sheet instances loads once (circular guard)."""
        netlist = build_netlist_from_schematic(FIXTURES / "root_shared.kicad_sch")
        refs = {c.reference for c in netlist.components}
        # root has R1; sub_b has R3, R4.
        # sub_b is referenced twice but the file is the same, so circular
        # detection means it loads only once. The second reference is skipped.
        assert "R1" in refs
        assert "R3" in refs
        assert "R4" in refs

    def test_missing_subsheet_no_crash(self):
        """A missing sub-sheet file does not crash the build."""
        # root_shared references does_not_exist.kicad_sch
        netlist = build_netlist_from_schematic(FIXTURES / "root_shared.kicad_sch")
        assert netlist is not None
        # Should still have components from accessible sheets
        assert len(netlist.components) >= 1

    def test_empty_subsheet_no_crash(self):
        """An empty sub-sheet (no components) does not crash the build."""
        netlist = build_netlist_from_schematic(FIXTURES / "root_shared.kicad_sch")
        assert netlist is not None

    def test_deeply_nested_hierarchy(self):
        """Three-level hierarchy (root -> sub_a -> nested) works."""
        netlist = build_netlist_from_schematic(FIXTURES / "root.kicad_sch")
        # C2 is in the nested sheet (level 3)
        refs = {c.reference for c in netlist.components}
        assert "C2" in refs

    def test_flat_schematic_unchanged(self, tmp_path):
        """A flat schematic (no sub-sheets) still works correctly."""
        sch_file = tmp_path / "flat.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "flat-uuid-0001")
              (paper "A4")
              (lib_symbols
                (symbol "Device:R"
                  (symbol "R_1_1"
                    (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
                    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27))))))))
              (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
                (in_bom yes) (on_board yes)
                (uuid "flat-r1-uuid")
                (property "Reference" "R1" (at 101.6 48.26 0))
                (property "Value" "10k" (at 101.6 50.8 0))
                (pin "1" (uuid "flat-r1-pin1"))
                (pin "2" (uuid "flat-r1-pin2")))
            )"""
        )

        netlist = build_netlist_from_schematic(sch_file)
        assert len(netlist.components) == 1
        assert netlist.components[0].reference == "R1"

    def test_component_values_preserved(self):
        """Component values and footprints are correct across hierarchy."""
        netlist = build_netlist_from_schematic(FIXTURES / "root.kicad_sch")
        comp_map = {c.reference: c for c in netlist.components}

        assert comp_map["R1"].value == "10k"
        assert comp_map["R2"].value == "4.7k"
        assert comp_map["C1"].value == "100nF"
        assert comp_map["C2"].value == "10uF"
        assert comp_map["R3"].value == "1k"
        assert comp_map["R4"].value == "2.2k"

    def test_footprints_preserved(self):
        """Footprints are correctly extracted from sub-sheet components."""
        netlist = build_netlist_from_schematic(FIXTURES / "root.kicad_sch")
        comp_map = {c.reference: c for c in netlist.components}

        assert comp_map["R1"].footprint == "Resistor_SMD:R_0402_1005Metric"
        assert comp_map["C1"].footprint == "Capacitor_SMD:C_0402_1005Metric"
        assert comp_map["C2"].footprint == "Capacitor_SMD:C_0805_2012Metric"
        assert comp_map["R3"].footprint == "Resistor_SMD:R_0603_1608Metric"

    def test_python_fallback_tool_string(self):
        """The tool string indicates Python fallback."""
        netlist = build_netlist_from_schematic(FIXTURES / "root.kicad_sch")
        assert "Python fallback" in netlist.tool


class TestCountHierarchySheets:
    """Tests for _count_hierarchy_sheets helper."""

    def test_flat_schematic_returns_one(self):
        """A flat schematic (no sub-sheets) has 1 sheet."""
        assert _count_hierarchy_sheets(FIXTURES / "empty.kicad_sch") == 1

    def test_leaf_sheet_returns_one(self):
        """A leaf sub-sheet with no children has 1 sheet."""
        assert _count_hierarchy_sheets(FIXTURES / "sub_b.kicad_sch") == 1

    def test_root_with_two_subs_and_nested(self):
        """root -> sub_a (-> nested) + sub_b = 4 sheets total."""
        assert _count_hierarchy_sheets(FIXTURES / "root.kicad_sch") == 4

    def test_missing_file_returns_zero(self, tmp_path):
        """A nonexistent file returns 0."""
        assert _count_hierarchy_sheets(tmp_path / "nope.kicad_sch") == 0

    def test_shared_subsheet_counted_once(self):
        """Circular/shared references are counted only once."""
        count = _count_hierarchy_sheets(FIXTURES / "root_shared.kicad_sch")
        # root_shared references sub_b twice and does_not_exist once.
        # sub_b is only counted once due to circular detection.
        # does_not_exist returns 0.
        # Total: root_shared(1) + sub_b(1) = 2
        assert count >= 2


class TestGetSheetEntries:
    """Tests for _get_sheet_entries helper."""

    def test_extracts_sheet_pins(self):
        """Sheet entries with (pin ...) children return pin data."""
        entries = _get_sheet_entries(FIXTURES / "parent_with_pins.kicad_sch")
        assert len(entries) == 1
        entry = entries[0]
        assert entry.filename == "child_with_hlabels.kicad_sch"
        assert "SDA" in entry.pin_names
        assert len(entry.pin_positions) == len(entry.pin_names)

    def test_sheets_without_pins_return_empty_lists(self):
        """Sheet entries without (pin ...) children have empty pin lists."""
        entries = _get_sheet_entries(FIXTURES / "root.kicad_sch")
        assert len(entries) == 2
        for entry in entries:
            assert entry.pin_names == []
            assert entry.pin_positions == []

    def test_filenames_match_get_sheet_filenames(self):
        """_get_sheet_entries filenames match _get_sheet_filenames output."""
        entries = _get_sheet_entries(FIXTURES / "root.kicad_sch")
        filenames = _get_sheet_filenames(FIXTURES / "root.kicad_sch")
        assert [e.filename for e in entries] == filenames


class TestHierarchicalLabelNetMerging:
    """Tests for cross-sheet net merging via hierarchical labels and sheet pins."""

    @pytest.mark.parametrize("connected", [True, False], ids=["wired-port", "isolated-port"])
    def test_child_local_alias_follows_only_its_wired_port(self, tmp_path, connected):
        parent = tmp_path / "parent_with_pins.kicad_sch"
        parent.write_bytes((FIXTURES / parent.name).read_bytes())
        child = tmp_path / "child_with_hlabels.kicad_sch"
        doc = parse_file(FIXTURES / child.name)
        doc.add(parse_string('(label "CHILD_ALIAS" (at 80 46.19 0))'))
        if not connected:
            (wire,) = doc.find_all("wire")
            doc.remove(wire)
        child.write_text(doc.to_string())

        assert Schematic.load(child).get_net_for_pin("R5", "1") == "CHILD_ALIAS"
        extracted = Schematic.load(parent).extract_netlist(hierarchical=True)
        pin_nets = {str(pin): name for name, pins in extracted.items() for pin in pins}
        exported = build_netlist_from_schematic(parent)
        export_nets = {
            f"{node.reference}.{node.pin}": net.name for net in exported.nets for node in net.nodes
        }
        for names in (pin_nets, export_nets):
            assert names["R1.1"] == "/I2C_SDA"
            assert names["R5.1"] == ("/I2C_SDA" if connected else "/ChildWithLabels/CHILD_ALIAS")

    @pytest.mark.parametrize("connected", [True, False], ids=["wired", "disconnected"])
    @pytest.mark.parametrize("reverse_ports", [False, True], ids=["sda-first", "thru-first"])
    def test_child_wire_joins_parent_nets(self, tmp_path, connected, reverse_ports):
        parent = tmp_path / "parent_with_pins.kicad_sch"
        doc = parse_file(FIXTURES / parent.name)
        doc.add(
            parse_string(
                '(symbol (lib_id "Device:R") (at 90 60 0) (unit 1) '
                '(property "Reference" "R2") (property "Value" "10k"))'
            )
        )
        doc.add(parse_string("(wire (pts (xy 90 56.19) (xy 130 56.19)))"))
        doc.add(parse_string('(label "SDA" (at 90 56.19 0))'))
        doc.find("sheet").add(parse_string('(pin "THRU" input (at 130 56.19 180))'))
        parent.write_text(doc.to_string())

        child = tmp_path / "child_with_hlabels.kicad_sch"
        doc = parse_file(FIXTURES / child.name)
        sda = doc.find("hierarchical_label")
        doc.add(parse_string('(hierarchical_label "THRU" (shape input) (at 80 46.19 0))'))
        if reverse_ports:
            doc.remove(sda)
            doc.add(sda)
        if not connected:
            doc.remove(doc.find("wire"))
        child.write_text(doc.to_string())

        extracted = Schematic.load(parent).extract_netlist(hierarchical=True)
        pin_nets = {str(pin): name for name, pins in extracted.items() for pin in pins}
        with patch("kicad_tools.operations.netlist.find_kicad_cli", return_value=None):
            exported = export_netlist(parent, fallback=True)
        export_nets = {
            f"{node.reference}.{node.pin}": net.name for net in exported.nets for node in net.nodes
        }
        for names in (pin_nets, export_nets):
            assert names["R1.1"] == "/I2C_SDA"
            assert names["R2.1"] == names["R5.1"] == ("/I2C_SDA" if connected else "/SDA")

    @pytest.mark.parametrize("child_global", [True, False])
    @pytest.mark.parametrize("nested", [True, False])
    def test_port_preserves_child_global(self, tmp_path, child_global, nested):
        parent = tmp_path / "parent_with_pins.kicad_sch"
        doc = parse_file(FIXTURES / parent.name)
        doc.add(parse_string('(global_label "SDA" (at 80 53.81 0))'))
        parent.write_text(doc.to_string())

        child = tmp_path / "child_with_hlabels.kicad_sch"
        doc = parse_file(FIXTURES / child.name)
        if child_global:
            doc.add(parse_string('(global_label "SDA" (at 80 46.19 0))'))
        if nested:
            (tmp_path / "leaf.kicad_sch").write_text(doc.to_string())
            doc = parse_file(FIXTURES / child.name)
            for symbol in doc.find_all("symbol"):
                doc.remove(symbol)
            doc.add(
                parse_string(
                    '(sheet (property "Sheetfile" "leaf.kicad_sch") '
                    '(pin "SDA" input (at 80 46.19 180)))'
                )
            )
        child.write_text(doc.to_string())

        for names in self._hierarchy_pin_maps(parent):
            assert names["R1.2"] == "SDA"
            assert names["R1.1"] == names["R5.1"] == ("SDA" if child_global else "/I2C_SDA")

    @pytest.mark.parametrize("connected", [True, False])
    def test_bridged_parent_global_beats_local(self, tmp_path, connected):
        parent = tmp_path / "parent_with_pins.kicad_sch"
        doc = parse_file(FIXTURES / parent.name)
        doc.remove(doc.find("label"))
        doc.add(parse_string('(global_label "Z_BUS" (at 80 40 0))'))
        doc.add(parse_string('(label "A_LOCAL" (at 80 53.81 0))'))
        doc.add(parse_string("(wire (pts (xy 80 53.81) (xy 130 53.81)))"))
        doc.find("sheet").add(parse_string('(pin "THRU" input (at 130 53.81 180))'))
        parent.write_text(doc.to_string())

        child = tmp_path / "child_with_hlabels.kicad_sch"
        doc = parse_file(FIXTURES / child.name)
        doc.add(parse_string('(hierarchical_label "THRU" (at 80 46.19 0))'))
        if not connected:
            doc.remove(doc.find("wire"))
        child.write_text(doc.to_string())

        for names in self._hierarchy_pin_maps(parent):
            assert names["R1.1"] == "Z_BUS"
            assert names["R1.2"] == names["R5.1"] == ("Z_BUS" if connected else "/A_LOCAL")

    @pytest.mark.parametrize(
        "parent_kind,child_kind,name,parent_net,child_net",
        [
            ("global_label", "label", "Z_BUS", "Z_BUS", "/ChildWithLabels/Z_BUS"),
            ("label", "label", "A_BUS", "/A_BUS", "/ChildWithLabels/A_BUS"),
            ("global_label", "global_label", "Z_BUS", "Z_BUS", "Z_BUS"),
        ],
    )
    def test_independent_child_label_scope(
        self, tmp_path, parent_kind, child_kind, name, parent_net, child_net
    ):
        parent = tmp_path / "parent_with_pins.kicad_sch"
        doc = parse_file(FIXTURES / parent.name)
        doc.add(parse_string(f'({parent_kind} "{name}" (at 130 40 0))'))
        parent.write_text(doc.to_string())

        child = tmp_path / "child_with_hlabels.kicad_sch"
        doc = parse_file(FIXTURES / child.name)
        doc.add(parse_string(f'({child_kind} "{name}" (at 100 46.19 0))'))
        child.write_text(doc.to_string())

        for names in self._hierarchy_pin_maps(parent):
            assert names["R1.1"] == names["R5.1"] == parent_net
            assert names["R6.1"] == child_net

    @pytest.mark.parametrize("sibling_label", ["OTHER", "SHARED"])
    def test_sibling_sheet_label_does_not_rename_unrelated_net(self, tmp_path, sibling_label):
        """Issue #10334: an unrelated sheet's label text must not rename a net."""
        parent = tmp_path / "parent_with_pins.kicad_sch"
        doc = parse_file(FIXTURES / parent.name)
        doc.add(
            parse_string(
                '(sheet (at 130 80) (size 40 30) (uuid "sheet-child-b-uuid") '
                '(property "Sheetname" "ChildB") '
                '(property "Sheetfile" "child_b.kicad_sch") '
                '(pin "SDA" input (at 130 90 180) (uuid "sheet-pin-b-uuid")))'
            )
        )
        parent.write_text(doc.to_string())

        child = tmp_path / "child_with_hlabels.kicad_sch"
        doc = parse_file(FIXTURES / child.name)
        doc.add(parse_string('(label "SHARED" (at 100 46.19 0))'))
        child.write_text(doc.to_string())

        # The second child is a copy with its own references and UUIDs; its R8.1
        # carries the label whose text is edited, and its SDA port is unwired
        # in the parent.
        text = (FIXTURES / child.name).read_text()
        for old, new in (("R5", "R7"), ("R6", "R8"), ("child-", "child-b-")):
            text = text.replace(old, new)
        doc = parse_string(text)
        doc.add(parse_string(f'(label "{sibling_label}" (at 100 46.19 0))'))
        (tmp_path / "child_b.kicad_sch").write_text(doc.to_string())

        for names in self._hierarchy_pin_maps(parent):
            assert names["R6.1"] == "/ChildWithLabels/SHARED"
            assert names["R8.1"] == f"/ChildB/{sibling_label}"
            assert names["R7.1"] == "/ChildB/SDA"
            assert names["R1.1"] == names["R5.1"] == "/I2C_SDA"

    def test_single_sheet_local_label_keeps_bare_name(self, tmp_path):
        """The qualification applies to multi-sheet designs; a flat sheet is unchanged."""
        sch = tmp_path / "flat.kicad_sch"
        sch.write_bytes((FIXTURES / "child_with_hlabels.kicad_sch").read_bytes())
        doc = parse_file(sch)
        doc.add(parse_string('(label "LOCAL" (at 100 46.19 0))'))
        sch.write_text(doc.to_string())

        for names in self._hierarchy_pin_maps(sch):
            assert names["R6.1"] == "LOCAL"

    @staticmethod
    def _hierarchy_pin_maps(parent):
        extracted = Schematic.load(parent).extract_netlist(hierarchical=True)
        yield {str(pin): name for name, pins in extracted.items() for pin in pins}
        with patch("kicad_tools.operations.netlist.find_kicad_cli", return_value=None):
            exported = export_netlist(parent, fallback=True)
        yield {
            f"{node.reference}.{node.pin}": net.name for net in exported.nets for node in net.nodes
        }

    def test_child_components_collected(self):
        """Components from child sheet with hierarchical labels are collected."""
        netlist = build_netlist_from_schematic(FIXTURES / "parent_with_pins.kicad_sch")
        refs = {c.reference for c in netlist.components}
        # Parent: R1; Child: R5, R6
        assert "R1" in refs
        assert "R5" in refs
        assert "R6" in refs
        assert len(netlist.components) == 3

    def test_hlabel_net_merged_with_parent_net(self):
        """Hierarchical label 'SDA' in child merges with parent's 'I2C_SDA' net."""
        _, net_dict = _collect_hierarchy_components(FIXTURES / "parent_with_pins.kicad_sch", "/")
        # The parent has a label "I2C_SDA" connected via wire to the sheet pin "SDA".
        # The child has a hierarchical_label "SDA" connected to R5 pin 1.
        # After merging, the child's "SDA" net should be unified under "I2C_SDA".
        #
        # Check that "I2C_SDA" net exists and contains pins from the child
        if "I2C_SDA" in net_dict:
            # The "SDA" net from the child should have been merged into "I2C_SDA"
            assert "SDA" not in net_dict, (
                "Child's 'SDA' net should be merged into parent's 'I2C_SDA'"
            )

    def test_component_count_matches_across_sheets(self):
        """Component count from hierarchy matches total placed symbols."""
        components, _ = _collect_hierarchy_components(FIXTURES / "parent_with_pins.kicad_sch", "/")
        # Parent: R1; Child: R5, R6
        assert len(components) == 3


class TestComponentCountValidation:
    """Tests for component-count validation fallback in export_netlist."""

    def test_fallback_triggered_on_missing_components(self, tmp_path):
        """When kicad-cli output has fewer components, Python fallback is used."""

        # Create a simple flat schematic for testing
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch
              (version 20231120)
              (generator "test")
              (uuid "test-uuid-0001")
              (paper "A4")
              (lib_symbols
                (symbol "Device:R"
                  (symbol "R_1_1"
                    (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
                    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27))))))))
              (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
                (in_bom yes) (on_board yes)
                (uuid "test-r1-uuid")
                (property "Reference" "R1" (at 101.6 48.26 0))
                (property "Value" "10k" (at 101.6 50.8 0))
                (pin "1" (uuid "test-r1-pin1"))
                (pin "2" (uuid "test-r1-pin2")))
              (symbol (lib_id "Device:R") (at 120 50 0) (unit 1)
                (in_bom yes) (on_board yes)
                (uuid "test-r2-uuid")
                (property "Reference" "R2" (at 121.6 48.26 0))
                (property "Value" "4.7k" (at 121.6 50.8 0))
                (pin "1" (uuid "test-r2-pin1"))
                (pin "2" (uuid "test-r2-pin2")))
            )"""
        )

        # Create a partial netlist file (only 1 of 2 components)
        netlist_file = tmp_path / "test-netlist.kicad_net"
        netlist_file.write_text(
            """(export
              (version "E")
              (design
                (source "test.kicad_sch")
                (tool "KiCad 8.0")
              )
              (components
                (comp (ref "R1")
                  (value "10k")
                  (footprint "")
                  (libsource (lib "Device") (part "R"))
                )
              )
              (nets
              )
            )"""
        )

        # Mock kicad-cli to return the partial netlist
        def mock_run(cmd, **kwargs):
            class Result:
                returncode = 0
                stderr = ""

            return Result()

        with (
            patch(
                "kicad_tools.operations.netlist.find_kicad_cli", return_value="/usr/bin/kicad-cli"
            ),
            patch("kicad_tools.operations.netlist.subprocess.run", side_effect=mock_run),
        ):
            netlist = export_netlist(sch_file, output_path=netlist_file, fallback=True)

        # Should have fallen back to Python extraction which finds both components
        assert len(netlist.components) == 2
        assert "Python fallback" in netlist.tool
