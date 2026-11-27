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
from collections.abc import Iterable, Iterator
from fractions import Fraction

from .jlcparts_catalog import JlcpartsCatalog
from .models import Part

_TIER_BASIC = 0
_TIER_PREFERRED = 1
_TIER_EXTENDED = 2

_TIER_NAMES = {_TIER_BASIC: "basic", _TIER_PREFERRED: "preferred", _TIER_EXTENDED: "extended"}

# Catalog rows read per query page. Every matching row is examined, so the page
# size bounds memory, never which substitutes are found.
_PAGE_SIZE = 500


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


def rank_alternatives(source: Part, candidates: Iterable[Part], limit: int = 5) -> list[Part]:
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
_CAP_FAMILY = "multilayer ceramic capacitors mlcc"
_RES_FAMILY = "chip resistor - surface mount"


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
    "Voltage Rating": _quantity(r"V(?:\s*(?i:DC))?"),
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
_RANGE = re.compile(
    rf"{_TEMP}\s*{_CELSIUS}?\s*(?:~|～|to|–|-|\.\.\.?|…)\s*{_TEMP}\s*{_CELSIUS}", re.I
)
_CELSIUS_MENTION = re.compile(r"\d\s*(?:℃|°\s*C)", re.I)


def _rating_values(part: Part, key: str, pattern: re.Pattern[str]) -> set[Fraction]:
    matches = pattern.findall(part.description)
    if key in part.specs:
        structured = pattern.fullmatch(part.specs[key])
        if not structured:
            raise ValueError("Unrecognized catalog rating")
        matches.append((structured[1], structured[2]))
    return {Fraction(number) * _FACTORS[prefix] for number, prefix in matches}


def _temperature_ranges(part: Part) -> set[tuple[Fraction, Fraction]]:
    text = part.description.replace("−", "-")
    ranges = _RANGE.findall(text)
    if not ranges and _CELSIUS_MENTION.search(text):
        # A temperature is stated in a notation we cannot read as a range.
        raise ValueError("Unrecognized operating range")
    for key in ("Operating Temperature", "Operating Temperature Range"):
        if key in part.specs:
            match = _RANGE.fullmatch(part.specs[key].replace("−", "-"))
            if not match:
                raise ValueError("Unrecognized operating range")
            ranges.append((match[1], match[2]))
    return {(Fraction(low), Fraction(high)) for low, high in ranges}


_DEC = r"(?:\d+(?:\.\d+)?|\.\d+)"
_NOMINALS = {
    "cap": (
        "Capacitance",
        re.compile(rf"(?<![\w./+-])({_DEC})\s*([pnuµμm]?)[Ff](?!\w)"),
        {
            "": Fraction(1),
            "m": Fraction(1, 10**3),
            "u": Fraction(1, 10**6),
            "µ": Fraction(1, 10**6),
            "μ": Fraction(1, 10**6),
            "n": Fraction(1, 10**9),
            "p": Fraction(1, 10**12),
        },
    ),
    "res": (
        "Resistance",
        re.compile(rf"(?<![\w./+-])({_DEC})\s*([mkKM]?)\s*(?:Ω|\u2126|[Oo]hms?)(?!\w)"),
        {
            "": Fraction(1),
            "m": Fraction(1, 10**3),
            "k": Fraction(10**3),
            "K": Fraction(10**3),
            "M": Fraction(10**6),
        },
    ),
}
_FARADS = rf"({_DEC})\s*([pnuµμm]?)[Ff](?!\w)"
_ABSOLUTE_CAP_TOL = re.compile(rf"(?:±|\+/-)\s*{_FARADS}")
_STRUCTURED_CAP_TOL = re.compile(rf"(?:(?:±|\+/-)\s*)?{_FARADS}")
_SAFETY_CLASS = re.compile(r"\b([XY][1-4])\b", re.I)
_DIELECTRIC = re.compile(r"(?<!\w)(C0G|NP0|X5R|X6S|X7R|X7S|X7T|X8R|X8L|Y5V|Z5U)(?!\w)", re.I)


_OTHER_FAMILY = re.compile(
    r"\b(?:networks?|arrays?|filters?|feed[- ]?through|(?:three|3)[- ]terminal)\b", re.I
)


