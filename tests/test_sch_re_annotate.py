"""Tests for the sch re-annotate command.

Covers _parse_reference(), _extract_symbols_from_text(),
_apply_reference_rename(), run_re_annotate() with dry-run,
backup, prefix filtering, start-from, per-sheet mode,
multi-unit components, power symbol exclusion, hierarchical
schematic traversal, and the read-only --check order report.
"""

import json
from pathlib import Path

import pytest

from kicad_tools.cli import main as kct
from kicad_tools.cli.sch_re_annotate import (
    _add_project_instance,
    _apply_reference_rename,
    _apply_uuid_reference_rename,
    _assign_numbers,
    _detect_indent,
    _detect_project_info,
    _extract_symbols_from_text,
    _format_reference,
    _parse_reference,
    main,
    run_re_annotate,
)

# ---------------------------------------------------------------------------
# Minimal schematic content for testing
# ---------------------------------------------------------------------------

MINIMAL_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0402_1005Metric"
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R5"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0402_1005Metric"
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R5") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 50 0)
\t\t(property "Reference" "C3"
\t\t\t(at 120 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100nF"
\t\t\t(at 120 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 120 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "C3") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# Schematic with gaps: R1, R5 (gap), C3 (gap) -> should become R1, R2, C1
GAPPED_SCHEMATIC = MINIMAL_SCHEMATIC

# Schematic with power symbols that should be excluded
POWER_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R3"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R3") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "power:GND")
\t\t(at 100 70 0)
\t\t(property "Reference" "#PWR01"
\t\t\t(at 100 76 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(property "Value" "GND"
\t\t\t(at 100 73 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 70 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "44444444-4444-4444-4444-444444444444")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "#PWR01") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# Schematic without instances blocks (simpler format)
NO_INSTANCES_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(property "Reference" "R5"
\t\t\t(at 102 48 0)
\t\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 102 50 0)
\t\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0603_1608Metric"
\t\t\t(at 100 50 0)
\t\t\t(effects (font (size 1.27 1.27)) hide)
\t\t)
\t\t(property "Datasheet" "~"
\t\t\t(at 100 50 0)
\t\t\t(effects (font (size 1.27 1.27)) hide)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-r5-1")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-r5-2")
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(property "Reference" "R10"
\t\t\t(at 102 78 0)
\t\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 102 80 0)
\t\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0603_1608Metric"
\t\t\t(at 100 80 0)
\t\t\t(effects (font (size 1.27 1.27)) hide)
\t\t)
\t\t(property "Datasheet" "~"
\t\t\t(at 100 80 0)
\t\t\t(effects (font (size 1.27 1.27)) hide)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-r10-1")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-r10-2")
\t\t)
\t)
)
"""

# Hierarchical schematic (parent with sub-sheet)
PARENT_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R3"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R3") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R7"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R7") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet
\t\t(at 150 50)
\t\t(size 20 20)
\t\t(property "Sheetname" "SubSheet"
\t\t\t(at 150 48 0)
\t\t)
\t\t(property "Sheetfile" "sub.kicad_sch"
\t\t\t(at 150 68 0)
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

CHILD_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "44444444-4444-4444-4444-444444444444")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 100 50 0)
\t\t(property "Reference" "C5"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "1uF"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "55555555-5555-5555-5555-555555555555")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/33333333-3333-3333-3333-333333333333" (reference "C5") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R14"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "66666666-6666-6666-6666-666666666666")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/33333333-3333-3333-3333-333333333333" (reference "R14") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/33333333-3333-3333-3333-333333333333" (page "2"))
\t)
)
"""

# Already sequential schematic
SEQUENTIAL_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R2"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R2") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# Schematic whose reference order disagrees with position order:
# the top resistor is R2 and the bottom one is R1.
SWAPPED_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R2"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R2") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# Resistors in position order, capacitors swapped: for --prefix filter tests.
RC_MIXED_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R2"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R2") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 50 0)
\t\t(property "Reference" "C2"
\t\t\t(at 120 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100nF"
\t\t\t(at 120 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 120 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "C2") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 80 0)
\t\t(property "Reference" "C1"
\t\t\t(at 120 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "1uF"
\t\t\t(at 120 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 120 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "55555555-5555-5555-5555-555555555555")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "C1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# Multi-unit symbols: U2 (units A and B) sits above U1, so the component
# numbers rank opposite to their positions.
MULTI_UNIT_SWAPPED_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "U2A"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "U2A") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 60 0)
\t\t(property "Reference" "U2B"
\t\t\t(at 100 58 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 62 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 64 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "U2B") (unit 2))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "U1A"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "U1A") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# Multi-unit symbols sharing one base number: never an order disagreement.
MULTI_UNIT_SAME_BASE_SCHEMATIC = MULTI_UNIT_SWAPPED_SCHEMATIC.replace("U2A", "U1A").replace(
    "U2B", "U1B"
)

# Child sheet whose single resistor is numbered below the parent's, so the
# continuous numbering order disagrees while per-sheet numbering agrees.
CHILD_LOW_NUMBER_SCHEMATIC = CHILD_SCHEMATIC.replace('"R14"', '"R1"').replace(
    '(reference "R14")', '(reference "R1")'
)


