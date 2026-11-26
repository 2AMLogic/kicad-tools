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


def _nominal(part: Part) -> str | None:
    for family, required in _PASSIVE_FAMILIES:
        if family in part.description and all(part.specs.get(key) for key in required):
            return part.specs[required[0]]
    return None


def _same_device(source: Part, candidate: Part) -> bool:
    return bool(
        source.manufacturer
        and source.mfr_part
        and candidate.manufacturer.casefold() == source.manufacturer.casefold()
        and candidate.mfr_part == source.mfr_part
    )


def suggest_alternatives(catalog: JlcpartsCatalog, source: Part, limit: int = 5) -> list[Part]:
    """Rank only candidates with matching identity or comparable passive specifications."""
    nominal = _nominal(source)
    if not source.package or not (nominal or (source.manufacturer and source.mfr_part)):
        return []
    candidates = catalog.search(
        nominal or source.mfr_part, package=source.package, min_stock=1, limit=_POOL_LIMIT
    )
    verified = [
        candidate
        for candidate in candidates
        if _same_device(source, candidate)
        or (
            nominal
            and _nominal(candidate) == nominal
            and source.specs.items() <= candidate.specs.items()
        )
    ]
    return rank_alternatives(source, verified, limit)
