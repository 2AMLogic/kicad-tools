"""Offline Basic/Preferred JLCPCB alternative suggestions.

Pure ranking helpers used by ``kct parts alternatives``. They operate on
:class:`~kicad_tools.parts.models.Part` objects so they can be unit-tested
without sqlite; the catalog query itself lives in
:func:`suggest_alternatives`.

Ranking rule: candidates must be in stock and not the source part; they are
ordered Basic, then Preferred, then Extended, then lowest unit price, then
highest stock. Only candidates with a strictly better library tier than the
source, or the same tier at a lower unit price, are suggested.
"""

from __future__ import annotations

import re
from fractions import Fraction

from .jlcparts_catalog import JlcpartsCatalog
from .models import Part

_TIER_BASIC = 0
_TIER_PREFERRED = 1
_TIER_EXTENDED = 2

_TIER_NAMES = {_TIER_BASIC: "basic", _TIER_PREFERRED: "preferred", _TIER_EXTENDED: "extended"}

# Candidate pool size pulled from the catalog before ranking in Python.
_POOL_LIMIT = 500


def library_tier(part: Part) -> int:
    """Return 0 for Basic, 1 for Preferred, 2 for Extended."""
    if part.is_basic:
        return _TIER_BASIC
    if part.is_preferred:
        return _TIER_PREFERRED
    return _TIER_EXTENDED


def library_tier_name(part: Part) -> str:
    """Return ``"basic"``, ``"preferred"`` or ``"extended"``."""
    return _TIER_NAMES[library_tier(part)]


def _price_key(part: Part) -> float:
    price = part.best_price
    return price if price is not None else float("inf")


def rank_alternatives(source: Part, candidates: list[Part], limit: int = 5) -> list[Part]:
    """Filter and rank *candidates* as substitutes for *source*.

    Excludes the source itself and out-of-stock parts, keeps only candidates
    with a strictly better library tier or the same tier at a lower unit
    price, and orders them Basic > Preferred > Extended, then cheapest, then
    highest stock.
    """
    src_tier = library_tier(source)
    src_price = _price_key(source)
    kept = []
    for cand in candidates:
        if cand.lcsc_part.upper() == source.lcsc_part.upper() or cand.stock <= 0:
            continue
        if not _compatible(source, cand):
            continue
        tier = library_tier(cand)
        if tier < src_tier or (tier == src_tier and _price_key(cand) < src_price):
            kept.append(cand)
    kept.sort(key=lambda p: (library_tier(p), _price_key(p), -p.stock, p.lcsc_part))
    return kept[: max(limit, 0)]


# jlcparts uses these description and attribute names for individual chip
# resistors and multilayer ceramic capacitors. Arrays and active parts need
# pinout/application evidence that the catalog cannot provide.
_PASSIVE_FAMILIES = (
    ("Chip Resistor - Surface Mount", ("Resistance", "Type", "Tolerance", "Power(Watts)")),
    (
        "Multilayer Ceramic Capacitors MLCC - SMD/SMT",
        ("Capacitance", "Temperature Coefficient", "Tolerance", "Voltage Rating"),
    ),
)


def _family(part: Part) -> tuple[str, ...]:
    for family, required in _PASSIVE_FAMILIES:
        if family in part.description:
            return required
    return ()


def _identified(value: str) -> bool:
    return any(c.isalnum() for c in value) and value.strip().casefold() not in {
        "n/a",
        "na",
        "unknown",
        "none",
        "null",
        "unspecified",
    }


def _has_identity(part: Part) -> bool:
    return _identified(part.manufacturer) and _identified(part.mfr_part)


# A description may repeat a structured rating. Require all reported values
# for a given rating to agree before using it for substitution.
_NUMBER = r"(?:\d+/[1-9]\d*|\d*\.\d+|\d+)"


def _quantity(unit: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w./+-])(?:(?:±|\+/-)\s*)?({_NUMBER})\s*([mkµu]?){unit}(?!\w)")