# Schematic with unannotated components (multi-project scenario)
UNANNOTATED_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R?"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "R?") (unit 1))
\t\t\t)
\t\t\t(project "other-project"
\t\t\t\t(path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/00000000-0000-0000-0000-000000000001" (reference "R6") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 50 0)
\t\t(property "Reference" "C?"
\t\t\t(at 120 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100nF"
\t\t\t(at 120 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 120 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "C?") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""


def _tabs_to_spaces(text: str, width: int = 2) -> str:
    """Convert tab-indented schematic text to space-indented."""
    lines = []
    for line in text.splitlines(True):
        # Count leading tabs
        stripped = line.lstrip("\t")
        n_tabs = len(line) - len(stripped)
        lines.append(" " * (n_tabs * width) + stripped)
    return "".join(lines)


# Space-indented variants of key test fixtures
SPACE_INDENTED_SCHEMATIC = _tabs_to_spaces(MINIMAL_SCHEMATIC)
SPACE_INDENTED_POWER_SCHEMATIC = _tabs_to_spaces(POWER_SCHEMATIC)
SPACE_INDENTED_NO_INSTANCES_SCHEMATIC = _tabs_to_spaces(NO_INSTANCES_SCHEMATIC)
SPACE_INDENTED_UNANNOTATED_SCHEMATIC = _tabs_to_spaces(UNANNOTATED_SCHEMATIC)
SPACE_INDENTED_SEQUENTIAL_SCHEMATIC = _tabs_to_spaces(SEQUENTIAL_SCHEMATIC)

# Mixed indentation: first symbol uses tabs, second uses spaces
MIXED_INDENTATION_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
  (symbol
    (lib_id "Device:R")
    (at 100 80 0)
    (property "Reference" "R5"
      (at 100 78 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "4.7k"
      (at 100 82 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" ""
      (at 100 84 0)
      (effects (font (size 1.27 1.27)) (hide yes))
    )
    (uuid "22222222-2222-2222-2222-222222222222")
    (instances
      (project "test"
        (path "/" (reference "R5") (unit 1))
      )
    )
  )
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""


def _write_sch(
    tmp_path: Path,
    content: str = MINIMAL_SCHEMATIC,
    name: str = "test.kicad_sch",
) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# _parse_reference() unit tests
# ---------------------------------------------------------------------------


class TestParseReference:
    def test_simple_resistor(self):
        assert _parse_reference("R1") == ("R", 1, "")

    def test_simple_capacitor(self):
        assert _parse_reference("C32") == ("C", 32, "")

    def test_multi_unit(self):
        assert _parse_reference("U1A") == ("U", 1, "A")

    def test_multi_unit_b(self):
        assert _parse_reference("U1B") == ("U", 1, "B")

    def test_power_symbol(self):
        assert _parse_reference("#PWR01") == ("#PWR", 1, "")

    def test_flag_symbol(self):
        assert _parse_reference("#FLG01") == ("#FLG", 1, "")

    def test_unannotated(self):
        assert _parse_reference("R?") == ("R", None, "")

    def test_large_number(self):
        assert _parse_reference("R100") == ("R", 100, "")

    def test_two_letter_prefix(self):
        assert _parse_reference("SW1") == ("SW", 1, "")


# ---------------------------------------------------------------------------
# _extract_symbols_from_text() unit tests
# ---------------------------------------------------------------------------


class TestExtractSymbols:
    def test_extracts_all_symbols(self):
        symbols = _extract_symbols_from_text(MINIMAL_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert refs == {"R1", "R5", "C3"}

    def test_extracts_positions(self):
        symbols = _extract_symbols_from_text(MINIMAL_SCHEMATIC)
        by_ref = {s["reference"]: s for s in symbols}
        assert by_ref["R1"]["position_x"] == 100
        assert by_ref["R1"]["position_y"] == 50
        assert by_ref["R5"]["position_y"] == 80

    def test_extracts_lib_id(self):
        symbols = _extract_symbols_from_text(MINIMAL_SCHEMATIC)
        by_ref = {s["reference"]: s for s in symbols}
        assert by_ref["R1"]["lib_id"] == "Device:R"
        assert by_ref["C3"]["lib_id"] == "Device:C"

    def test_extracts_prefix_and_number(self):
        symbols = _extract_symbols_from_text(MINIMAL_SCHEMATIC)
        by_ref = {s["reference"]: s for s in symbols}
        assert by_ref["R5"]["prefix"] == "R"
        assert by_ref["R5"]["number"] == 5
        assert by_ref["C3"]["prefix"] == "C"
        assert by_ref["C3"]["number"] == 3

    def test_power_symbols_extracted(self):
        symbols = _extract_symbols_from_text(POWER_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert "#PWR01" in refs

    def test_no_instances_format(self):
        symbols = _extract_symbols_from_text(NO_INSTANCES_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert refs == {"R5", "R10"}


# ---------------------------------------------------------------------------
# _apply_reference_rename() unit tests
# ---------------------------------------------------------------------------


class TestApplyRename:
    def test_renames_property(self):
        result = _apply_reference_rename(MINIMAL_SCHEMATIC, "R5", "R2")
        assert '"Reference" "R2"' in result
        assert '"Reference" "R5"' not in result
        # R1 should be unchanged
        assert '"Reference" "R1"' in result

    def test_renames_instances(self):
        result = _apply_reference_rename(MINIMAL_SCHEMATIC, "R5", "R2")
        assert '(reference "R2")' in result
        assert '(reference "R5")' not in result
        # R1 instance should be unchanged
        assert '(reference "R1")' in result

    def test_no_match_unchanged(self):
        result = _apply_reference_rename(MINIMAL_SCHEMATIC, "U99", "U1")
        assert result == MINIMAL_SCHEMATIC


# ---------------------------------------------------------------------------
# _assign_numbers() unit tests
# ---------------------------------------------------------------------------


def _old_new_map(uuid_mapping: dict[str, dict]) -> dict[str, str]:
    """Convert UUID-keyed mapping to old->new for simpler test assertions."""
    return {info["old"]: info["new"] for info in uuid_mapping.values()}


class TestAssignNumbers:
    def test_closes_gaps(self):
        symbols = [
            {"reference": "R1", "prefix": "R", "number": 1, "unit_suffix": ""},
            {"reference": "R5", "prefix": "R", "number": 5, "unit_suffix": ""},
            {"reference": "C3", "prefix": "C", "number": 3, "unit_suffix": ""},
        ]
        mapping = _old_new_map(_assign_numbers(symbols, None, 1))
        assert mapping == {"R1": "R1", "R5": "R2", "C3": "C1"}

    def test_prefix_filter(self):
        symbols = [
            {"reference": "R5", "prefix": "R", "number": 5, "unit_suffix": ""},
            {"reference": "C3", "prefix": "C", "number": 3, "unit_suffix": ""},
        ]
        mapping = _old_new_map(_assign_numbers(symbols, ["R"], 1))
        assert mapping == {"R5": "R1"}
        assert "C3" not in mapping

    def test_start_from(self):
        symbols = [
            {"reference": "R5", "prefix": "R", "number": 5, "unit_suffix": ""},
        ]
        mapping = _old_new_map(_assign_numbers(symbols, None, 100))
        assert mapping == {"R5": "R100"}

    def test_multi_unit_shares_number(self):
        symbols = [
            {"reference": "U1A", "prefix": "U", "number": 1, "unit_suffix": "A"},
            {"reference": "U1B", "prefix": "U", "number": 1, "unit_suffix": "B"},
            {"reference": "U5A", "prefix": "U", "number": 5, "unit_suffix": "A"},
        ]
        mapping = _old_new_map(_assign_numbers(symbols, None, 1))
        assert mapping["U1A"] == "U1A"
        assert mapping["U1B"] == "U1B"
        assert mapping["U5A"] == "U2A"

    def test_excludes_power_symbols(self):
        symbols = [
            {"reference": "#PWR01", "prefix": "#PWR", "number": 1, "unit_suffix": ""},
            {"reference": "R3", "prefix": "R", "number": 3, "unit_suffix": ""},
        ]
        mapping = _old_new_map(_assign_numbers(symbols, None, 1))
        assert "#PWR01" not in mapping
        assert mapping["R3"] == "R1"

    def test_annotates_unannotated(self):
        """Unannotated refs (R?) should be assigned numbers."""
        symbols = [
            {
                "reference": "R?",
                "prefix": "R",
                "number": None,
                "unit_suffix": "",
                "uuid": "uuid-r-question",
            },
            {"reference": "R3", "prefix": "R", "number": 3, "unit_suffix": ""},
        ]
        raw = _assign_numbers(symbols, None, 1)
        mapping = _old_new_map(raw)
        assert mapping["R?"] == "R1"
        assert mapping["R3"] == "R2"
        # Verify the unannotated flag is set
        assert raw["uuid-r-question"]["unannotated"] is True

    def test_multiple_unannotated_get_unique_numbers(self):
        """Multiple R? components should each get a unique number."""
        symbols = [
            {
                "reference": "R?",
                "prefix": "R",
                "number": None,
                "unit_suffix": "",
                "uuid": "uuid-r1",
            },
            {
                "reference": "R?",
                "prefix": "R",
                "number": None,
                "unit_suffix": "",
                "uuid": "uuid-r2",
            },
            {"reference": "R5", "prefix": "R", "number": 5, "unit_suffix": ""},
        ]
        raw = _assign_numbers(symbols, None, 1)
        new_refs = {info["new"] for info in raw.values()}
        assert new_refs == {"R1", "R2", "R3"}


# ---------------------------------------------------------------------------
# run_re_annotate() integration tests
# ---------------------------------------------------------------------------


class TestRunReAnnotate:
    def test_dry_run_does_not_modify(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        original = sch.read_text()
        ret = run_re_annotate(schematic_path=sch, dry_run=True, backup=False)
        assert ret == 0
        assert sch.read_text() == original

    def test_closes_gaps(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        # R1 stays R1, R5 becomes R2, C3 becomes C1
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        assert '"Reference" "C1"' in text
        # Old refs should be gone
        assert '"Reference" "R5"' not in text
        assert '"Reference" "C3"' not in text

    def test_updates_instances(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '(reference "R2")' in text
        assert '(reference "C1")' in text
        assert '(reference "R5")' not in text
        assert '(reference "C3")' not in text

    def test_creates_backup(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=True)
        assert ret == 0
        backups = list(tmp_path.glob("test_backup_*"))
        assert len(backups) == 1

    def test_prefix_filter(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False, prefixes=["R"])
        assert ret == 0
        text = sch.read_text()
        # R5 -> R2 (renumbered)
        assert '"Reference" "R2"' in text
        # C3 should stay unchanged (not in prefix filter)
        assert '"Reference" "C3"' in text

    def test_start_from(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False, start_from=10)
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R10"' in text
        assert '"Reference" "R11"' in text
        assert '"Reference" "C10"' in text

    def test_already_sequential(self, tmp_path):
        sch = _write_sch(tmp_path, SEQUENTIAL_SCHEMATIC)
        original = sch.read_text()
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        # File should be unchanged (no effective renames)
        assert sch.read_text() == original

    def test_power_symbols_excluded(self, tmp_path):
        sch = _write_sch(tmp_path, POWER_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        # R3 -> R1
        assert '"Reference" "R1"' in text
        # Power symbol should be unchanged
        assert '"Reference" "#PWR01"' in text

    def test_missing_schematic(self, tmp_path):
        ret = run_re_annotate(schematic_path=tmp_path / "nonexistent.kicad_sch")
        assert ret == 1

    def test_json_output(self, tmp_path, capsys):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=True, backup=False, format="json")
        assert ret == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["dry_run"] is True
        assert data["total"] > 0

    def test_no_instances_format(self, tmp_path):
        sch = _write_sch(tmp_path, NO_INSTANCES_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        # R5 -> R1, R10 -> R2
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        assert '"Reference" "R5"' not in text
        assert '"Reference" "R10"' not in text


def _series_partners(*, crossed=True, names=("SER_A", "SER_B")):
    """Keep both rows in the same position order. Only the connections change."""
    from kicad_tools.schematic.models import Schematic

    sch = Schematic.load(str(Path(__file__).parent / "fixtures/electrical_rating/leds.kicad_sch"))
    sch.symbols = [s for s in sch.symbols if s.reference in {"D1", "D2", "R1", "R2"}]
    sch.labels.clear()
    for sym in sch.symbols:
        is_led = sym.reference.startswith("D")
        if is_led:
            sym.y += 15
        pin = sym.pin_position("1" if is_led else "2")
        end = (pin[0], round(pin[1] + (-2.54 if is_led else 2.54), 2))
        sch.add_wire(pin, end, snap=False)
        column = int(sym.reference[1:]) - 1
        name = names[1 - column if is_led and crossed else column]
        sch.add_label(name, *end, snap=False)
    return sch


class TestPartnerOrder:
    def test_crossed_report_names_both_pairs_and_preserves_input(self, tmp_path, capsys):
        path = tmp_path / "pair.kicad_sch"
        _series_partners().write(path)
        before = path.read_bytes()
        assert main([str(path), "--check", "--format", "json"]) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["out_of_order"] == []
        assert report["total"] == 2
        assert [(p["parts"], p["net"]) for p in report["partner_inversions"]] == [
            (["D1", "R2"], "SER_B"),
            (["D2", "R1"], "SER_A"),
        ]
        assert main([str(path), "--check", "--backup"]) == 1
        out = capsys.readouterr().out
        assert "D1/R2" in out and "D2/R1" in out
        assert "power" in out.lower() and "shared" in out and "multiple" in out
        assert path.read_bytes() == before
        assert list(tmp_path.iterdir()) == [path]

    def test_aligned_pairs_are_clean(self, tmp_path, capsys):
        path = tmp_path / "pair.kicad_sch"
        _series_partners(crossed=False).write(path)
        assert main([str(path), "--check", "--format", "json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["total"] == 0
        assert report["partner_inversions"] == []

    @pytest.mark.parametrize("args", [["--prefix", "R"], ["--unannotated-only"]])
    def test_filters_exclude_partner_comparison(self, tmp_path, capsys, args):
        path = tmp_path / "pair.kicad_sch"
        _series_partners().write(path)
        assert main([str(path), "--check", "--format", "json", *args]) == 0
        assert json.loads(capsys.readouterr().out) == {
            "check": True,
            "out_of_order": [],
            "total": 0,
        }

    @pytest.mark.parametrize("net", ["GND", "+5V"])
    def test_named_rails_do_not_establish_partners(self, tmp_path, capsys, net):
        path = tmp_path / "pair.kicad_sch"
        _series_partners(names=(net, "SER_B")).write(path)
        assert main([str(path), "--check", "--format", "json"]) == 0
        assert json.loads(capsys.readouterr().out)["total"] == 0

    def test_power_flag_on_two_pin_net(self, tmp_path, capsys):
        from kicad_tools.operations.netlist import build_netlist_from_schematic
        from kicad_tools.schematic.models.elements import PowerSymbol

        path = tmp_path / "pair.kicad_sch"
        sch = _series_partners()
        label = next(label for label in sch.labels if label.text == "SER_A")
        sch.power_symbols.append(
            PowerSymbol("power:PWR_FLAG", label.x, label.y, reference="#FLG01")
        )
        sch.write(path)
        # The ordinary netlist still contains just the two component pins.
        nets = build_netlist_from_schematic(path).nets
        assert {n.reference for n in next(n for n in nets if n.name == "SER_A").nodes} == {
            "D2",
            "R1",
        }
        assert main([str(path), "--check", "--format", "json"]) == 0
        assert json.loads(capsys.readouterr().out)["total"] == 0

    def test_supply_input_on_signal_named_net(self, tmp_path, capsys):
        path = tmp_path / "pair.kicad_sch"
        _series_partners().write(path)
        # Both test LEDs have this facing pin. Only its electrical type changes.
        marker = '(pin passive line (at 0 3.81 270) (length 1.27) (name "K")'
        text = path.read_text()
        assert marker in text
        path.write_text(text.replace(marker, marker.replace("passive", "power_in")))
        assert main([str(path), "--check", "--format", "json"]) == 0
        assert json.loads(capsys.readouterr().out)["total"] == 0

    def test_shared_net_is_skipped(self, tmp_path, capsys):
        path = tmp_path / "pair.kicad_sch"
        sch = _series_partners()
        r2 = next(sym for sym in sch.symbols if sym.reference == "R2")
        sch.add_label("SER_A", *r2.pin_position("1"), snap=False, validate_connection=False)
        sch.write(path)
        assert main([str(path), "--check", "--format", "json"]) == 0
        assert json.loads(capsys.readouterr().out)["total"] == 0

    def test_second_candidate_excluded_even_if_filtered_out(self, tmp_path, capsys):
        path = tmp_path / "pair.kicad_sch"
        sch = _series_partners()
        r2 = next(sym for sym in sch.symbols if sym.reference == "R2")
        thermistor = sch.add_symbol("Device:R", 140, 50, ref="RT1", value="NTC")
        for pin in (r2.pin_position("1"), thermistor.pin_position("1")):
            sch.add_label("EXTRA", *pin, snap=False, validate_connection=False)
        sch.write(path)
        assert main([str(path), "--check", "--prefix", "D,R", "--format", "json"]) == 0
        assert json.loads(capsys.readouterr().out)["total"] == 0

    @pytest.mark.parametrize("separate_unit", [False, True])
    def test_unwired_third_pin_disqualifies_a_part(self, tmp_path, capsys, separate_unit):
        path = tmp_path / "pair.kicad_sch"
        _series_partners().write(path)
        # The package still has a third pin if it belongs to an unplaced unit.
        extra_pin = '(pin passive line (at 3.81 0 180) (length 1.27) (name "NC") (number "3"))'
        marker = '(symbol "LED_1_1"'
        definition = (
            f'(symbol "LED_2_1" {extra_pin}) {marker}' if separate_unit else f"{marker} {extra_pin}"
        )
        text = path.read_text().replace(marker, definition)
        path.write_text(text)
        assert main([str(path), "--check", "--format", "json"]) == 0
        assert json.loads(capsys.readouterr().out) == {
            "check": True,
            "out_of_order": [],
            "total": 0,
        }

    def test_per_sheet_compares_only_pairs_on_the_same_sheet(self, tmp_path, capsys):
        from kicad_tools.sexp import parse_string

        parent = tmp_path / "parent.kicad_sch"
        child = tmp_path / "sub.kicad_sch"
        for path, keep in [(parent, {"D1", "R2"}), (child, {"D2", "R1"})]:
            sch = _series_partners()
            sch.symbols = [sym for sym in sch.symbols if sym.reference in keep]
            sch.write(path)
        root = parse_string(parent.read_text())
        root.append(parse_string(PARENT_SCHEMATIC).get("sheet"))
        parent.write_text(root.to_string())
        inputs = {path: path.read_bytes() for path in (parent, child)}
        for args, expected in [([], 2), (["--per-sheet"], 0)]:
            assert main([str(parent), "--check", "--format", "json", *args]) == int(expected > 0)
            report = json.loads(capsys.readouterr().out)
            assert len(report["partner_inversions"]) == expected
        assert {path: path.read_bytes() for path in inputs} == inputs


class TestCheckMode:
    def test_check_swapped_exits_nonzero_and_lists_mapping(self, tmp_path, capsys):
        sch = _write_sch(tmp_path, SWAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, backup=False, check=True)
        assert ret == 1
        out = capsys.readouterr().out
        assert "R2 -> R1" in out
        assert "R1 -> R2" in out
        assert "R:" in out

    def test_check_never_modifies_files(self, tmp_path):
        sch = _write_sch(tmp_path, SWAPPED_SCHEMATIC)
        original = sch.read_text()
        ret = run_re_annotate(schematic_path=sch, backup=True, check=True)
        assert ret == 1
        assert sch.read_text() == original
        # --check never writes backups either
        assert list(tmp_path.glob("*_backup_*")) == []

    def test_check_ordered_exits_zero(self, tmp_path, capsys):
        sch = _write_sch(tmp_path, SEQUENTIAL_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, backup=False, check=True)
        assert ret == 0
        assert "agree" in capsys.readouterr().out

    def test_check_gaps_are_not_order_disagreements(self, tmp_path, capsys):
        """R1, R5 in position order: a gap, but the order agrees."""
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, backup=False, check=True)
        assert ret == 0
        assert capsys.readouterr().out.strip() == (
            "All reference numbers agree with schematic position order."
        )

    def test_check_prefix_filter_agreeing_prefix(self, tmp_path):
        sch = _write_sch(tmp_path, RC_MIXED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, backup=False, prefixes=["R"], check=True)
        assert ret == 0

    def test_check_prefix_filter_swapped_prefix(self, tmp_path, capsys):
        sch = _write_sch(tmp_path, RC_MIXED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, backup=False, prefixes=["C"], check=True)
        assert ret == 1
        out = capsys.readouterr().out
        assert "C2 -> C1" in out
        assert "C1 -> C2" in out
        # The agreeing R prefix is filtered out
        assert "R2 ->" not in out

    def test_check_json_output(self, tmp_path, capsys):
        sch = _write_sch(tmp_path, SWAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, backup=False, format="json", check=True)
        assert ret == 1
        data = json.loads(capsys.readouterr().out)
        assert data["check"] is True
        assert data["total"] == 2
        entries = data["out_of_order"]
        assert {e["old"] for e in entries} == {"R1", "R2"}
        for e in entries:
            assert e["prefix"] == "R"
            assert e["uuid"]
            assert e["new"] in ("R1", "R2")
            assert e["old"] != e["new"]

    def test_check_json_clean_outputs_document(self, tmp_path, capsys):
        sch = _write_sch(tmp_path, SEQUENTIAL_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, backup=False, format="json", check=True)
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert data == {"check": True, "out_of_order": [], "total": 0}

    def test_check_continuous_mode_spans_sheets(self, tmp_path, capsys):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_LOW_NUMBER_SCHEMATIC)
        # Continuous: R order is R3, R7 (parent) then R1 (child) -> inverted
        ret = run_re_annotate(schematic_path=parent, backup=False, check=True)
        assert ret == 1
        out = capsys.readouterr().out
        assert "R1 -> R3" in out

    def test_check_per_sheet_scopes_each_sheet(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_LOW_NUMBER_SCHEMATIC)
        # Per-sheet: parent R3,R7 and child R1 each agree within their sheet
        ret = run_re_annotate(schematic_path=parent, backup=False, per_sheet=True, check=True)
        assert ret == 0

    def test_check_unannotated_excluded(self, tmp_path):
        """R? has no number to rank, so it cannot disagree."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        original = sch.read_text()
        ret = run_re_annotate(schematic_path=sch, backup=False, check=True)
        assert ret == 0
        assert sch.read_text() == original

    def test_check_multi_unit_swapped(self, tmp_path, capsys):
        sch = _write_sch(tmp_path, MULTI_UNIT_SWAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, backup=False, check=True)
        assert ret == 1
        out = capsys.readouterr().out
        assert "U2A -> U1A" in out
        assert "U2B -> U1B" in out
        assert "U1A -> U2A" in out

    def test_check_multi_unit_same_base_agrees(self, tmp_path):
        sch = _write_sch(tmp_path, MULTI_UNIT_SAME_BASE_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, backup=False, check=True)
        assert ret == 0

    def test_check_power_symbols_excluded(self, tmp_path):
        sch = _write_sch(tmp_path, POWER_SCHEMATIC)
        original = sch.read_text()
        ret = run_re_annotate(schematic_path=sch, backup=False, check=True)
        assert ret == 0
        assert sch.read_text() == original

    def test_main_check_flag(self, tmp_path, capsys):
        sch = _write_sch(tmp_path, SWAPPED_SCHEMATIC)
        ret = main(["--check", str(sch)])
        assert ret == 1
        assert "R2 -> R1" in capsys.readouterr().out
        ordered = _write_sch(tmp_path, SEQUENTIAL_SCHEMATIC, name="ordered.kicad_sch")
        assert main(["--check", str(ordered)]) == 0


# ---------------------------------------------------------------------------
# Hierarchical schematic support
# ---------------------------------------------------------------------------


class TestHierarchicalReAnnotate:
    def test_continuous_across_sheets(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)

        ret = run_re_annotate(schematic_path=parent, dry_run=False, backup=False)
        assert ret == 0

        parent_text = parent.read_text()
        child_text = child.read_text()

        # Parent: R3 -> R1, R7 -> R2
        assert '"Reference" "R1"' in parent_text
        assert '"Reference" "R2"' in parent_text

        # Child: R14 -> R3, C5 -> C1
        assert '"Reference" "R3"' in child_text
        assert '"Reference" "C1"' in child_text

    def test_per_sheet_mode(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)

        ret = run_re_annotate(schematic_path=parent, dry_run=False, backup=False, per_sheet=True)
        assert ret == 0

        parent_text = parent.read_text()
        child_text = child.read_text()

        # Parent: R3 -> R1, R7 -> R2
        assert '"Reference" "R1"' in parent_text
        assert '"Reference" "R2"' in parent_text

        # Child (per-sheet restart): R14 -> R1, C5 -> C1
        assert '"Reference" "R1"' in child_text
        assert '"Reference" "C1"' in child_text

    def test_dry_run_hierarchical(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)
        original_parent = parent.read_text()
        original_child = child.read_text()

        ret = run_re_annotate(schematic_path=parent, dry_run=True, backup=False)
        assert ret == 0
        assert parent.read_text() == original_parent
        assert child.read_text() == original_child

    def test_backup_hierarchical(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)

        ret = run_re_annotate(schematic_path=parent, dry_run=False, backup=True)
        assert ret == 0
        # Both files should have backups
        backups = list(tmp_path.glob("*_backup_*"))
        assert len(backups) == 2


# ---------------------------------------------------------------------------
# Unannotated component support
# ---------------------------------------------------------------------------


class TestUnannotatedComponents:
    def test_annotates_unannotated_refs(self, tmp_path):
        """R? and C? should get assigned numbers."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        # R1 stays R1, R? becomes R2, C? becomes C1
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        assert '"Reference" "C1"' in text
        # No more unannotated refs
        assert '"Reference" "R?"' not in text
        assert '"Reference" "C?"' not in text

    def test_adds_project_instance(self, tmp_path):
        """Unannotated components should get project instance entries."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        # Should have a project instance for the test project
        assert '(project "test"' in text
        # The new ref should appear in an instance entry
        assert '(reference "R2")' in text
        assert '(reference "C1")' in text

    def test_preserves_other_project_instances(self, tmp_path):
        """Existing project instances (other-project) should be preserved."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '(project "other-project"' in text
        assert '(reference "R6")' in text

    def test_dry_run_shows_unannotated(self, tmp_path, capsys):
        """Dry run should show unannotated components in the mapping."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=True, backup=False, format="json")
        assert ret == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Should include mappings for unannotated refs
        old_refs = {m["old"] for m in data["mappings"]}
        assert "R?" in old_refs
        assert "C?" in old_refs

    def test_extract_uuid(self):
        """_extract_symbols_from_text should extract UUIDs."""
        symbols = _extract_symbols_from_text(UNANNOTATED_SCHEMATIC)
        by_uuid = {s["uuid"]: s for s in symbols}
        assert "11111111-1111-1111-1111-111111111111" in by_uuid
        assert "22222222-2222-2222-2222-222222222222" in by_uuid
        assert by_uuid["22222222-2222-2222-2222-222222222222"]["reference"] == "R?"


# ---------------------------------------------------------------------------
# Space-indented schematic support
# ---------------------------------------------------------------------------


class TestDetectIndent:
    def test_detects_tabs(self):
        assert _detect_indent(MINIMAL_SCHEMATIC) == "\t"

    def test_detects_spaces(self):
        assert _detect_indent(SPACE_INDENTED_SCHEMATIC) == "  "

    def test_detects_four_spaces(self):
        text = _tabs_to_spaces(MINIMAL_SCHEMATIC, width=4)
        assert _detect_indent(text) == "    "


class TestSpaceIndentedExtractSymbols:
    def test_extracts_all_symbols(self):
        symbols = _extract_symbols_from_text(SPACE_INDENTED_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert refs == {"R1", "R5", "C3"}

    def test_extracts_positions(self):
        symbols = _extract_symbols_from_text(SPACE_INDENTED_SCHEMATIC)
        by_ref = {s["reference"]: s for s in symbols}
        assert by_ref["R1"]["position_x"] == 100
        assert by_ref["R1"]["position_y"] == 50
        assert by_ref["R5"]["position_y"] == 80

    def test_extracts_lib_id(self):
        symbols = _extract_symbols_from_text(SPACE_INDENTED_SCHEMATIC)
        by_ref = {s["reference"]: s for s in symbols}
        assert by_ref["R1"]["lib_id"] == "Device:R"
        assert by_ref["C3"]["lib_id"] == "Device:C"

    def test_power_symbols_extracted(self):
        symbols = _extract_symbols_from_text(SPACE_INDENTED_POWER_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert "#PWR01" in refs

    def test_no_instances_format(self):
        symbols = _extract_symbols_from_text(SPACE_INDENTED_NO_INSTANCES_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert refs == {"R5", "R10"}

    def test_unannotated_symbols(self):
        symbols = _extract_symbols_from_text(SPACE_INDENTED_UNANNOTATED_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert "R?" in refs
        assert "C?" in refs
        assert "R1" in refs


class TestSpaceIndentedRunReAnnotate:
    def test_closes_gaps(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        assert '"Reference" "C1"' in text
        assert '"Reference" "R5"' not in text
        assert '"Reference" "C3"' not in text

    def test_updates_instances(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '(reference "R2")' in text
        assert '(reference "C1")' in text

    def test_already_sequential(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_SEQUENTIAL_SCHEMATIC)
        original = sch.read_text()
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        assert sch.read_text() == original

    def test_power_symbols_excluded(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_POWER_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R1"' in text
        assert '"Reference" "#PWR01"' in text

    def test_no_instances_format(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_NO_INSTANCES_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text

    def test_annotates_unannotated_refs(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        assert '"Reference" "C1"' in text
        assert '"Reference" "R?"' not in text
        assert '"Reference" "C?"' not in text


class TestSpaceIndentedApplyUuidRename:
    def test_renames_in_space_indented_file(self):
        text = _apply_uuid_reference_rename(
            SPACE_INDENTED_SCHEMATIC,
            "22222222-2222-2222-2222-222222222222",
            "R99",
        )
        assert '"Reference" "R99"' in text
        # R1 should be unchanged
        assert '"Reference" "R1"' in text

    def test_renames_in_tab_indented_file(self):
        text = _apply_uuid_reference_rename(
            MINIMAL_SCHEMATIC,
            "22222222-2222-2222-2222-222222222222",
            "R99",
        )
        assert '"Reference" "R99"' in text
        assert '"Reference" "R1"' in text


class TestSpaceIndentedDetectProjectInfo:
    def test_finds_root_uuid_in_space_indented(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_SCHEMATIC)
        project_name, root_uuid, file_paths = _detect_project_info(sch, [sch])
        assert root_uuid == "00000000-0000-0000-0000-000000000001"
        assert project_name == "test"

    def test_finds_root_uuid_in_tab_indented(self, tmp_path):
        sch = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        project_name, root_uuid, file_paths = _detect_project_info(sch, [sch])
        assert root_uuid == "00000000-0000-0000-0000-000000000001"


class TestSpaceIndentedAddProjectInstance:
    def test_adds_instance_with_space_indentation(self):
        text = _add_project_instance(
            SPACE_INDENTED_UNANNOTATED_SCHEMATIC,
            "33333333-3333-3333-3333-333333333333",
            "newproject",
            "/root-uuid",
            "C1",
        )
        assert '(project "newproject"' in text
        assert '(reference "C1")' in text
        # Instance entry should use space indentation matching the file
        assert '\t\t\t(project "newproject"' not in text


class TestMixedIndentation:
    def test_extracts_both_tab_and_space_symbols(self):
        symbols = _extract_symbols_from_text(MIXED_INDENTATION_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert refs == {"R1", "R5"}

    def test_run_re_annotate_on_mixed(self, tmp_path):
        sch = _write_sch(tmp_path, MIXED_INDENTATION_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        assert '"Reference" "R5"' not in text


class TestSpaceIndentedHierarchical:
    def test_continuous_across_space_indented_sheets(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(_tabs_to_spaces(PARENT_SCHEMATIC))
        child = tmp_path / "sub.kicad_sch"
        child.write_text(_tabs_to_spaces(CHILD_SCHEMATIC))

        ret = run_re_annotate(schematic_path=parent, dry_run=False, backup=False)
        assert ret == 0

        parent_text = parent.read_text()
        child_text = child.read_text()

        # Parent: R3 -> R1, R7 -> R2
        assert '"Reference" "R1"' in parent_text
        assert '"Reference" "R2"' in parent_text

        # Child: R14 -> R3, C5 -> C1
        assert '"Reference" "R3"' in child_text
        assert '"Reference" "C1"' in child_text


# ---------------------------------------------------------------------------
# --unannotated-only mode tests
# ---------------------------------------------------------------------------

# Schematic with R1, R2, and R? -- R? should become R3 (avoiding 1 and 2)
COLLISION_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 65 0)
\t\t(property "Reference" "R2"
\t\t\t(at 100 63 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 67 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 69 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R2") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R?"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "1k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "R?") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""


class TestUnannotatedOnly:
    def test_skips_assigned(self, tmp_path):
        """With --unannotated-only, R1 stays R1 and R? becomes R2."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            unannotated_only=True,
        )
        assert ret == 0
        text = sch.read_text()
        # R1 must remain unchanged
        assert '"Reference" "R1"' in text
        # R? should be assigned R2 (next after R1)
        assert '"Reference" "R2"' in text
        # C? should be assigned C1
        assert '"Reference" "C1"' in text
        # No unannotated refs remain
        assert '"Reference" "R?"' not in text
        assert '"Reference" "C?"' not in text

    def test_avoids_collisions(self, tmp_path):
        """R1, R2 exist; R? must become R3, not R1 or R2."""
        sch = _write_sch(tmp_path, COLLISION_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            unannotated_only=True,
        )
        assert ret == 0
        text = sch.read_text()
        # Existing refs unchanged
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        # R? -> R3 (skipping 1 and 2)
        assert '"Reference" "R3"' in text
        assert '"Reference" "R?"' not in text

    def test_no_unannotated(self, tmp_path, capsys):
        """All refs already assigned -> no changes needed."""
        sch = _write_sch(tmp_path, SEQUENTIAL_SCHEMATIC)
        original = sch.read_text()
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            unannotated_only=True,
        )
        assert ret == 0
        assert sch.read_text() == original
        captured = capsys.readouterr()
        assert "No references to renumber" in captured.out

    def test_with_prefix_filter(self, tmp_path):
        """--unannotated-only with --prefix R: only unannotated R refs assigned."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            prefixes=["R"],
            unannotated_only=True,
        )
        assert ret == 0
        text = sch.read_text()
        # R? should be assigned
        assert '"Reference" "R?"' not in text
        assert '"Reference" "R2"' in text
        # C? should remain unannotated (prefix filter excludes C)
        assert '"Reference" "C?"' in text

    def test_with_start_from(self, tmp_path):
        """--unannotated-only with --start-from should respect start value."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            start_from=10,
            unannotated_only=True,
        )
        assert ret == 0
        text = sch.read_text()
        # R1 stays R1 (already assigned)
        assert '"Reference" "R1"' in text
        # R? should become R10 (start_from=10, no collision with R1)
        assert '"Reference" "R10"' in text
        # C? should become C10
        assert '"Reference" "C10"' in text

    def test_assign_numbers_unit_level(self):
        """_assign_numbers with unannotated_only preserves existing refs."""
        symbols = [
            {"reference": "R1", "prefix": "R", "number": 1, "unit_suffix": ""},
            {"reference": "R5", "prefix": "R", "number": 5, "unit_suffix": ""},
            {
                "reference": "R?",
                "prefix": "R",
                "number": None,
                "unit_suffix": "",
                "uuid": "uuid-rq",
            },
        ]
        raw = _assign_numbers(symbols, None, 1, unannotated_only=True)
        # Only the unannotated ref should be in the mapping
        assert len(raw) == 1
        assert raw["uuid-rq"]["old"] == "R?"
        # Should get R2 (1 and 5 are reserved, next available from 1 is 2)
        assert raw["uuid-rq"]["new"] == "R2"
        assert raw["uuid-rq"]["unannotated"] is True

    def test_per_sheet_mode(self, tmp_path):
        """--unannotated-only with --per-sheet scopes reserved numbers per sheet."""
        # Create a parent with R1 assigned and a child with R? unannotated
        parent_sch = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet
\t\t(at 150 50)
\t\t(size 20 20)
\t\t(property "Sheetname" "SubSheet"
\t\t\t(at 150 48 0)
\t\t)
\t\t(property "Sheetfile" "sub.kicad_sch"
\t\t\t(at 150 68 0)
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""
        child_sch = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "44444444-4444-4444-4444-444444444444")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R?"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "55555555-5555-5555-5555-555555555555")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001/33333333-3333-3333-3333-333333333333" (reference "R?") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/33333333-3333-3333-3333-333333333333" (page "2"))
\t)
)
"""
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(parent_sch)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(child_sch)

        ret = run_re_annotate(
            schematic_path=parent,
            dry_run=False,
            backup=False,
            per_sheet=True,
            unannotated_only=True,
        )
        assert ret == 0

        parent_text = parent.read_text()
        child_text = child.read_text()

        # Parent: R1 stays R1 (already assigned, unannotated_only)
        assert '"Reference" "R1"' in parent_text

        # Child: R? gets assigned R1 (per-sheet restart, no reserved in child sheet)
        assert '"Reference" "R1"' in child_text
        assert '"Reference" "R?"' not in child_text


# ---------------------------------------------------------------------------
# Bare unannotated symbols (no instances block, no pin entries)
# ---------------------------------------------------------------------------

# Schematic with one assigned R1 and one R? that has NO (instances) block and
# NO (pin) entries — a "bare" unannotated symbol as KiCad may write it when a
# component is placed but not yet annotated in a new project.
BARE_UNANNOTATED_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R?"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 50 0)
\t\t(property "Reference" "C?"
\t\t\t(at 120 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100nF"
\t\t\t(at 120 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 120 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

SPACE_INDENTED_BARE_UNANNOTATED_SCHEMATIC = _tabs_to_spaces(BARE_UNANNOTATED_SCHEMATIC)


class TestBareUnannotatedSymbols:
    """Tests for symbols with ``Reference "?"`` but no (instances) or (pin) blocks.

    These are "bare" unannotated symbols that KiCad may emit when a component
    is placed but not yet annotated in a freshly created project.
    """

    def test_extract_detects_bare_unannotated(self):
        """_extract_symbols_from_text must find bare R? and C? symbols."""
        symbols = _extract_symbols_from_text(BARE_UNANNOTATED_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert "R?" in refs
        assert "C?" in refs
        assert "R1" in refs

    def test_extract_uuid_for_bare_symbols(self):
        """UUIDs must be correctly extracted for bare unannotated symbols."""
        symbols = _extract_symbols_from_text(BARE_UNANNOTATED_SCHEMATIC)
        by_uuid = {s["uuid"]: s for s in symbols}
        assert "22222222-2222-2222-2222-222222222222" in by_uuid
        assert by_uuid["22222222-2222-2222-2222-222222222222"]["reference"] == "R?"
        assert "33333333-3333-3333-3333-333333333333" in by_uuid
        assert by_uuid["33333333-3333-3333-3333-333333333333"]["reference"] == "C?"

    def test_unannotated_only_annotates_bare_symbols(self, tmp_path):
        """--unannotated-only must assign numbers to bare unannotated symbols."""
        sch = _write_sch(tmp_path, BARE_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            unannotated_only=True,
        )
        assert ret == 0
        text = sch.read_text()
        # R1 must remain unchanged
        assert '"Reference" "R1"' in text
        # R? must be assigned R2 (next after R1)
        assert '"Reference" "R2"' in text
        # C? must be assigned C1
        assert '"Reference" "C1"' in text
        # No unannotated refs remain
        assert '"Reference" "R?"' not in text
        assert '"Reference" "C?"' not in text

    def test_creates_instances_block_for_bare_symbols(self, tmp_path):
        """Bare unannotated symbols must receive an (instances) block."""
        sch = _write_sch(tmp_path, BARE_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            unannotated_only=True,
        )
        assert ret == 0
        text = sch.read_text()
        # The instances block for the project must be present
        assert '(project "test"' in text
        # The new references must appear in instance entries
        assert '(reference "R2")' in text
        assert '(reference "C1")' in text

    def test_instances_block_contains_correct_path(self, tmp_path):
        """The created instances block must use the correct project UUID path."""
        sch = _write_sch(tmp_path, BARE_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            unannotated_only=True,
        )
        assert ret == 0
        text = sch.read_text()
        # Root UUID is 00000000-0000-0000-0000-000000000001 so path must be
        # /00000000-0000-0000-0000-000000000001
        assert "/00000000-0000-0000-0000-000000000001" in text

    def test_full_annotate_also_handles_bare_symbols(self, tmp_path):
        """Full re-annotate (not just --unannotated-only) must handle bare symbols too."""
        sch = _write_sch(tmp_path, BARE_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
        )
        assert ret == 0
        text = sch.read_text()
        # All symbols must be annotated
        assert '"Reference" "R?"' not in text
        assert '"Reference" "C?"' not in text

    def test_space_indented_bare_unannotated(self, tmp_path):
        """Bare unannotated symbols in space-indented files are also handled."""
        sch = _write_sch(tmp_path, SPACE_INDENTED_BARE_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            unannotated_only=True,
        )
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R2"' in text
        assert '"Reference" "C1"' in text
        assert '"Reference" "R?"' not in text
        assert '"Reference" "C?"' not in text

    def test_add_project_instance_bare_symbol(self):
        """_add_project_instance creates an instances block for bare symbols."""
        result = _add_project_instance(
            BARE_UNANNOTATED_SCHEMATIC,
            "22222222-2222-2222-2222-222222222222",
            "test",
            "/00000000-0000-0000-0000-000000000001",
            "R2",
        )
        assert "(instances" in result
        assert '(project "test"' in result
        assert '(reference "R2")' in result


# ---------------------------------------------------------------------------
# --include-power flag tests
# ---------------------------------------------------------------------------

# Schematic with unannotated power and flag symbols
POWER_UNANNOTATED_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "power:+5V")
\t\t(at 100 30 0)
\t\t(property "Reference" "#PWR?"
\t\t\t(at 100 26 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(property "Value" "+5V"
\t\t\t(at 100 23 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 30 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "#PWR?") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "power:PWR_FLAG")
\t\t(at 120 30 0)
\t\t(property "Reference" "#FLG?"
\t\t\t(at 120 26 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(property "Value" "PWR_FLAG"
\t\t\t(at 120 23 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 120 30 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "#FLG?") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# Schematic with existing #PWR01 and unannotated #PWR?
POWER_COLLISION_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "power:GND")
\t\t(at 100 70 0)
\t\t(property "Reference" "#PWR01"
\t\t\t(at 100 76 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(property "Value" "GND"
\t\t\t(at 100 73 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 70 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "44444444-4444-4444-4444-444444444444")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "#PWR01") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "power:+5V")
\t\t(at 100 30 0)
\t\t(property "Reference" "#PWR?"
\t\t\t(at 100 26 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(property "Value" "+5V"
\t\t\t(at 100 23 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 30 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "#PWR?") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""


class TestFormatReference:
    def test_normal_prefix(self):
        assert _format_reference("R", 1) == "R1"
        assert _format_reference("C", 10) == "C10"

    def test_power_prefix_zero_padded(self):
        assert _format_reference("#PWR", 1) == "#PWR01"
        assert _format_reference("#PWR", 10) == "#PWR10"
        assert _format_reference("#PWR", 99) == "#PWR99"
        assert _format_reference("#PWR", 100) == "#PWR100"

    def test_flag_prefix_zero_padded(self):
        assert _format_reference("#FLG", 1) == "#FLG01"
        assert _format_reference("#FLG", 12) == "#FLG12"

    def test_unit_suffix(self):
        assert _format_reference("U", 1, "A") == "U1A"
        assert _format_reference("#PWR", 1, "") == "#PWR01"


class TestIncludePower:
    def test_power_symbols_annotated_with_flag(self, tmp_path):
        """--include-power annotates #PWR? and #FLG? symbols."""
        sch = _write_sch(tmp_path, POWER_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            include_power=True,
        )
        assert ret == 0
        text = sch.read_text()
        # Power and flag symbols should be annotated with zero-padding
        assert '"Reference" "#PWR01"' in text
        assert '"Reference" "#FLG01"' in text
        assert '"Reference" "#PWR?"' not in text
        assert '"Reference" "#FLG?"' not in text
        # R1 should still be present (renumbered to R1)
        assert '"Reference" "R1"' in text

    def test_power_symbols_excluded_without_flag(self, tmp_path):
        """Without --include-power, #PWR? and #FLG? remain unannotated."""
        sch = _write_sch(tmp_path, POWER_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            include_power=False,
        )
        assert ret == 0
        text = sch.read_text()
        # Power/flag symbols should remain unannotated
        assert '"Reference" "#PWR?"' in text
        assert '"Reference" "#FLG?"' in text

    def test_power_annotation_collision_avoidance(self, tmp_path):
        """#PWR? avoids collision with existing #PWR01."""
        sch = _write_sch(tmp_path, POWER_COLLISION_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            unannotated_only=True,
            include_power=True,
        )
        assert ret == 0
        text = sch.read_text()
        # Existing #PWR01 should remain unchanged
        assert '"Reference" "#PWR01"' in text
        # #PWR? should become #PWR02 (avoiding collision with 01)
        assert '"Reference" "#PWR02"' in text
        assert '"Reference" "#PWR?"' not in text

    def test_power_annotation_zero_padding(self):
        """Number 1 produces #PWR01, number 10 produces #PWR10."""
        symbols = [
            {
                "reference": "#PWR?",
                "prefix": "#PWR",
                "number": None,
                "unit_suffix": "",
                "uuid": "uuid-pwr1",
            },
        ]
        raw = _assign_numbers(symbols, None, 1, include_power=True)
        assert raw["uuid-pwr1"]["new"] == "#PWR01"

        symbols2 = [
            {
                "reference": "#PWR?",
                "prefix": "#PWR",
                "number": None,
                "unit_suffix": "",
                "uuid": "uuid-pwr1",
            },
        ]
        raw2 = _assign_numbers(symbols2, None, 10, include_power=True)
        assert raw2["uuid-pwr1"]["new"] == "#PWR10"

    def test_include_power_without_unannotated_only(self, tmp_path):
        """Full renumber mode with --include-power re-sequences power symbols."""
        sch = _write_sch(tmp_path, POWER_COLLISION_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            include_power=True,
        )
        assert ret == 0
        text = sch.read_text()
        # Both power symbols should be sequentially numbered
        assert '"Reference" "#PWR01"' in text
        assert '"Reference" "#PWR02"' in text
        assert '"Reference" "#PWR?"' not in text

    def test_sym_prefix_always_excluded(self):
        """#SYM symbols are never annotated even with --include-power."""
        symbols = [
            {"reference": "#SYM1", "prefix": "#SYM", "number": 1, "unit_suffix": ""},
            {"reference": "R3", "prefix": "R", "number": 3, "unit_suffix": ""},
        ]
        mapping = _old_new_map(_assign_numbers(symbols, None, 1, include_power=True))
        assert "#SYM1" not in mapping
        assert mapping["R3"] == "R1"

    def test_json_output_includes_power_mappings(self, tmp_path, capsys):
        """--format json output includes power symbol mappings."""
        sch = _write_sch(tmp_path, POWER_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=True,
            backup=False,
            format="json",
            include_power=True,
        )
        assert ret == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        new_refs = {m["new"] for m in data["mappings"]}
        assert "#PWR01" in new_refs
        assert "#FLG01" in new_refs

    def test_unannotated_only_without_include_power_skips_power(self, tmp_path):
        """--unannotated-only without --include-power skips #PWR? symbols."""
        sch = _write_sch(tmp_path, POWER_UNANNOTATED_SCHEMATIC)
        sch.read_text()
        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            unannotated_only=True,
            include_power=False,
        )
        assert ret == 0
        text = sch.read_text()
        # Power symbols remain unannotated
        assert '"Reference" "#PWR?"' in text
        assert '"Reference" "#FLG?"' in text


# ---------------------------------------------------------------------------
# --pcb board sync
# ---------------------------------------------------------------------------


def _pcb_footprint(ref: str, uuid: str) -> str:
    return (
        '  (footprint "Resistor_SMD:R_0402"\n'
        '    (layer "F.Cu")\n'
        f'    (uuid "{uuid}")\n'
        "    (at 100 100)\n"
        f'    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))\n'
        '    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))\n'
        '    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))\n'
        '    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))\n'
        "  )\n"
    )


def _make_pcb(refs: list[str]) -> str:
    body = "".join(_pcb_footprint(ref, f"fp-{ref.lower()}") for ref in refs)
    return (
        "(kicad_pcb\n"
        "  (version 20240108)\n"
        '  (generator "test")\n'
        "  (layers\n"
        '    (0 "F.Cu" signal)\n'
        '    (31 "B.Cu" signal)\n'
        "  )\n"
        '  (net 0 "")\n'
        '  (net 1 "GND")\n'
        f"{body})\n"
    )


def _write_pcb(
    tmp_path: Path,
    content: str,
    name: str = "test.kicad_pcb",
) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


def _board_refs(path: Path) -> set[str]:
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(path)
    return {fp.reference for fp in pcb.footprints}


# Schematic whose symbols sort to R3 (y=50), R8 (y=80), R10 (y=110):
# mapping R3->R1, R8->R2, R10->R3 (R10->R3 chains onto R3->R1).
CHAIN_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R3"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R3") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R8"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R8") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 110 0)
\t\t(property "Reference" "R10"
\t\t\t(at 100 108 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "1k"
\t\t\t(at 100 112 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R10") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# Multi-unit: U2A (y=50), U2B (y=80), U5A (y=110) -> U1A, U1B, U2A.
# Board carries base refs U2, U5 -> U1, U2 (a collision chain).
MULTI_UNIT_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:OpAmp")
\t\t(at 100 50 0)
\t\t(unit 1)
\t\t(property "Reference" "U2A"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "half1"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "U2A") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:OpAmp")
\t\t(at 100 80 0)
\t\t(unit 2)
\t\t(property "Reference" "U2B"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "half2"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "U2B") (unit 2))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:OpAmp")
\t\t(at 100 110 0)
\t\t(unit 1)
\t\t(property "Reference" "U5A"
\t\t\t(at 100 108 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "other"
\t\t\t(at 100 112 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "U5A") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# KiCad stores each gate's unit number separately from the package reference.
NATIVE_UNIT_SCHEMATIC = (
    MULTI_UNIT_SCHEMATIC.replace('"U2A"', '"U2"').replace('"U2B"', '"U2"').replace('"U5A"', '"U5"')
)


class TestPcbSync:
    def test_native_units_share_one_paired_reference(self, tmp_path, capsys):
        sch = _write_sch(tmp_path, NATIVE_UNIT_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["U2", "U5"]))
        args = [
            "sch",
            "re-annotate",
            str(sch),
            "--pcb",
            str(pcb),
            "--start-from",
            "81",
            "--format",
            "json",
        ]
        assert kct(args) == 0
        result = json.loads(capsys.readouterr().out)
        assert [(m["old"], m["new"]) for m in result["mappings"]] == [
            ("U2", "U81"),
            ("U2", "U81"),
            ("U5", "U82"),
        ]
        assert result["pcb"] == {"path": str(pcb), "renamed": 2, "warnings": []}
        assert _board_refs(pcb) == {"U81", "U82"}
        assert sch.read_text().count('(property "Reference" "U81"') == 2
        assert sch.read_text().count('(reference "U81")') == 2

    def test_padded_source_is_distinct_from_unpadded_footprint(self, tmp_path, capsys):
        from kicad_tools.schema.pcb import PCB

        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC.replace('"R5"', '"R01"'))
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "R01", "C3"]))
        args = [
            "sch",
            "re-annotate",
            str(sch),
            "--pcb",
            str(pcb),
            "--prefix",
            "R",
            "--start-from",
            "81",
            "--format",
            "json",
        ]
        assert kct(args) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["pcb"] == {"path": str(pcb), "renamed": 2, "warnings": []}
        assert {fp.uuid: fp.reference for fp in PCB.load(pcb).footprints} == {
            "fp-r1": "R81",
            "fp-r01": "R82",
            "fp-c3": "C3",
        }
        assert all(f'(property "Reference" "{ref}"' in sch.read_text() for ref in ("R81", "R82"))

    @pytest.mark.parametrize(
        ("old", "new"), [("(unit 2)", "(unit 1)"), ("Device:OpAmp", "Device:Other")]
    )
    def test_ambiguous_native_package_is_refused_before_backup(self, tmp_path, capsys, old, new):
        sch = _write_sch(tmp_path, NATIVE_UNIT_SCHEMATIC.replace(old, new, 1))
        pcb = _write_pcb(tmp_path, _make_pcb(["U2", "U5"]))
        before = {p: p.read_bytes() for p in tmp_path.iterdir()}
        assert kct(["sch", "re-annotate", str(sch), "--pcb", str(pcb), "--backup"]) == 1
        assert "Ambiguous schematic reference 'U2'" in capsys.readouterr().err
        assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before

    def test_units_on_different_sheets_cannot_split_one_footprint(self, tmp_path, capsys):
        parent = PARENT_SCHEMATIC.replace('"R3"', '"U9"').replace('"R7"', '"U1"')
        child = (
            CHILD_SCHEMATIC.replace('"R14"', '"U1"')
            .replace('(property "Reference" "U1"', '(unit 2)\n\t\t(property "Reference" "U1"')
            .replace('(reference "U1") (unit 1)', '(reference "U1") (unit 2)')
        )
        sch = _write_sch(tmp_path, parent.replace("Device:R", "Device:OpAmp"))
        sub = _write_sch(tmp_path, child.replace("Device:R", "Device:OpAmp"), "sub.kicad_sch")
        pcb = _write_pcb(tmp_path, _make_pcb(["U9", "U1", "C5"]))
        before = {p: p.read_bytes() for p in tmp_path.iterdir()}
        args = ["sch", "re-annotate", str(sch), "--pcb", str(pcb), "--prefix", "U"]
        assert kct([*args, "--per-sheet", "--backup"]) == 1
        assert "Units of 'U1' would receive different references" in capsys.readouterr().err
        assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before

        assert kct(args) == 0
        assert '(property "Reference" "U2"' in sch.read_text()
        assert '(property "Reference" "U2"' in sub.read_text()
        assert _board_refs(pcb) == {"U1", "U2", "C5"}

    def test_board_references_match_renamed_schematic(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "R5", "C3"]))

        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False, pcb_path=pcb)
        assert ret == 0

        # Schematic: R1, R5 -> R1, R2 and C3 -> C1
        assert _board_refs(pcb) == {"R1", "R2", "C1"}
        sch_text = sch.read_text()
        assert '"Reference" "R2"' in sch_text
        assert '"Reference" "C1"' in sch_text

    def test_collision_chain_applied_safely(self, tmp_path):
        sch = _write_sch(tmp_path, CHAIN_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R3", "R8", "R10"]))

        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False, pcb_path=pcb)
        assert ret == 0

        assert _board_refs(pcb) == {"R1", "R2", "R3"}
        assert "_TEMP_" not in pcb.read_text()

    def test_multi_unit_refs_map_to_board_base_refs(self, tmp_path):
        sch = _write_sch(tmp_path, MULTI_UNIT_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["U2", "U5"]))

        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False, pcb_path=pcb)
        assert ret == 0

        assert _board_refs(pcb) == {"U1", "U2"}
        sch_text = sch.read_text()
        assert '"Reference" "U1A"' in sch_text
        assert '"Reference" "U1B"' in sch_text
        assert '"Reference" "U2A"' in sch_text

    def test_dry_run_leaves_both_files_untouched(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "R5", "C3"]))
        sch_before = sch.read_text()
        pcb_before = pcb.read_text()

        ret = run_re_annotate(schematic_path=sch, dry_run=True, backup=False, pcb_path=pcb)
        assert ret == 0

        assert sch.read_text() == sch_before
        assert pcb.read_text() == pcb_before

    def test_missing_pcb_file_exits_1_schematic_untouched(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        sch_before = sch.read_text()

        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=False,
            backup=False,
            pcb_path=tmp_path / "missing.kicad_pcb",
        )
        assert ret == 1
        assert sch.read_text() == sch_before

    def test_unloadable_pcb_exits_1_before_schematic_write(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        sch_before = sch.read_text()
        pcb = _write_pcb(tmp_path, "(not a pcb\n")

        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False, pcb_path=pcb)
        assert ret == 1
        assert sch.read_text() == sch_before

    def test_unannotated_entry_skipped_on_board_with_warning(self, tmp_path, capsys):
        # UNANNOTATED_SCHEMATIC: R1 stays, R? -> R2 (schematic-only change).
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1"]))
        pcb_before = pcb.read_text()

        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False, pcb_path=pcb)
        assert ret == 0

        assert '"Reference" "R2"' in sch.read_text()
        assert pcb.read_text() == pcb_before
        captured = capsys.readouterr()
        assert "R?" in captured.out
        assert "ambiguous" in captured.out

    def test_board_ref_absent_from_pcb_warns_not_errors(self, tmp_path, capsys):
        # Board lacks the R5 footprint the schematic renames.
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "C3"]))

        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False, pcb_path=pcb)
        assert ret == 0

        assert _board_refs(pcb) == {"R1", "C1"}
        captured = capsys.readouterr()
        assert "'R5' not found in PCB" in captured.out
        assert '"Reference" "R2"' in sch.read_text()

    def test_json_output_single_document_with_pcb_section(self, tmp_path, capsys):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "R5", "C3"]))

        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=True,
            backup=False,
            format="json",
            pcb_path=pcb,
        )
        assert ret == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["dry_run"] is True
        assert data["pcb"]["path"] == str(pcb)
        assert data["pcb"]["renamed"] == 2
        assert isinstance(data["pcb"]["warnings"], list)

    def test_json_empty_mapping_still_single_document(self, tmp_path, capsys):
        sch = _write_sch(tmp_path, SEQUENTIAL_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "C1"]))

        ret = run_re_annotate(
            schematic_path=sch,
            dry_run=True,
            backup=False,
            format="json",
            pcb_path=pcb,
        )
        assert ret == 0

        data = json.loads(capsys.readouterr().out)
        assert data["total"] == 0
        assert data["pcb"]["renamed"] == 0

    def test_backup_covers_board_too(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "R5", "C3"]))

        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=True, pcb_path=pcb)
        assert ret == 0

        assert list(tmp_path.glob("test_backup_*.kicad_pcb"))
        assert _board_refs(pcb) == {"R1", "R2", "C1"}


