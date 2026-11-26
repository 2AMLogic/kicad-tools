"""
Package information extraction from datasheets.

Provides data models and extraction logic for IC package information
including dimensions, pin counts, and pitch values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Package type definitions with default properties
PACKAGE_TYPES: dict[str, dict[str, str | float]] = {
    # QFP family
    "LQFP": {"type": "qfp", "profile": "low"},
    "TQFP": {"type": "qfp", "profile": "thin"},
    "QFP": {"type": "qfp"},
    "PQFP": {"type": "qfp", "profile": "plastic"},
    "CQFP": {"type": "qfp", "profile": "ceramic"},
    # QFN/DFN family
    "QFN": {"type": "qfn"},
    "DFN": {"type": "dfn"},
    "WQFN": {"type": "qfn", "profile": "very_thin"},
    "UQFN": {"type": "qfn", "profile": "ultra_thin"},
    "VQFN": {"type": "qfn", "profile": "very_thin"},
    "HVQFN": {"type": "qfn", "profile": "thin"},
    "SON": {"type": "qfn"},
    "WSON": {"type": "qfn", "profile": "thin"},
    # SOIC family
    "SOIC": {"type": "soic"},
    "SOP": {"type": "soic"},
    "SSOP": {"type": "soic", "pitch": 0.65},
    "TSSOP": {"type": "soic", "pitch": 0.65, "profile": "thin"},
    "MSOP": {"type": "soic", "pitch": 0.65, "profile": "mini"},
    "TSOP": {"type": "soic", "profile": "thin"},
    "QSOP": {"type": "soic", "pitch": 0.635},
    "VSOP": {"type": "soic", "profile": "very_small"},
    # SOT family
    "SOT-23": {"type": "sot", "variant": "SOT-23"},
    "SOT-223": {"type": "sot", "variant": "SOT-223"},
    "SOT-89": {"type": "sot", "variant": "SOT-89"},
    "SOT-363": {"type": "sot", "variant": "SOT-363"},
    "SOT-143": {"type": "sot", "variant": "SOT-143"},
    "SOT-323": {"type": "sot", "variant": "SOT-323"},
    "SOT-523": {"type": "sot", "variant": "SOT-523"},
    "SOT-666": {"type": "sot", "variant": "SOT-666"},
    "SC-70": {"type": "sot", "variant": "SC-70"},
    # Through-hole
    "DIP": {"type": "dip"},
    "PDIP": {"type": "dip", "profile": "plastic"},
    "CDIP": {"type": "dip", "profile": "ceramic"},
    "CERDIP": {"type": "dip", "profile": "ceramic"},
    # BGA family
    "BGA": {"type": "bga"},
    "FBGA": {"type": "bga", "profile": "fine_pitch"},
    "LFBGA": {"type": "bga", "profile": "low_profile"},
    "TFBGA": {"type": "bga", "profile": "thin"},
    "WLCSP": {"type": "bga", "variant": "wlcsp"},
    "VFBGA": {"type": "bga", "profile": "very_fine"},
    "CABGA": {"type": "bga", "profile": "chip_array"},
    "CTBGA": {"type": "bga", "profile": "chip_thin"},
    # LGA
    "LGA": {"type": "lga"},
    # PLCC
    "PLCC": {"type": "plcc"},
    # TO packages
    "TO-220": {"type": "to", "variant": "TO-220"},
    "TO-252": {"type": "to", "variant": "TO-252"},
    "TO-263": {"type": "to", "variant": "TO-263"},
    "TO-92": {"type": "to", "variant": "TO-92"},
    "DPAK": {"type": "to", "variant": "TO-252"},
    "D2PAK": {"type": "to", "variant": "TO-263"},
}


@dataclass
class PackageInfo:
    """
    Information about an IC package extracted from a datasheet.

    Attributes:
        name: Package designation (e.g., "LQFP48", "SOIC-8")
        type: Package family type (e.g., "qfp", "soic", "qfn")
        pin_count: Number of pins/balls
        body_width: Package body width in mm
        body_length: Package body length in mm
        pitch: Pin pitch in mm
        height: Package height in mm (if available)
        exposed_pad: Exposed pad dimensions as (width, height) in mm
        source_page: Page number where package info was found
        pitch_inferred: Pitch was supplied by a package-family default
        confidence: Confidence score (0-1) for the extraction
    """

    name: str
    type: str
    pin_count: int
    body_width: float
    body_length: float
    pitch: float
    height: float | None = None
    exposed_pad: tuple[float, float] | None = None
    source_page: int = 0
    confidence: float = 0.0
    pitch_inferred: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {
            "name": self.name,
            "type": self.type,
            "pin_count": self.pin_count,
            "body_width": self.body_width,
            "body_length": self.body_length,
            "pitch": self.pitch,
            "pitch_inferred": self.pitch_inferred,
            "height": self.height,
            "exposed_pad": list(self.exposed_pad) if self.exposed_pad else None,
            "source_page": self.source_page,
            "confidence": self.confidence,
        }


@dataclass
class PackageExtractionResult:
    """Result of package extraction from a datasheet."""

    packages: list[PackageInfo] = field(default_factory=list)
    source_pages: list[int] = field(default_factory=list)
    extraction_method: str = "combined"

    def __len__(self) -> int:
        return len(self.packages)

    def __iter__(self):
        return iter(self.packages)


def parse_package_name(name: str) -> dict:
    """
    Parse a package name to extract type and pin count.

    Args:
        name: Package name string (e.g., "LQFP48", "SOIC-8", "QFN-32")

    Returns:
        Dictionary with parsed components
    """
    result: dict = {
        "original": name,
        "type": None,
        "pin_count": None,
        "body_size": None,
        "pitch": None,
    }

    # Normalize the name
    name_upper = name.upper().replace("_", "-").replace(" ", "")

    # Try to match known package types
    for pkg_name, pkg_info in PACKAGE_TYPES.items():
        if name_upper.startswith(pkg_name.replace("-", "")):
            result["type"] = str(pkg_info.get("type", pkg_name.lower()))

            # Extract pin count (digits after package name)
            remaining = name_upper[len(pkg_name.replace("-", "")) :]
            pin_match = re.match(r"[-]?(\d+)", remaining)
            if pin_match:
                result["pin_count"] = int(pin_match.group(1))

            # Check for default pitch
            if "pitch" in pkg_info:
                result["pitch"] = float(pkg_info["pitch"])

            break

    # Try to extract body size from name (e.g., "7x7", "7.0x7.0")
    size_match = re.search(r"(\d+\.?\d*)[xX](\d+\.?\d*)", name)
    if size_match:
        width = float(size_match.group(1))
        length = float(size_match.group(2))
        result["body_size"] = (width, length)

    # Try to extract pitch from name (e.g., "P0.5", "_0.5mm")
    pitch_match = re.search(r"(?:(?<![A-Za-z0-9])[Pp]|_)(\d+(?:\.\d+)?)\s*mm\b", name)
    if pitch_match:
        result["pitch"] = float(pitch_match.group(1))

    return result


_PITCH_UNITS = ("mm", "millimeter", "millimeters", "millimetre", "millimetres", "mil", "mils")


def _pitch_token_unit(tail: str) -> str | None:
    """Classify the text that follows a pitch number.

    Returns the lower-cased unit ("" when none), or None when a letter or underscore glued
    onto the number or its unit makes the token incomplete (``1.27B``,
    ``1.27mmA``, ``1.27 mmA``).
    """
    # Scan the entire Unicode suffix so an unsupported unit like µm stays intact.
    word = re.match(r"(\s*)([^\W\d]*)", tail)
    assert word is not None
    gap, suffix = word.group(1), word.group(2)
    lowered = suffix.lower()
    if lowered in _PITCH_UNITS:
        rest = tail[word.end() :]
        return None if rest[:1].isdigit() else lowered
    if not suffix:
        return ""
    if gap and not lowered.startswith(("mm", "mil")):
        return ""  # a separate word, not part of the measurement
    return None


def extract_dimension_from_text(text: str) -> dict[str, float]:
    """
    Extract dimension values from text.

    Args:
        text: Text containing dimension information

    Returns:
        Dictionary of dimension name to value mappings
    """
    dimensions: dict[str, float] = {}

    # Pattern for dimensions with labels (e.g., "A = 7.0", "D = 7.0mm")
    labeled_pattern = re.compile(
        r"([A-Za-z][A-Za-z0-9]*)\s*[=:]\s*(\d+\.?\d*)\s*(mm|mil)?",
        re.IGNORECASE,
    )

    for match in labeled_pattern.finditer(text):
        # JEDEC uses uppercase E for body length and lowercase e for pitch.
        label = "PITCH" if match.group(1) == "e" else match.group(1).upper()
        if label == "PITCH":
            # Only the pitch-specific parser below may set PITCH, so a token
            # it rejects (e.g. "e=1.27mmA") cannot slip in through here.
            continue
        value = float(match.group(2))
        unit = match.group(3)

        # Convert mil to mm if needed
        if unit and unit.lower() == "mil":
            value *= 0.0254

        dimensions[label] = value

    # Pattern for body dimensions (D x E)
    body_pattern = re.compile(
        r"(?:body|package)\s*(?:size)?[:\s]*(\d+\.?\d*)\s*[xX×]\s*(\d+\.?\d*)",
        re.IGNORECASE,
    )
    body_match = body_pattern.search(text)
    if body_match:
        dimensions["D"] = float(body_match.group(1))
        dimensions["E"] = float(body_match.group(2))

    # Pattern for pitch (e.g., "pitch: 0.5mm", "e = 0.5"). The number is
    # validated for a complete token below: a letter glued onto the number or
    # its unit makes the candidate ambiguous and the search moves on.
    pitch_prefix = re.compile(
        r"(?<![A-Za-z0-9])(?:(?i:pitch|p)\s*[=:]?|e\s*[=:])\s*(\d+(?:\.\d+)?)"
    )
    for pitch_match in pitch_prefix.finditer(text):
        unit = _pitch_token_unit(text[pitch_match.end() :])
        if unit is None:
            continue
        value = float(pitch_match.group(1))
        dimensions["PITCH"] = value * 0.0254 if unit in ("mil", "mils") else value
        return dimensions
    # Trailing-label form ("0.8 mm pitch"). A "pitch" that is itself followed by
    # a number labels that number (a rejected candidate above), not the one
    # before it ("7 x 7 mm pitch = 0.65mmX").
    trailing_match = re.search(
        r"(\d+(?:\.\d+)?)\s*((?i:mm|mils?))\s+(?i:pitch)\b(?!\s*[=:]?\s*\d)", text
    )
    if trailing_match:
        value = float(trailing_match.group(1))
        unit = trailing_match.group(2).lower()
        dimensions["PITCH"] = value * 0.0254 if unit in ("mil", "mils") else value

    return dimensions


def get_default_pitch(package_type: str, pin_count: int = 0) -> float:
    """
    Get default pitch for a package type.

    Args:
        package_type: Package type (e.g., "qfp", "soic")
        pin_count: Number of pins (affects some defaults)

    Returns:
        Default pitch in mm
    """
    defaults = {
        "qfp": 0.5,  # Standard QFP pitch
        "qfn": 0.5,
        "dfn": 0.5,
        "soic": 1.27,  # 50 mil
        "sop": 1.27,
        "ssop": 0.65,
        "tssop": 0.65,
        "msop": 0.65,
        "dip": 2.54,  # 100 mil
        "bga": 0.8,  # Common BGA pitch
        "sot": 0.95,  # SOT-23 typical
        "plcc": 1.27,
        "lga": 0.5,
        "to": 2.54,
    }

    return defaults.get(package_type.lower(), 0.5)


def get_default_body_size(package_type: str, pin_count: int) -> tuple[float, float]:
    """
    Estimate default body size based on package type and pin count.

    Args:
        package_type: Package type (e.g., "qfp", "soic")
        pin_count: Number of pins

    Returns:
        Tuple of (width, length) in mm
    """
    pkg_type = package_type.lower()

    if pkg_type in ("qfp", "lqfp", "tqfp"):
        # QFP sizes scale with pin count
        if pin_count <= 32 or pin_count <= 48:
            return (7.0, 7.0)
        elif pin_count <= 64:
            return (10.0, 10.0)
        elif pin_count <= 100:
            return (14.0, 14.0)
        elif pin_count <= 144:
            return (20.0, 20.0)
        else:
            return (24.0, 24.0)

    elif pkg_type in ("qfn", "dfn"):
        # QFN sizes
        if pin_count <= 8:
            return (2.0, 2.0)
        elif pin_count <= 16:
            return (3.0, 3.0)
        elif pin_count <= 24:
            return (4.0, 4.0)
        elif pin_count <= 32:
            return (5.0, 5.0)
        elif pin_count <= 48:
            return (6.0, 6.0)
        else:
            return (7.0, 7.0)

    elif pkg_type in ("soic", "sop"):
        # SOIC: 2 rows, width depends on pin count
        pins_per_side = pin_count // 2
        length = pins_per_side * 1.27  # 50 mil pitch typical
        if pin_count <= 8:
            return (3.9, max(4.9, length))
        elif pin_count <= 16:
            return (3.9, max(9.9, length))
        else:
            return (7.5, max(12.8, length))

    elif pkg_type in ("ssop", "tssop", "msop"):
        pins_per_side = pin_count // 2
        length = pins_per_side * 0.65
        if pin_count <= 8:
            return (3.0, max(3.0, length))
        elif pin_count <= 16:
            return (4.4, max(5.0, length))
        else:
            return (4.4, max(6.5, length))

    elif pkg_type == "dip":
        pins_per_side = pin_count // 2
        length = pins_per_side * 2.54
        if pin_count <= 8:
            return (6.35, max(9.91, length))
        elif pin_count <= 20:
            return (6.35, max(24.3, length))
        else:
            return (15.24, max(30.0, length))

    elif pkg_type == "bga":
        # BGA: approximate square
        import math

        side = math.ceil(math.sqrt(pin_count))
        size = side * 0.8  # Typical 0.8mm pitch
        return (size, size)

    # Default fallback
    return (5.0, 5.0)