def _kind(part: Part) -> str | None:
    """``"cap"`` or ``"res"`` when the part is recognisably one chip passive."""
    if _OTHER_FAMILY.search(part.description) or _OTHER_FAMILY.search(part.specs.get("Type", "")):
        return None
    description = part.description.casefold()
    if _RES_FAMILY in description:
        return "res"
    if _CAP_FAMILY in description:
        return "cap"
    # Legacy rows can consist solely of capacitor ratings and the package.
    if _DIELECTRIC.search(part.description) and _NOMINALS["cap"][1].search(part.description):
        remainder = part.description
        for pattern in (
            _RANGE,
            _ABSOLUTE_CAP_TOL,
            _NOMINALS["cap"][1],
            _DIELECTRIC,
            *_RATINGS.values(),
        ):
            remainder = pattern.sub(" ", remainder)
        if remainder.strip().casefold() in ("", part.package.casefold()):
            return "cap"
    return None


def _nominals(part: Part, kind: str) -> set[Fraction]:
    key, pattern, factors = _NOMINALS[kind]
    description = (
        _ABSOLUTE_CAP_TOL.sub(" ", part.description) if kind == "cap" else part.description
    )
    found = {Fraction(n) * factors[prefix] for n, prefix in pattern.findall(description)}
    if key in part.specs:
        structured = pattern.fullmatch(part.specs[key].strip())
        if structured:
            found.add(Fraction(structured[1]) * factors[structured[2]])
    return found


def _dielectrics(part: Part) -> set[str]:
    found = {code.casefold() for code in _DIELECTRIC.findall(part.description)}
    if "Temperature Coefficient" in part.specs:
        found.add(part.specs["Temperature Coefficient"].strip().casefold())
    return found


_PPM = _quantity(r"ppm\s*/\s*(?:°\s*C|℃|K)")


def _covers_coefficient(source: Part, candidate: Part) -> bool:
    try:
        required = _rating_values(source, "Temperature Coefficient", _PPM)
        if not required:
            return True
        offered = _rating_values(candidate, "Temperature Coefficient", _PPM)
        return len(required) == len(offered) == 1 and offered.pop() <= required.pop()
    except ValueError:
        return False


def _cap_tolerances(part: Part) -> set[Fraction]:
    """Return each published tolerance as a percentage of nominal capacitance."""
    percent = _RATINGS["Tolerance"]
    values = {Fraction(n) * _FACTORS[p] for n, p in percent.findall(part.description)}
    absolute = _ABSOLUTE_CAP_TOL.findall(part.description)
    if "Tolerance" in part.specs:
        value = part.specs["Tolerance"].strip()
        if match := percent.fullmatch(value):
            values.add(Fraction(match[1]) * _FACTORS[match[2]])
        elif match := _STRUCTURED_CAP_TOL.fullmatch(value):
            absolute.append((match[1], match[2]))
        else:
            raise ValueError("Unrecognized capacitance tolerance")
    if absolute:
        nominal = _nominals(part, "cap")
        if len(nominal) != 1 or 0 in nominal:
            raise ValueError("Absolute tolerance needs a known positive capacitance")
        scale = 100 / nominal.pop()
        values.update(Fraction(n) * _NOMINALS["cap"][2][p] * scale for n, p in absolute)
    return values


def _safety_compatible(source: Part, candidate: Part, same_device: bool) -> bool:
    required = set(_SAFETY_CLASS.findall(source.description.upper()))
    offered = set(_SAFETY_CLASS.findall(candidate.description.upper()))
    if required:
        return required <= offered or (same_device and not offered)
    return same_device or not re.search(r"\bsafety\b", source.description, re.I)


# Preserve input/output roles alongside the voltages in exact-device descriptions.
_DEVICE_VOLTAGE = re.compile(_RATINGS["Voltage Rating"].pattern + r"|\b(?i:input|output)\b")


def _device_voltages(part: Part) -> list[str | Fraction]:
    evidence: list[str | Fraction] = []
    for match in _DEVICE_VOLTAGE.finditer(part.description):
        if match[1]:
            evidence.append(Fraction(match[1]) * _FACTORS[match[2]])
        else:
            evidence.append(match[0].casefold())
    return evidence if any(isinstance(item, Fraction) for item in evidence) else []


