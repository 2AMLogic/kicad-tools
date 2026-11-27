"""Archived ``design-source.zip`` recipe sources must match the live recipe (Issue #9231).

Boards 02, 05, 06 and 07 ship their archived recipe sources as
``boards/<board>/output/manufacturing/design-source.zip`` (built by
``scripts/boards/_bundle_links.py`` / ``scripts/boards/refresh_readiness.py``).
The archive is what a fab or an outside reviewer reads to see how the board was
produced, so a stale member misrepresents the recipe that made the committed
artifacts -- and nothing compared the members to the live tree, so drift was
invisible until someone unzipped.

Only ``.py`` members are compared: ``.md`` members are link-rewritten and
generated outputs (BOM, netlists) are not recipe sources.  Members are read
with :mod:`zipfile`; nothing is extracted to disk.

The loose ``design-source/*.py`` copies of boards 03/04 are a separate
archive form with their own escape hatch; keep the two independent.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARDS_DIR = REPO_ROOT / "boards"
REFRESH_SCRIPT = REPO_ROOT / "scripts" / "boards" / "refresh_readiness.py"

# Archived members deliberately left behind the live recipe, keyed by
# "<board-dir>/<member path inside the zip>".  Each entry cites the issue that
# owns the refresh; remove the entry when that lands (strict xfail turns an
# unexpected pass into a failure).
EXPECTED_STALE: dict[str, str] = {
    "02-charlieplex-led/generate_design.py": (
        "Issue #9800: the live recipe uses the shared silk pass. The registered "
        "producer keeps the previous design-source.zip until the five via-only "
        "warnings (#9112/#9033) and the changed artwork's render binding pass."
    ),
    "02-charlieplex-led/package_release.py": (
        "Issue #9800: the live publisher gained the changed-artwork render "
        "binding (renders ship only with kct render's render-provenance.json "
        "tied to the checked PCB). The registered producer keeps the previous "
        "design-source.zip until the five via-only warnings (#9112/#9033) "
        "clear and the package can be re-issued."
    ),
}

# Until the Board 05 reissue (#9921), accept only these published/live pairs.
# Remove the pins with the reissue. Updated archive members use ordinary parity.
BOARD05_PENDING_REISSUE = {
    "05-bldc-motor-controller/redesign/build.py": (
        "76f8818fc218d1fd6282230f58e936eecd17a1d71fe67b32590f9f21665b02cc",
        "88c2331bf50d421a6c09abb2a0e1ee334fb4e87fa5474cf2f800997a866197c0",
    ),
    "05-bldc-motor-controller/redesign/hardware.py": (
        "ffb9ef695e98be35a2794e3836af783b4c21a43ae40f55b3cc5434752dc12cd1",
        "ea8c6c6b03815433f3225532ee73a076e1ff4e277647bda215d9de2c2abbc9c6",
    ),
    "05-bldc-motor-controller/redesign/package.py": (
        "3691b3992c2d6687e668d0bd08947d41cb987b3b608253aa5192fe67a0df6d79",
        "13aca58cb6e23656f4c49d2536f08f48bdbc08dac875299df1e3a2d44f6936d0",
    ),
    "05-bldc-motor-controller/redesign/routing.py": (
        "7bd3033cbe6268efee57e3e7c2cc06ffd46d7c655c7cbec6c65675d4b142c4df",
        "f3ee1f358f528eb55fb1f64011cd6f364124614d8bec1670f7e0d5e4c5882f27",
    ),
}


def _zip_members() -> list[tuple[Path, str]]:
    """Every ``.py`` member of every board ``design-source.zip``, sorted."""
    members: list[tuple[Path, str]] = []
    for archive in sorted(BOARDS_DIR.glob("*/output/manufacturing/design-source.zip")):
        with zipfile.ZipFile(archive) as zf:
            members.extend(
                (archive, info.filename)
                for info in zf.infolist()
                if not info.is_dir() and info.filename.endswith(".py")
            )
    return members


ZIP_MEMBERS = _zip_members()


def board_dir(archive: Path) -> Path:
    """Return the board directory owning ``<board>/output/manufacturing/design-source.zip``."""
    return archive.parents[2]


def member_key(param: tuple[Path, str]) -> str:
    """Return the ``<board-dir>/<member path>`` identity of an archived member."""
    archive, member = param
    return f"{board_dir(archive).name}/{member}"


def refresh_guidance(board: str) -> str:
    """Say which refresh path exists for ``board``, rather than blanket advice."""
    if f'"{board}": BoardConfig(' in REFRESH_SCRIPT.read_text(encoding="utf-8"):
        return (
            f"refresh it through scripts/boards/refresh_readiness.py "
            f"(it has a BOARDS entry for {board}) so the manifest hashes stay "
            "consistent"
        )
    return (
        f"scripts/boards/refresh_readiness.py has no BOARDS entry for {board}, "
        "so regenerate the archive with the board's own recipe packaging step"
    )


@pytest.mark.parametrize("zip_member", ZIP_MEMBERS, ids=member_key)
def test_design_source_zip_member_matches_live_recipe(
    zip_member: tuple[Path, str], request
) -> None:
    archive, member = zip_member
    key = member_key(zip_member)
    board = board_dir(archive)
    live = board / member
    assert live.is_file(), (
        f"{key}: archived design-source.zip member has no live counterpart "
        f"({live}); the archive is the only remaining copy, so parity cannot "
        "hold. Restore the live recipe or regenerate the archive without it."
    )
    if key in EXPECTED_STALE:
        request.applymarker(pytest.mark.xfail(reason=EXPECTED_STALE[key], strict=True))
    with zipfile.ZipFile(archive) as zf:
        archived = zf.read(member)
    # Compare digests, not raw bytes, so a mismatch never renders a huge
    # sequence diff that trips pytest-timeout and hides this message (Issue #9481).
    archived_digest = hashlib.sha256(archived).hexdigest()
    live_digest = hashlib.sha256(live.read_bytes()).hexdigest()
    pending = BOARD05_PENDING_REISSUE.get(key)
    if pending and archived_digest == pending[0]:
        assert live_digest == pending[1], f"{key}: live recipe differs from the #9921 pin"
        return
    assert archived_digest == live_digest, (
        f"{key}: archived design-source.zip member drifted from the live "
        f"recipe ({live}; sha256 archive={archived_digest[:12]} "
        f"live={live_digest[:12]}); {refresh_guidance(board.name)}, or record it in "
        "EXPECTED_STALE citing the owning issue."
    )


def test_every_zip_board_is_collected() -> None:
    """No board with a ``design-source.zip`` may be silently skipped by the glob."""
    boards_with_zip = {
        p.parent.parent.parent.name
        for p in BOARDS_DIR.glob("*/output/manufacturing/design-source.zip")
    }
    assert boards_with_zip, "no boards/*/output/manufacturing/design-source.zip found"
    collected = {board_dir(archive).name for archive, _ in ZIP_MEMBERS}
    missing = boards_with_zip - collected
    assert not missing, (
        f"design-source.zip present but no .py members collected for {sorted(missing)}; "
        "the parity check is comparing nothing for them."
    )


def test_expected_stale_entries_name_real_members() -> None:
    """A renamed or removed member must not leave a dead escape-hatch entry."""
    keys = {member_key(m) for m in ZIP_MEMBERS}
    dead = sorted((EXPECTED_STALE.keys() | BOARD05_PENDING_REISSUE.keys()) - keys)
    assert not dead, f"Recipe parity entries match no archived .py member: {dead}"
