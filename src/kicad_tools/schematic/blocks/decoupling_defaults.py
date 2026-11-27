"""Supply capacitor values and bank sizes used by schematic blocks."""

BYPASS = "100nF"
BULK = "10uF"
MCU_BYPASS = (BYPASS,) * 4
MCU_ARRAY_BULK = "4.7uF"
CURRENT_SENSE_BYPASS = (BYPASS,)
GATE_DRIVER_BYPASS = (BULK, BYPASS)
LDO_OUTPUT = (BULK, BYPASS)
CASCADE_LDO_OUTPUT = (BULK,)
BUCK_INPUT = "100uF"
BUCK_OUTPUT = "220uF"
BUCK_12V_OUTPUT = "330uF"
BARREL_INPUT = "100uF"


class DefaultValue(str):
    """An omitted argument or a factory preset, forwarded through block composition.

    Callers supply ordinary strings, including values equal to a preset. Only
    the placement helper inspects this marker. The placed symbol holds a plain
    string, so reusing its value in another block remains a caller override.
    """


def default_bank(values: tuple[str, ...]) -> list[str]:
    """Apply a preset bank while preserving the provenance of each capacitor."""
    return [DefaultValue(value) for value in values]