_RATINGS = {
    "Voltage Rating": _quantity("V"),
    "Power(Watts)": _quantity("W"),
    "Tolerance": _quantity("%"),
}
_FACTORS = {
    "": Fraction(1),
    "k": Fraction(1000),
    "m": Fraction(1, 1000),
    "u": Fraction(1, 1000000),
    "µ": Fraction(1, 1000000),
}
_CELSIUS = r"(?:℃|°?\s*C)"
_TEMP = r"([+-]?\d+(?:\.\d+)?)"
_RANGE = re.compile(rf"{_TEMP}\s*{_CELSIUS}?\s*(?:~|～|to|–|-)\s*{_TEMP}\s*{_CELSIUS}", re.I)


def _rating_values(part: Part, key: str, pattern: re.Pattern[str]) -> set[Fraction]:
    matches = pattern.findall(part.description)
    if key in part.specs:
        structured = pattern.fullmatch(part.specs[key])
        if not structured:
            raise ValueError("Unrecognized catalog rating")
        matches.append((structured[1], structured[2]))
    return {Fraction(number) * _FACTORS[prefix] for number, prefix in matches}


def _temperature_ranges(part: Part) -> set[tuple[Fraction, Fraction]]:
    ranges = _RANGE.findall(part.description.replace("−", "-"))
    for key in ("Operating Temperature", "Operating Temperature Range"):
        if key in part.specs:
            match = _RANGE.fullmatch(part.specs[key].replace("−", "-"))
            if not match:
                raise ValueError("Unrecognized operating range")
            ranges.append((match[1], match[2]))
    return {(Fraction(low), Fraction(high)) for low, high in ranges}


def _covers_ratings(source: Part, candidate: Part) -> bool:
    try:
        for key, pattern in _RATINGS.items():
            required = _rating_values(source, key, pattern)
            if not required:
                continue
            offered = _rating_values(candidate, key, pattern)
            if len(required) != 1 or len(offered) != 1:
                return False
            rating, replacement = required.pop(), offered.pop()
            if key == "Tolerance":
                if replacement > rating:
                    return False
            elif replacement < rating:
                return False
        temperatures = _temperature_ranges(source)
        if not temperatures:
            return True
        replacements = _temperature_ranges(candidate)
        if len(temperatures) != 1 or len(replacements) != 1:
            return False
        low, high = temperatures.pop()
        other_low, other_high = replacements.pop()
        return other_low <= low <= high <= other_high
    except ValueError:
        # The catalog published a rating we could not establish.
        return False


def _compatible(source: Part, candidate: Part) -> bool:
    if not _identified(source.package) or candidate.package.casefold() != source.package.casefold():
        return False
    family = _family(source)
    other_family = _family(candidate)
    if family and other_family and family != other_family:
        return False
    same_device = (
        _has_identity(source)
        and source.manufacturer.casefold() == candidate.manufacturer.casefold()
        and source.mfr_part == candidate.mfr_part
    )
    if not same_device and not (
        family
        and family == other_family
        and all(_identified(source.specs.get(key, "")) for key in family)
    ):
        return False
    # For additional catalog attributes with no comparison rule (e.g. a
    # resistor temperature coefficient), only identical evidence suffices.
    for key, value in source.specs.items():
        if key in _RATINGS or key in (
            "Operating Temperature",
            "Operating Temperature Range",
            "Notes",
        ):
            continue
        if candidate.specs.get(key) != value:
            return False
    return _covers_ratings(source, candidate)


def suggest_alternatives(catalog: JlcpartsCatalog, source: Part, limit: int = 5) -> list[Part]:
    """Collect comparable passives by nominal attribute and exact-device candidates by MPN."""
    if not _identified(source.package):
        return []
    candidates = []
    family = _family(source)
    if family and all(_identified(source.specs.get(key, "")) for key in family):
        candidates.extend(
            catalog.search(
                source.specs[family[0]],
                attribute=family[0],
                package=source.package,
                min_stock=1,
                limit=_POOL_LIMIT,
            )
        )
    if _has_identity(source):
        candidates.extend(
            catalog.search(source.mfr_part, package=source.package, min_stock=1, limit=_POOL_LIMIT)
        )
    unique = {candidate.lcsc_part: candidate for candidate in candidates}
    return rank_alternatives(source, list(unique.values()), limit)
