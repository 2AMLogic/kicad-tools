"""Buttress-wire list sidecar for ``kct pcb reinforce`` (Issue #8744).

Slice 3 of #8741. Each non-dry-run ``reinforce`` pass in ``ends`` mode
records the wires the board owner must cut and bend in
``<board>.reinforcement.json`` next to the board, one entry per leg
(:class:`kicad_tools.pcb.reinforce.LegSummary`). Re-running replaces its own
wires, keeps other nets' entries and never duplicates; output is sorted and
free of timestamps so it diffs cleanly.

Coordinates are sheet-absolute (like refusals, Issue #8493).
"""

from __future__ import annotations

import json
import math
import os
import shutil
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from kicad_tools.pcb.reinforce import LEG_COPY, STRATEGY_PARALLEL, STRATEGY_WIRE
from kicad_tools.physics.wire_gauge import bare_copper_diameter_mm

SIDECAR_VERSION = 1
SIDECAR_SUFFIX = ".reinforcement.json"


def sidecar_path(board: Path) -> Path:
    """Find the board's wire record, including boards reached through a symlink."""
    if board.is_symlink():
        named = board.with_name(board.name + SIDECAR_SUFFIX)
        if os.path.lexists(named):
            return named  # Keep a record explicitly paired with the alias.
        board = board.resolve()
    return board.with_name(board.name + SIDECAR_SUFFIX)


def board_lock_path(path: Path) -> Path:
    target = path.resolve()
    return target.with_name(f".{target.name}.center.lock")