def _covers_ratings(
    source: Part, candidate: Part, mandatory: tuple[str, ...], passive: str | None
) -> bool:
    try:
        for key, pattern in _RATINGS.items():
            if passive is None and key == "Voltage Rating":
                left, right = _device_voltages(source), _device_voltages(candidate)
                if left and right and left != right:
                    return False
                continue
            cap_tolerance = passive == "cap" and key == "Tolerance"
            required = (
                _cap_tolerances(source) if cap_tolerance else _rating_values(source, key, pattern)
            )
            if not required:
                if key in mandatory:
                    return False
                continue
            offered = (
                _cap_tolerances(candidate)
                if cap_tolerance
                else _rating_values(candidate, key, pattern)
            )
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
    same_device = (
        _has_identity(source)
        and source.manufacturer.casefold() == candidate.manufacturer.casefold()
        and source.mfr_part.casefold() == candidate.mfr_part.casefold()
    )
    skip = {*_RATINGS, "Operating Temperature", "Operating Temperature Range", "Notes"}
    mandatory: tuple[str, ...] = ()
    kind = _kind(source)
    other_kind = _kind(candidate)
    if kind and other_kind and kind != other_kind:
        return False
    if not same_device and (kind is None or kind != other_kind):
        return False
    passive = kind or other_kind
    if passive and (kind is None or other_kind is None):
        unclassified = candidate if kind else source
        if _OTHER_FAMILY.search(unclassified.description) or _OTHER_FAMILY.search(
            unclassified.specs.get("Type", "")
        ):
            return False
    if same_device and passive is None:
        skip.discard("Voltage Rating")
        # Incomplete descriptions still carry evidence against an identity match.
        for possible in _NOMINALS:
            values = _nominals(source, possible) | _nominals(candidate, possible)
            if len(values) > 1:
                return False
            if (
                possible == "cap"
                and values
                and len(_dielectrics(source) | _dielectrics(candidate)) > 1
            ):
                return False
    if passive:
        key, pattern, _ = _NOMINALS[passive]
        required = _nominals(source, passive)
        offered = _nominals(candidate, passive)
        if len(required | offered) > 1:
            return False
        if not same_device and (not required or not offered):
            return False
        for part in (source, candidate):
            if key in part.specs and not pattern.fullmatch(part.specs[key].strip()):
                if not same_device or source.specs.get(key) != candidate.specs.get(key):
                    return False
        skip.add(key)
        if passive == "cap":
            if not _safety_compatible(source, candidate, same_device):
                return False
            dielectric = _dielectrics(source)
            other_dielectric = _dielectrics(candidate)
            if len(dielectric | other_dielectric) > 1:
                return False
            if not same_device:
                if not dielectric or not other_dielectric:
                    return False
                mandatory = ("Voltage Rating",)
                if _CAP_FAMILY in source.description.casefold():
                    mandatory += ("Tolerance",)
            skip.add("Temperature Coefficient")
        else:
            if not same_device:
                if not _identified(source.specs.get("Type", "")):
                    return False
                mandatory = ("Power(Watts)", "Tolerance")
            if not _covers_coefficient(source, candidate):
                return False
            skip.add("Temperature Coefficient")
    for key, value in source.specs.items():
        if key not in skip and candidate.specs.get(key) != value:
            return False
    return _covers_ratings(source, candidate, mandatory, passive)


def _device_rows(catalog: JlcpartsCatalog, source: Part) -> Iterator[Part]:
    offset = 0
    while True:
        rows = catalog.find_device(
            source.mfr_part, source.package, min_stock=1, limit=_PAGE_SIZE, offset=offset
        )
        yield from rows
        if len(rows) < _PAGE_SIZE:
            return
        offset += _PAGE_SIZE


def suggest_alternatives(catalog: JlcpartsCatalog, source: Part, limit: int = 5) -> list[Part]:
    """Find substitutes with the same nominal value, or the same manufacturer part.

    Every catalog row matching either query is examined before ranking, so
    incompatible rows can never displace an eligible substitute.
    """
    if not _identified(source.package):
        return []
    kind = _kind(source)
    if kind and len(_nominals(source, kind)) == 1:
        # Use the ranking parser for every spelling, including zero ohms.
        return rank_alternatives(source, catalog.in_stock_package(source.package), limit)
    if _has_identity(source):
        return rank_alternatives(source, _device_rows(catalog, source), limit)
    return []
