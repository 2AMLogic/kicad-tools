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
from decimal import Decimal
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
        (
            ("pF", Fraction(1, 10**12)),
            ("nF", Fraction(1, 10**9)),
            ("µF", Fraction(1, 10**6)),
            ("uF", Fraction(1, 10**6)),
            ("mF", Fraction(1, 10**3)),
            ("F", Fraction(1)),
        ),
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
        (
            ("mΩ", Fraction(1, 10**3)),
            ("Ω", Fraction(1)),
            ("kΩ", Fraction(10**3)),
            ("MΩ", Fraction(10**6)),
        ),
    ),
}
_DIELECTRIC = re.compile(r"(?<!\w)(C0G|NP0|X5R|X6S|X7R|X7S|X7T|X8R|X8L|Y5V|Z5U)(?!\w)", re.I)


def _kind(part: Part) -> str | None:
    """``"cap"`` or ``"res"`` when the part is recognisably one chip passive."""
    kinds = set()
    if (
        "Multilayer Ceramic Capacitors MLCC" in part.description
        or "Capacitance" in part.specs
        or _NOMINALS["cap"][1].search(part.description)
        and _DIELECTRIC.search(part.description)
    ):
        kinds.add("cap")
    if "Chip Resistor - Surface Mount" in part.description or "Resistance" in part.specs:
        kinds.add("res")
    return kinds.pop() if len(kinds) == 1 else None


def _nominal(part: Part, kind: str) -> Fraction | None:
    """The one nominal value (F or Ω) the description and attribute agree on."""
    key, pattern, factors, _ = _NOMINALS[kind]
    found = {Fraction(n) * factors[prefix] for n, prefix in pattern.findall(part.description)}
    if key in part.specs:
        structured = pattern.fullmatch(part.specs[key].strip())
        if not structured:
            return None
        found.add(Fraction(structured[1]) * factors[structured[2]])
    return found.pop() if len(found) == 1 else None


def _spellings(kind: str, value: Fraction) -> list[str]:
    """Plausible catalog spellings of a nominal, e.g. ``100nF`` and ``0.1µF``."""
    out = []
    for unit, size in _NOMINALS[kind][3]:
        quotient = value / size
        if not Fraction(1, 1000) <= quotient < 10**6:
            continue
        if _prime_factors(quotient.denominator) - {2, 5}:
            continue
        number = Decimal(quotient.numerator) / Decimal(quotient.denominator)
        out.append(f"{format(number.normalize(), 'f')}{unit}")
    return out


def _prime_factors(n: int) -> set[int]:
    factors, p = set(), 2
    while n > 1 and p * p <= n:
        while n % p == 0:
            factors.add(p)
            n //= p
        p += 1
    if n > 1:
        factors.add(n)
    return factors


def _dielectrics(part: Part) -> set[str]:
    found = {code.casefold() for code in _DIELECTRIC.findall(part.description)}
    if "Temperature Coefficient" in part.specs:
        found.add(part.specs["Temperature Coefficient"].strip().casefold())
    return found


def _covers_ratings(source: Part, candidate: Part, mandatory: tuple[str, ...] = ()) -> bool:
    try:
        for key, pattern in _RATINGS.items():
            required = _rating_values(source, key, pattern)
            if not required:
                if key in mandatory:
                    return False
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
    skip = {*_RATINGS, "Operating Temperature", "Operating Temperature Range", "Notes"}
    mandatory: tuple[str, ...] = ()
    if not same_device:
        # Anything else must be the same kind of passive with the same nominal
        # value, read with units, that source and candidate descriptions and
        # attributes agree on.
        kind = _kind(source)
        if kind is None or kind != _kind(candidate):
            return False
        if family:
            if not all(_identified(source.specs.get(key, "")) for key in family):
                return False
        elif source.specs or kind != "cap":
            return False
        else:
            mandatory = ("Voltage Rating",)
        nominal = _nominal(source, kind)
        if nominal is None or nominal != _nominal(candidate, kind):
            return False
        skip.add(_NOMINALS[kind][0])
        if kind == "cap":
            dielectric = _dielectrics(source)
            if len(dielectric) != 1 or dielectric != _dielectrics(candidate):
                return False
            skip.add("Temperature Coefficient")
    # For additional catalog attributes with no comparison rule (e.g. a
    # resistor temperature coefficient), only identical evidence suffices.
    for key, value in source.specs.items():
        if key not in skip and candidate.specs.get(key) != value:
            return False
    return _covers_ratings(source, candidate, mandatory)


def _collect(source: Part, fetch, found: dict[str, Part]) -> None:
    """Page through every row *fetch* yields, keeping the compatible ones."""
    offset = 0
    while True:
        rows = fetch(_PAGE_SIZE, offset)
        for candidate in rows:
            if candidate.stock > 0 and _compatible(source, candidate):
                found[candidate.lcsc_part] = candidate
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
    found: dict[str, Part] = {}
    kind = _kind(source)
    nominal = _nominal(source, kind) if kind else None
    if kind and nominal is not None:
        key = _NOMINALS[kind][0]
        spellings = _spellings(kind, nominal)
        if spellings:
            _collect(
                source,
                lambda size, offset: catalog.search(
                    source.specs.get(key) or spellings[0],
                    attribute=key,
                    values=spellings,
                    package=source.package,
                    min_stock=1,
                    limit=size,
                    offset=offset,
                ),
                found,
            )
    if _has_identity(source):
        _collect(
            source,
            lambda size, offset: catalog.find_device(
                source.mfr_part, source.package, min_stock=1, limit=size, offset=offset
            ),
            found,
        )
    return rank_alternatives(source, list(found.values()), limit)