def _file_version(path: Path) -> tuple[int, int, int, int, int] | None:
    """Detect replacement, creation, removal or an in-place edit during a read."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def board_stream_lock(fd: int, board: Path, *, writing: bool) -> Iterator[None]:
    """Coordinate ordinary in-place text writes without creating a sibling in a read-only directory."""
    from kicad_tools.core.package_publication import publication_lock

    try:
        import fcntl
    except ImportError:  # Windows uses the same sibling reservation as PCB.save.
        if writing:
            with publication_lock(board_lock_path(board)):
                yield
        else:
            yield
        return

    mode = fcntl.LOCK_EX if writing else fcntl.LOCK_SH
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise ValueError(f"Another operation is publishing the input: {board}") from exc
    try:
        if writing and board_lock_path(board).exists():
            raise ValueError(f"Another operation is publishing the output: {board}")
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def board_versions(board: Path):
    record = sidecar_path(board)
    return board.resolve(), record.resolve(), _file_version(board), _file_version(record)


@contextmanager
def reinforce_publication(source: Path, output: Path) -> Iterator[Callable[[], None]]:
    """Reserve the output before reading geometry. Check a distinct input again before saving."""
    from kicad_tools.core.package_publication import publication_lock

    versions = board_versions(source)
    # A save can replace the output symlink itself. Reserve its name as well
    # as the physical board and either possible inventory.
    locks = {
        output.parent.resolve() / f".{output.name}.center.lock",
        board_lock_path(output),
        board_lock_path(sidecar_path(output)),
        board_lock_path(saved_record_path(output, True)),
    }
    source_locks = {board_lock_path(source), board_lock_path(sidecar_path(source))}

    def verify() -> None:
        if versions != board_versions(source) or any(
            lock.exists() for lock in source_locks - locks
        ):
            raise ValueError(f"PCB or reinforcement record changed while reinforcing: {source}")

    with ExitStack() as stack:
        for lock in sorted(locks):
            stack.enter_context(publication_lock(lock))
        # On Windows the sibling reservation also guards recordless moves.
        # Keeping an open board handle there would block PCB.save's rename.
        if os.name != "nt":
            for path in {source.resolve(), output.resolve()}:
                if path.is_file():
                    stream = stack.enter_context(path.open("rb"))
                    stack.enter_context(board_stream_lock(stream.fileno(), path, writing=False))
        verify()
        yield verify


def edit_seed_record(input_board: Path, output_board: Path) -> Path:
    """The inventory the PCB being saved to ``output_board`` starts from.

    Through a board symlink the unsaved name resolves to the alias target's
    record. That is the saved PCB's inventory only when the alias is the board
    being edited. For a distinct input the saved PCB is the edited input, so it
    starts from the input's own inventory, never the alias target's or a record
    that happens to sit beside the alias name.
    """
    if output_board.is_symlink() and not _same_board(input_board, output_board):
        return sidecar_path(input_board)
    return sidecar_path(output_board)


def alias_export_conflict(
    input_board: Path, output_board: Path, will_save: bool, writes_records: bool
) -> str | None:
    """Why an export through a board symlink must be refused, or ``None``.

    Checked before anything is written. ``output_board`` being a symlink to a
    board other than ``input_board`` means the wire list could land in the
    other board's record (Issue #10250):

    * nothing is saved but wire records would be written: they would describe
      a PCB that is not written and pair with the alias target;
    * a PCB is saved and a record already sits beside the alias name with a
      different inventory than the edited input's: the saved PCB cannot have
      both.
    """
    if not output_board.is_symlink() or _same_board(input_board, output_board):
        return None
    if not will_save:
        if writes_records:
            return (
                f"{output_board} is a symlink to a different board and nothing would be "
                "saved, so its wire records cannot be paired with a PCB; refusing before "
                "writing. Point -o at a plain path or at the input board."
            )
        return None
    beside = output_board.with_name(output_board.name + SIDECAR_SUFFIX)
    if not os.path.lexists(beside):
        return None
    seed = sidecar_path(input_board)
    try:
        if beside.resolve() == seed.resolve():
            return None
        if seed.is_file() and beside.is_file() and beside.read_bytes() == seed.read_bytes():
            return None
    except OSError:
        pass
    return (
        f"{beside} already holds a different wire list than the input board's; saving "
        f"through the symlink {output_board} would pair the wrong inventory with the "
        "saved PCB. Move or remove it, or point -o at a plain path."
    )


def _same_board(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def saved_record_path(board: Path, will_save: bool) -> Path:
    """The record that pairs with ``board`` once a save has replaced it.

    A save through a board symlink replaces the alias with its own file, so
    the wire list must sit beside that file, not beside the unchanged target.
    """
    if will_save and board.is_symlink():
        return board.with_name(board.name + SIDECAR_SUFFIX)
    return sidecar_path(board)


def _restrict_to_creation_mode(path: Path, wanted: int) -> None:
    """Give a just-created ``path`` the permissions of ``wanted`` it may keep.

    ``path`` was created under the process umask; never widen that, so a
    private umask still yields a private record.
    """
    path.chmod(wanted & (path.stat().st_mode & 0o7777))


def carry_record(seed: Path, dest: Path, detach: bool = False) -> None:
    """Give ``dest`` the inventory ``seed`` held.

    When they are the same file this is a no-op, except with ``detach`` (the
    board was a symlink that a save has replaced) and ``dest`` is itself a
    symlink into the original board's inventory: it is then replaced by an
    independent copy so edits cannot reach the original board's wire list.
    A new file keeps the seed's permissions only as far as the creation mask
    allows.
    """
    if dest == seed:
        if detach and dest.is_symlink() and dest.is_file():
            data = dest.read_bytes()
            mode = dest.stat().st_mode & 0o7777
            dest.unlink()
            dest.write_bytes(data)
            _restrict_to_creation_mode(dest, mode)
        return
    if not seed.is_file():
        return
    if dest.is_symlink():
        dest.unlink()
    elif dest.exists():
        return  # an inventory already paired with the saved board wins
    mode = seed.stat().st_mode & 0o7777
    dest.write_bytes(seed.read_bytes())
    _restrict_to_creation_mode(dest, mode)


@dataclass(frozen=True)
class PreparedRecordUpdate:
    """In-memory reinforcement sidecar update ready to stage and publish."""

    dest: Path
    content: bytes | None
    seed_mode: int | None = None
    replace_symlink: bool = False
    dropped: int = 0


def _parse_wires(raw: bytes, path: Path) -> list[dict[str, Any]]:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("version") != SIDECAR_VERSION:
        raise ValueError(f"{path}: not a version {SIDECAR_VERSION} reinforcement sidecar")
    wires = data.get("wires")
    if not isinstance(wires, list):
        raise ValueError(f"{path}: missing 'wires' list")
    check_wire_records(path, wires)
    return wires


def prepare_record_update(
    seed: Path,
    dest: Path,
    *,
    carry: bool = False,
    detach: bool = False,
    drop: tuple[str, list[tuple[float, float]], list[tuple[Any, ...]]] | None = None,
    new_wires: list[dict[str, Any]] | None = None,
) -> PreparedRecordUpdate:
    """Compute the final sidecar bytes in memory without touching disk.

    Combines carrying/detaching ``seed`` into ``dest``, dropping cleared wire
    records, and merging ``new_wires`` so validation errors fail before any
    PCB or sidecar file changes.
    """
    source_path: Path | None = None
    source_bytes: bytes | None = None
    seed_mode: int | None = None
    replace_symlink = False
    dirty = False

    if carry:
        if dest == seed:
            if detach and dest.is_symlink():
                replace_symlink = True
                if dest.is_file():
                    source_path = dest
                    source_bytes = dest.read_bytes()
                    seed_mode = dest.stat().st_mode & 0o7777
                    dirty = True
            elif dest.is_file():
                source_path = dest
        elif seed.is_file():
            if dest.is_symlink():
                source_path = seed
                source_bytes = seed.read_bytes()
                seed_mode = seed.stat().st_mode & 0o7777
                replace_symlink = True
                dirty = True
            elif dest.exists():
                if dest.is_file():
                    source_path = dest
            else:
                source_path = seed
                source_bytes = seed.read_bytes()
                seed_mode = seed.stat().st_mode & 0o7777
                dirty = True
        elif detach and dest.is_symlink():
            replace_symlink = True
        elif dest.is_file():
            source_path = dest
    elif dest.is_file():
        source_path = dest

    current_wires: list[dict[str, Any]] | None = None
    content: bytes | None = None
    dropped = 0

    if drop is not None:
        net, removed, removed_copies = drop
        if (removed or removed_copies) and source_path is not None:
            raw = source_bytes if source_bytes is not None else source_path.read_bytes()
            wires = _parse_wires(raw, source_path)
            gone = wires_with_anchors(wires, net, removed)
            keys = set(removed_copies)
            gone += [w for w in wires_for_net(wires, net) if is_parallel(w) and wire_key(w) in keys]
            dropped = len(gone)
            if dropped:
                gone_ids = {id(w) for w in gone}
                current_wires = sort_wires([w for w in wires if id(w) not in gone_ids])
                content = render_sidecar(current_wires).encode("utf-8")
                dirty = True
            else:
                current_wires = wires

    if new_wires:
        if current_wires is None:
            if source_path is not None:
                raw = source_bytes if source_bytes is not None else source_path.read_bytes()
                current_wires = _parse_wires(raw, source_path)
            else:
                current_wires = []
        merged = merge_wires(current_wires, new_wires)
        content = render_sidecar(merged).encode("utf-8")
        dirty = True

    if dirty and content is None:
        content = source_bytes

    return PreparedRecordUpdate(
        dest=dest,
        content=content,
        seed_mode=seed_mode,
        replace_symlink=replace_symlink,
        dropped=dropped,
    )


@contextmanager
def stage_record_update(
    plan: PreparedRecordUpdate,
    *,
    pcb_output: Path | None = None,
) -> Iterator[Callable[[], None]]:
    """Stage ``plan`` into a sibling file and publish it atomically on commit.

    No target sidecar is created, modified, or unlinked before the returned
    ``commit`` callback runs, so a failed PCB save leaves the prior PCB,
    symlink, and wire inventory intact. If ``commit`` itself fails after a
    PCB save to ``pcb_output``, ``pcb_output`` is restored to its pre-save
    entry (regular file, symlink, or absent).
    """
    if plan.content is None:
        yield lambda: None
        return

    write_target = (
        plan.dest if (plan.replace_symlink or not plan.dest.is_symlink()) else plan.dest.resolve()
    )
    if write_target.is_dir() and not plan.replace_symlink:
        raise IsADirectoryError(f"{write_target} is a directory")
    replacing_regular = (
        not plan.replace_symlink and write_target.exists() and not write_target.is_symlink()
    )
    if replacing_regular:
        with write_target.open("ab"):
            pass

    with ExitStack() as stack:
        tmpdir = Path(
            stack.enter_context(TemporaryDirectory(prefix=".reinforce-", dir=write_target.parent))
        )
        candidate = tmpdir / "record"
        with candidate.open("wb") as stream:
            stream.write(plan.content)
            stream.flush()
            os.fsync(stream.fileno())
        if replacing_regular:
            shutil.copymode(write_target, candidate)
        elif plan.seed_mode is not None:
            _restrict_to_creation_mode(candidate, plan.seed_mode)

        pcb_backup: Path | None = None
        if pcb_output is not None and os.path.lexists(pcb_output):
            if pcb_output.parent.resolve() == write_target.parent.resolve():
                backup_dir = tmpdir
            else:
                backup_dir = Path(
                    stack.enter_context(
                        TemporaryDirectory(prefix=".reinforce-pcb-", dir=pcb_output.parent)
                    )
                )
            pcb_backup = backup_dir / "original_pcb"
            shutil.copy2(pcb_output, pcb_backup, follow_symlinks=False)

        def commit() -> None:
            try:
                os.replace(candidate, write_target)
            except BaseException:
                if pcb_output is not None:
                    if pcb_backup is not None and os.path.lexists(pcb_backup):
                        os.replace(pcb_backup, pcb_output)
                    else:
                        pcb_output.unlink(missing_ok=True)
                raise

        yield commit


def _pt(x: float, y: float) -> dict[str, float]:
    return {"x": round(x, 4), "y": round(y, 4)}


def build_wire_records(outcome: Any, origin_x: float, origin_y: float) -> list[dict[str, Any]]:
    """Wire records from a ``ReinforceResult``'s legs (empty in spaced mode).

    Legs the minimum-wire-length filter skipped are omitted entirely (Issue
    #8915): the owner does not want a wire there, so it is not on the list.
    A leg reinforced by a parallel copy instead (Issue #8916) is recorded
    whatever its length, with ``"strategy": "parallel"`` and the copy's
    copper, so ``--clear`` / ``--replace`` can remove it and
    ``reinforcement_incomplete`` stops asking it for a wire. The leg that IS
    such a copy (status ``copy``) gets no record of its own.
    """
    records: list[dict[str, Any]] = []
    bare = round(bare_copper_diameter_mm(outcome.wire_gauge_awg), 4)
    for leg in outcome.legs:
        if not leg.polyline:
            continue
        parallel = getattr(leg, "strategy", STRATEGY_WIRE) == STRATEGY_PARALLEL
        if parallel and leg.status == LEG_COPY:
            continue
        # A leg below the minimum buttress-wire length gets no wire at all
        # (Issue #8915), so it gets no record either -- the sidecar is the
        # owner's cut-and-bend list. Every other unanchored leg still earns a
        # record: it names copper that WANTS a wire and says why it has none.
        # (The record would also be unreachable for ``drop_wires``, which
        # matches on anchors, and this one has none.) A short leg that got
        # parallel copper instead (#8916) IS reinforced, so it is recorded.
        copy = getattr(leg, "copy", None)
        if getattr(leg, "below_min_wire_length", False) and copy is None:
            continue
        sx, sy = leg.polyline[0]
        ex, ey = leg.polyline[-1]
        start = _pt(sx + origin_x, sy + origin_y)
        end = _pt(ex + origin_x, ey + origin_y)
        anchors = [_pt(a.x + origin_x, a.y + origin_y) for a in leg.anchors]
        straight = None
        if len(anchors) == 2:
            straight = round(
                math.hypot(anchors[1]["x"] - anchors[0]["x"], anchors[1]["y"] - anchors[0]["y"]),
                4,
            )
        records.append(
            {
                "net": leg.net,
                "layer": leg.layer,
                "kind": leg.kind,
                "path": leg.paths[0] if leg.paths else None,
                "paths": sorted(leg.paths),
                "leg_start": start,
                "leg_end": end,
                "anchors": anchors,
                "length_mm": round(leg.length_mm, 4),
                "straight_line_mm": straight,
                "wire_gauge_awg": outcome.wire_gauge_awg,
                # Additive (#8803): the --max-end-shift used; None = default.
                "max_end_shift_mm": getattr(outcome, "max_end_shift_mm", None),
                "bare_diameter_mm": bare,
                # Filled by #8743 (solder-mask opening along the run).
                "mask_opening": None,
                "status": leg.status,
                "reason": leg.reason,
            }
        )
        if parallel:
            records[-1].update(_parallel_fields(leg, copy, origin_x, origin_y))
    return sort_wires(records)


def _parallel_fields(leg: Any, copy: Any, origin_x: float, origin_y: float) -> dict[str, Any]:
    """The extra keys of a parallel-copy record (Issue #8916).

    Absent ``strategy`` means ``wire``, so pre-#8916 sidecars read unchanged
    and the version stays :data:`SIDECAR_VERSION`.
    """
    fields: dict[str, Any] = {
        "strategy": STRATEGY_PARALLEL,
        "copy_status": leg.copy_status,
        "copy_layer": copy.layer if copy is not None else None,
        "polyline": [_pt(x + origin_x, y + origin_y) for x, y in leg.polyline],
        "copy_segments": [],
        "stitch_vias": [],
        "stitch_via_size_mm": None,
        "stitch_via_drill_mm": None,
    }
    if copy is not None:
        fields["copy_segments"] = [
            {
                "start": _pt(a[0] + origin_x, a[1] + origin_y),
                "end": _pt(b[0] + origin_x, b[1] + origin_y),
                "width_mm": round(w, 4),
            }
            for a, b, w in copy.segments
        ]
        fields["stitch_vias"] = [_pt(x + origin_x, y + origin_y) for x, y in copy.stitch_vias]
        fields["stitch_via_size_mm"] = round(copy.via_size_mm, 4)
        fields["stitch_via_drill_mm"] = round(copy.via_drill_mm, 4)
    return fields


def is_parallel(wire: dict[str, Any]) -> bool:
    """True for a parallel-copy record; an absent ``strategy`` is a wire."""
    return bool(wire.get("strategy", STRATEGY_WIRE) == STRATEGY_PARALLEL)


def wire_key(wire: dict[str, Any]) -> tuple[Any, ...]:
    """Identity of a wire: net + layer + leg start/end (end order ignored).

    A parallel copy (Issue #8916) is keyed on BOTH its layers, so a re-run
    that meets the leg from its copy's side replaces the record, not adds one.
    """
    ends = sorted(
        [
            (wire["leg_start"]["x"], wire["leg_start"]["y"]),
            (wire["leg_end"]["x"], wire["leg_end"]["y"]),
        ]
    )
    if is_parallel(wire):
        layers = "+".join(sorted({wire["layer"], wire.get("copy_layer") or ""} - {""}))
        return (wire["net"], f"parallel:{layers}", tuple(ends))
    return (wire["net"], wire["layer"], tuple(ends))


def sort_wires(wires: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(wires, key=wire_key)


def merge_wires(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace wires whose key matches a new one; keep the rest; sort."""
    merged = {wire_key(w): w for w in existing}
    for w in new:
        merged[wire_key(w)] = w
    return sort_wires(list(merged.values()))


def _near(a: dict[str, float], b: tuple[float, float], tol: float = 0.05) -> bool:
    return abs(a["x"] - b[0]) <= tol and abs(a["y"] - b[1]) <= tol


def wires_for_net(wires: list[dict[str, Any]], net: str) -> list[dict[str, Any]]:
    return [w for w in wires if w.get("net") == net]


def wires_with_anchors(
    wires: list[dict[str, Any]], net: str, removed: list[tuple[float, float]]
) -> list[dict[str, Any]]:
    """Wires of ``net`` that have an anchor at one of the ``removed`` (sheet-absolute) points."""
    return [
        w
        for w in wires_for_net(wires, net)
        if any(_near(a, r) for a in w.get("anchors", []) for r in removed)
    ]


def drop_wires(
    path: Path,
    net: str,
    removed: list[tuple[float, float]],
    removed_copies: list[tuple[Any, ...]] | None = None,
) -> int:
    """Delete the sidecar wires of ``net`` anchored at a removed point; returns the count.

    ``removed_copies`` are the :func:`wire_key` s of parallel-copy records whose
    copper was removed (Issue #8916); those records go too. A no-op when the
    sidecar is absent. The file is rewritten deterministically.

    Raises:
        ValueError: The sidecar holds an entry that is not a JSON object
            (:func:`load_wire_records`). Nothing is written then.
    """
    if not path.is_file():
        return 0
    wires = load_wire_records(path)
    gone = wires_with_anchors(wires, net, removed)
    keys = set(removed_copies or ())
    gone += [w for w in wires_for_net(wires, net) if is_parallel(w) and wire_key(w) in keys]
    if not gone:
        return 0
    gone_ids = {id(w) for w in gone}
    kept = [w for w in wires if id(w) not in gone_ids]
    text = json.dumps(
        {"version": SIDECAR_VERSION, "wires": sort_wires(kept)}, indent=2, sort_keys=True
    )
    path.write_text(text + "\n", encoding="utf-8")
    return len(gone)


def load_wires(path: Path) -> list[dict[str, Any]]:
    """Read a sidecar's wires, every entry as written. Raises ``OSError``/``ValueError``.

    An entry that is not a JSON object comes back too: the writers
    (:func:`write_sidecar`, :func:`drop_wires`) read through this and must not
    drop anything from the owner's file. Read-only consumers go through
    :func:`load_wires_if_present`.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != SIDECAR_VERSION:
        raise ValueError(f"{path}: not a version {SIDECAR_VERSION} reinforcement sidecar")
    wires = data.get("wires")
    if not isinstance(wires, list):
        raise ValueError(f"{path}: missing 'wires' list")
    return wires


def _shift_pt(pt: dict[str, float], dx_mm: float, dy_mm: float) -> dict[str, float]:
    """A copy of point ``pt`` moved by (dx, dy), rounded to the sidecar precision."""
    return _pt(pt["x"] + dx_mm, pt["y"] + dy_mm)


def translate_sidecar_wires(
    wires: list[dict[str, Any]], dx_mm: float, dy_mm: float
) -> list[dict[str, Any]]:
    """Wire records rigidly translated by (``dx_mm``, ``dy_mm``) (Issue #10250).

    Only the sheet-absolute coordinate points move: ``leg_start``,
    ``leg_end``, ``anchors`` and, on parallel-copy records, ``polyline``,
    ``copy_segments[].start/.end`` and ``stitch_vias``. Lengths, gauges, nets
    and every other field are translation-invariant and copied unchanged.
    The input is not modified; the result is re-sorted like every sidecar
    write.
    """
    out: list[dict[str, Any]] = []
    for wire in wires:
        moved = dict(wire)
        for key in ("leg_start", "leg_end"):
            if isinstance(wire.get(key), dict):
                moved[key] = _shift_pt(wire[key], dx_mm, dy_mm)
        for key in ("anchors", "polyline", "stitch_vias"):
            if isinstance(wire.get(key), list):
                moved[key] = [_shift_pt(p, dx_mm, dy_mm) for p in wire[key]]
        if isinstance(wire.get("copy_segments"), list):
            moved["copy_segments"] = [
                {
                    **seg,
                    "start": _shift_pt(seg["start"], dx_mm, dy_mm),
                    "end": _shift_pt(seg["end"], dx_mm, dy_mm),
                }
                for seg in wire["copy_segments"]
            ]
        out.append(moved)
    return sort_wires(out)


def render_sidecar(wires: list[dict[str, Any]]) -> str:
    """The deterministic sidecar file text for ``wires``."""
    text = json.dumps({"version": SIDECAR_VERSION, "wires": wires}, indent=2, sort_keys=True)
    return text + "\n"


def load_wires_if_present(board: Path) -> list[dict[str, Any]]:
    """Wires of the board's sidecar; empty when absent or unreadable (#8746).

    Only the object entries of ``wires`` are returned, in file order and with
    their fields untouched; an entry that is not a JSON object (``null``, a
    string, a number, an array) is left out, so a reader can treat every
    element as a record (Issue #10398). Fields inside a record are not
    validated, and the file is never rewritten.
    """
    path = sidecar_path(board)
    if not path.is_file():
        return []
    try:
        return [w for w in load_wires(path) if isinstance(w, dict)]
    except (OSError, ValueError):
        return []


def write_sidecar(board: Path, new_wires: list[dict[str, Any]], record: Path | None = None) -> Path:
    """Merge ``new_wires`` into the board's sidecar and write it deterministically.

    An existing sidecar is read with :func:`load_wire_records`: one that holds
    an entry which is not a JSON object raises ``ValueError`` and is left as
    it is (Issue #10401), rather than being rewritten without the entry.
    """
    path = record if record is not None else sidecar_path(board)
    existing = load_wire_records(path) if path.is_file() else []
    wires = merge_wires(existing, new_wires)
    text = json.dumps({"version": SIDECAR_VERSION, "wires": wires}, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def _json_type(value: Any) -> str:
    """A loaded entry's JSON type, as a phrase that reads after ``is``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    return type(value).__name__


def check_wire_records(path: Path, wires: Sequence[Any]) -> None:
    """Raise ``ValueError`` when an entry of ``wires`` is not a JSON object (Issue #10401).

    A hand-edited sidecar can carry ``null``, a number, a string, an array or
    a boolean among its records. :func:`load_wires` returns those as they are,
    so a writer never drops them from the owner's file; a caller that reads
    the list as records calls this first and refuses the whole list, before
    it filters by net, looks for a match or saves anything. The message names
    the sidecar, the zero-based index of the first offending entry and what
    every entry must be. Fields inside an object are not checked.
    """
    for index, wire in enumerate(wires):
        if not isinstance(wire, dict):
            raise ValueError(f"{path}: wires[{index}] is {_json_type(wire)}, not a JSON object")


def load_wire_records(path: Path) -> list[dict[str, Any]]:
    """:func:`load_wires` for a consumer that reads the list as records (Issue #10401).

    Raises ``OSError`` / ``ValueError`` when the file, its envelope or one of
    its entries is unreadable; nothing is written either way.
    """
    wires = load_wires(path)
    check_wire_records(path, wires)
    return wires


# ---------------------------------------------------------------- adopt (#9419)


class AdoptResult:
    """Outcome of :func:`adopt_wire_records`: records built, strips left unresolved."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        #: ``(net, layer, (x, y), why)``, sheet-absolute; reported, never guessed.
        self.unresolved: list[tuple[str, str, tuple[float, float], str]] = []


def _dist_to_seg(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    sq = dx * dx + dy * dy
    if sq <= 1e-18:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / sq))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def adopt_wire_records(pcb: Any, net: str | None = None) -> AdoptResult:
    """Build wire records from strips and anchors already on the board (Issue #9419).

    ``pcb reinforce`` leaves, per wire, one continuous solder-mask strip (a
    chain of ``gr_line`` pieces on the leg's side's mask layer) from anchor to
    anchor, and a plated anchor hole of the wire's drill at each end. A board
    whose wires were re-laid by hand keeps both but loses the sidecar; this
    reads them back, moving nothing. A strip chain becomes a record only when
    every piece lies on one net's copper on the matching outer layer, the
    chain is a simple open path, and each end stands on a same-net hole whose
    drill is a supported wire gauge's anchor drill (both ends the same gauge).
    Anything else is returned in ``unresolved`` rather than guessed.

    ``net`` restricts the adoption to one net. Coordinates are written
    sheet-absolute, like every other sidecar coordinate.
    """
    from kicad_tools.core.geometry import rotate_pad_offset
    from kicad_tools.pcb.reinforce import MASK_LAYER_FOR_COPPER
    from kicad_tools.physics.wire_gauge import anchor_drill_for_awg, supported_gauges
    from kicad_tools.validate.rules.reinforcement_incomplete import (
        DRILL_TOLERANCE_MM,
        MASK_TOLERANCE_MM,
    )

    out = AdoptResult()
    ox, oy = getattr(pcb, "board_origin", None) or (0.0, 0.0)
    Pt = tuple[float, float]

    def absolute(p: Pt) -> Pt:
        return (p[0] + ox, p[1] + oy)

    wanted_number: int | None = None
    if net is not None:
        netobj = pcb.get_net_by_name(net)
        if netobj is None:
            return out
        wanted_number = netobj.number
    net_names = {number: n.name for number, n in pcb.nets.items()}

    gauge_by_drill = [(awg, anchor_drill_for_awg(awg)) for awg in supported_gauges()]

    def gauge_for(drill: float) -> int | None:
        for awg, d in gauge_by_drill:
            if abs(d - drill) <= DRILL_TOLERANCE_MM:
                return awg
        return None

    # Same-net holes, board-relative: (net_number, centre, drill).
    holes: list[tuple[int, Pt, float]] = [
        (v.net_number, (v.position[0], v.position[1]), v.drill) for v in pcb.vias if v.drill > 0
    ]
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.type != "thru_hole" or pad.drill <= 0:
                continue
            off = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
            holes.append(
                (pad.net_number, (fp.position[0] + off[0], fp.position[1] + off[1]), pad.drill)
            )

    for copper, mask in MASK_LAYER_FOR_COPPER.items():
        pieces = [
            ((g.start[0] - ox, g.start[1] - oy), (g.end[0] - ox, g.end[1] - oy))
            for g in pcb.graphic_lines
            if g.layer == mask and math.dist(g.start, g.end) > 1e-9
        ]
        if not pieces:
            continue
        segs_by_net: dict[int, list[tuple[Pt, Pt, float]]] = {}
        for seg in pcb.segments:
            if seg.layer == copper:
                segs_by_net.setdefault(seg.net_number, []).append(
                    (tuple(seg.start), tuple(seg.end), float(seg.width))
                )

        def on_net(point: Pt, number: int, segs_by_net: dict = segs_by_net) -> bool:
            return any(
                _dist_to_seg(point, a, b) <= w / 2.0 + MASK_TOLERANCE_MM
                for a, b, w in segs_by_net.get(number, [])
            )

        # Each piece belongs to the one net whose copper both its ends stand on.
        by_net: dict[int, list[tuple[Pt, Pt]]] = {}
        for a, b in pieces:
            owners = [n for n in segs_by_net if on_net(a, n) and on_net(b, n)]
            if len(owners) == 1:
                by_net.setdefault(owners[0], []).append((a, b))
            elif owners:
                out.unresolved.append(
                    ("", copper, absolute(a), "strip lies on several nets' copper")
                )
        for number, net_pieces in sorted(by_net.items()):
            if wanted_number is not None and number != wanted_number:
                continue
            name = net_names.get(number, "")
            # Chains: pieces whose ends meet (within the strip tolerance).
            parent = list(range(len(net_pieces)))

            def find(i: int, parent: list[int] = parent) -> int:
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i

            for i, (a1, b1) in enumerate(net_pieces):
                for j in range(i + 1, len(net_pieces)):
                    a2, b2 = net_pieces[j]
                    if any(
                        math.dist(p, q) <= MASK_TOLERANCE_MM for p in (a1, b1) for q in (a2, b2)
                    ):
                        parent[find(i)] = find(j)
            chains: dict[int, list[tuple[Pt, Pt]]] = {}
            for i, piece in enumerate(net_pieces):
                chains.setdefault(find(i), []).append(piece)
            for chain in chains.values():
                record = _adopt_chain(chain, name, number, copper, holes, gauge_for, absolute, out)
                if record is not None:
                    out.records.append(record)
    out.records = sort_wires(out.records)
    return out


def _adopt_chain(
    chain: list[tuple[tuple[float, float], tuple[float, float]]],
    net: str,
    number: int,
    layer: str,
    holes: list[tuple[int, tuple[float, float], float]],
    gauge_for: Any,
    absolute: Any,
    out: AdoptResult,
) -> dict[str, Any] | None:
    """One strip chain -> a record, or ``None`` with the reason in ``out.unresolved``."""
    from kicad_tools.physics.wire_gauge import bare_copper_diameter_mm
    from kicad_tools.validate.rules.reinforcement_incomplete import (
        ANCHOR_ON_LEG_SLACK_MM,
        MASK_TOLERANCE_MM,
    )

    # Distinct end points of the chain, with how many piece ends meet there.
    nodes: list[list[Any]] = []  # [point, count]
    for a, b in chain:
        for p in (a, b):
            for node in nodes:
                if math.dist(node[0], p) <= MASK_TOLERANCE_MM:
                    node[1] += 1
                    break
            else:
                nodes.append([p, 1])
    here = absolute(chain[0][0])

    def fail(why: str) -> None:
        out.unresolved.append((net, layer, here, why))

    ends = [n[0] for n in nodes if n[1] == 1]
    if len(ends) != 2 or any(n[1] > 2 for n in nodes):
        fail("strip is not a single open anchor-to-anchor run (branched or closed)")
        return None
    anchors = []
    drills = []
    for end in ends:
        best = None
        for hole_net, centre, drill in holes:
            if hole_net != number:
                continue
            d = math.dist(centre, end)
            if d <= ANCHOR_ON_LEG_SLACK_MM + MASK_TOLERANCE_MM and (best is None or d < best[0]):
                best = (d, centre, drill)
        if best is None:
            fail(f"no same-net hole at the strip end {absolute(end)}")
            return None
        anchors.append(best[1])
        drills.append(best[2])
    awg = gauge_for(drills[0])
    if awg is None or gauge_for(drills[1]) != awg:
        fail("end holes are not a supported wire gauge's anchor drill")
        return None
    length = sum(math.dist(a, b) for a, b in chain)
    a_abs = [absolute(a) for a in anchors]
    straight = math.hypot(a_abs[1][0] - a_abs[0][0], a_abs[1][1] - a_abs[0][1])
    return {
        "net": net,
        "layer": layer,
        "kind": "run",
        "path": None,
        "paths": [],
        "leg_start": _pt(*absolute(ends[0])),
        "leg_end": _pt(*absolute(ends[1])),
        "anchors": [_pt(*a) for a in a_abs],
        "length_mm": round(length, 4),
        "straight_line_mm": round(straight, 4),
        "wire_gauge_awg": awg,
        "max_end_shift_mm": None,
        "bare_diameter_mm": round(bare_copper_diameter_mm(awg), 4),
        "mask_opening": None,
        "status": "adopted",
        "reason": "adopted from the solder-mask strip and anchor holes already on the board",
    }


def already_recorded(existing: list[dict[str, Any]], record: dict[str, Any]) -> bool:
    """True when ``existing`` already has a wire of the same net and layer at these anchors.

    Adoption must not shadow a richer ``pcb reinforce`` record (which carries
    its path name and leg ends, so a different :func:`wire_key`) with a bare
    one for the same wire.
    """
    anchors = [(a["x"], a["y"]) for a in record.get("anchors", [])]
    if not anchors:
        return False
    for w in existing:
        if w.get("net") != record["net"] or w.get("layer") != record["layer"]:
            continue
        have = w.get("anchors") or []
        if len(have) == len(anchors) and all(any(_near(h, a) for h in have) for a in anchors):
            return True
    return False


def format_wire_list(wires: list[dict[str, Any]]) -> str:
    """Human-readable wire list."""
    if not wires:
        return "No wires recorded."
    lines = [f"{len(wires)} wire(s):"]
    for i, w in enumerate(wires):
        anchors = ", ".join(f"({a['x']:.2f}, {a['y']:.2f})" for a in w["anchors"]) or "none"
        lines.append(
            f"  {i}. {w['net']} {w['layer']} {w['length_mm']:.1f} mm "
            f"{w['wire_gauge_awg']} AWG [{w['status']}] anchors: {anchors}"
        )
        if is_parallel(w):
            lines.append(
                f"     parallel copy on {w.get('copy_layer') or '-'} "
                f"[{w.get('copy_status') or '-'}]: {len(w.get('copy_segments') or [])} "
                f"segment(s), {len(w.get('stitch_vias') or [])} stitching via(s)"
            )
        if w.get("path"):
            lines.append(f"     path: {w['path']}")
    return "\n".join(lines)