class TestPcbPairedWriteSafety:
    """Issue #10145 review: paired save must be recoverable and collision-free."""

    def test_late_pcb_write_failure_is_recoverable(self, tmp_path, monkeypatch):
        from kicad_tools.schema.pcb import PCB

        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "R5", "C3"]))
        sch_before = sch.read_bytes()
        pcb_before = pcb.read_bytes()

        def boom(self, path):
            raise OSError("Operation not permitted")

        with monkeypatch.context() as m:
            m.setattr(PCB, "save", boom)
            ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False, pcb_path=pcb)
        assert ret == 1
        # Neither file changed, so the identical command can be retried.
        assert sch.read_bytes() == sch_before
        assert pcb.read_bytes() == pcb_before

        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False, pcb_path=pcb)
        assert ret == 0
        assert _board_refs(pcb) == {"R1", "R2", "C1"}
        assert '"Reference" "R2"' in sch.read_text()

    def test_schematic_write_failure_rolls_back_board(self, tmp_path, monkeypatch):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "R5", "C3"]))
        sch_before = sch.read_bytes()
        pcb_before = pcb.read_bytes()

        real_write_text = Path.write_text

        def fail_sch(self, *a, **kw):
            if self.suffix == ".kicad_sch":
                raise OSError("disk full")
            return real_write_text(self, *a, **kw)

        monkeypatch.setattr(Path, "write_text", fail_sch)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False, pcb_path=pcb)
        monkeypatch.undo()

        assert ret == 1
        assert sch.read_bytes() == sch_before
        assert pcb.read_bytes() == pcb_before

        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False, pcb_path=pcb)
        assert ret == 0
        assert _board_refs(pcb) == {"R1", "R2", "C1"}

    def test_schematic_backup_failure_leaves_both_sources_untouched(
        self, tmp_path, monkeypatch, capsys
    ):
        """A refused schematic backup must not leave the board renamed."""
        import kicad_tools.cli.sch_re_annotate as mod

        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "R5", "C3"]))
        sch_before = sch.read_bytes()
        pcb_before = pcb.read_bytes()

        real_backup = mod.create_backup

        def refuse_sch_backup(path):
            if path.suffix == ".kicad_sch":
                raise PermissionError("read-only directory")
            return real_backup(path)

        monkeypatch.setattr(mod, "create_backup", refuse_sch_backup)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=True, pcb_path=pcb)
        monkeypatch.undo()

        assert ret == 1
        assert "backup" in capsys.readouterr().err.lower()
        assert sch.read_bytes() == sch_before
        assert pcb.read_bytes() == pcb_before

        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=True, pcb_path=pcb)
        assert ret == 0
        assert _board_refs(pcb) == {"R1", "R2", "C1"}
        assert '"Reference" "R2"' in sch.read_text()

    def test_per_sheet_duplicate_board_targets_rejected_before_write(self, tmp_path, capsys):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R3", "R7", "R14", "C5"]))
        before = (parent.read_bytes(), child.read_bytes(), pcb.read_bytes())

        ret = run_re_annotate(
            schematic_path=parent,
            dry_run=False,
            backup=False,
            per_sheet=True,
            pcb_path=pcb,
        )
        assert ret == 1
        assert "duplicate" in capsys.readouterr().err
        assert (parent.read_bytes(), child.read_bytes(), pcb.read_bytes()) == before

    def test_refused_backup_json_emits_only_after_successful_publication(
        self, tmp_path, monkeypatch, capsys
    ):
        """Refused backup under --format json must not report completed renames."""
        import kicad_tools.cli.sch_re_annotate as mod

        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "R5", "C3"]))
        sch_before = sch.read_bytes()
        pcb_before = pcb.read_bytes()

        real_backup = mod.create_backup

        def refuse_sch_backup(path):
            if path.suffix == ".kicad_sch":
                raise PermissionError("read-only directory")
            return real_backup(path)

        monkeypatch.setattr(mod, "create_backup", refuse_sch_backup)
        ret = main([str(sch), "--pcb", str(pcb), "--backup", "--format", "json"])
        monkeypatch.undo()

        captured = capsys.readouterr()
        assert ret == 1
        assert captured.out.strip() == ""
        assert "backup" in captured.err.lower()
        assert "no files were modified" in captured.err.lower()
        assert sch.read_bytes() == sch_before
        assert pcb.read_bytes() == pcb_before

        # Dry run still emits the proposed mapping in JSON.
        ret = main([str(sch), "--pcb", str(pcb), "--dry-run", "--format", "json"])
        assert ret == 0
        dry_data = json.loads(capsys.readouterr().out)
        assert dry_data["dry_run"] is True
        assert dry_data["total"] == 2
        assert dry_data["pcb"]["renamed"] == 2

        # Writable retry publishes both files and reports completed renames.
        ret = main([str(sch), "--pcb", str(pcb), "--backup", "--format", "json"])
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert data["dry_run"] is False
        assert data["total"] == 2
        assert data["pcb"]["path"] == str(pcb)
        assert data["pcb"]["renamed"] == 2
        assert data["pcb"]["warnings"] == []
        assert _board_refs(pcb) == {"R1", "R2", "C1"}
        assert '"Reference" "R2"' in sch.read_text()

    def test_write_failure_json_does_not_claim_completed_renames(
        self, tmp_path, monkeypatch, capsys
    ):
        """A late PCB write refusal under --format json must not report completed renames."""
        from kicad_tools.schema.pcb import PCB

        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "R5", "C3"]))
        sch_before = sch.read_bytes()
        pcb_before = pcb.read_bytes()

        def boom(self, path):
            raise OSError("Operation not permitted")

        def refuse_restore(path, data):
            assert path == pcb
            raise PermissionError("board is read-only")

        with monkeypatch.context() as m:
            m.setattr(PCB, "save", boom)
            m.setattr(Path, "write_bytes", refuse_restore)
            ret = main([str(sch), "--pcb", str(pcb), "--format", "json"])

        captured = capsys.readouterr()
        assert ret == 1
        assert captured.out.strip() == ""
        assert "Operation not permitted" in captured.err
        assert "could not restore" not in captured.err.lower()
        assert sch.read_bytes() == sch_before
        assert pcb.read_bytes() == pcb_before

        # Retry succeeds and reports completed renames.
        ret = main([str(sch), "--pcb", str(pcb), "--format", "json"])
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert data["dry_run"] is False
        assert data["total"] == 2
        assert data["pcb"]["renamed"] == 2
        assert _board_refs(pcb) == {"R1", "R2", "C1"}

    @pytest.mark.parametrize("board_restore_error", [None, "before_write", "after_write"])
    def test_partial_schematic_write_reports_final_pair(
        self, tmp_path, monkeypatch, capsys, board_restore_error
    ):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        pcb = _write_pcb(tmp_path, _make_pcb(["R1", "R5", "C3"]))
        sch_before, pcb_before = sch.read_bytes(), pcb.read_bytes()
        write_text = Path.write_text
        write_bytes = Path.write_bytes

        def interrupt_schematic(path, text, *args, **kwargs):
            if path == sch:
                write_bytes(path, b"part of the new schematic")
                raise OSError("schematic write interrupted")
            return write_text(path, text, *args, **kwargs)

        def restore(path, data):
            if path == pcb and board_restore_error == "before_write":
                raise PermissionError("board restore refused")
            count = write_bytes(path, data)
            if path == pcb and board_restore_error == "after_write":
                raise OSError("failure after the bytes reached disk")
            return count

        with monkeypatch.context() as m:
            m.setattr(Path, "write_text", interrupt_schematic)
            m.setattr(Path, "write_bytes", restore)
            ret = main([str(sch), "--pcb", str(pcb), "--format", "json"])

        captured = capsys.readouterr()
        assert ret == 1 and not captured.out.strip()
        assert "schematic write interrupted" in captured.err
        assert sch.read_bytes() == sch_before
        if board_restore_error == "before_write":
            assert pcb.read_bytes() != pcb_before
            assert f"could not restore {pcb}" in captured.err
            assert "may be out of sync" in captured.err.lower()
            assert "no files were modified" not in captured.err.lower()
            assert "match their original contents" not in captured.err.lower()
        else:
            assert pcb.read_bytes() == pcb_before
            assert "could not restore" not in captured.err.lower()
            assert "may be out of sync" not in captured.err.lower()
