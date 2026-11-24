"""KiCad PCB item locks and generator ownership inherited through groups."""

from __future__ import annotations

from kicad_tools.sexp import SExp


def is_locally_locked(item: SExp) -> bool:
    """Read an item's own KiCad 6/7 bare flag or its newer lock property."""
    atoms = item.get_atoms()
    if item.name == "group":
        # The first argument is the user-chosen name, which can itself be "locked".
        atoms = atoms[1:]
    if "locked" in atoms:
        return True
    flag = item.find_child("locked")
    return flag is not None and flag.get_first_atom() in (None, "yes", "true")


def _item_id(item: SExp) -> str | None:
    # KiCad 6/7 used tstamp for items and id for groups. Newer boards use uuid.
    for tag in ("uuid", "tstamp", "id"):
        field = item.find_child(tag)
        if field is not None:
            value = field.get_string(0)
            return value.lower() if value else None
    return None


def _descendants(members_by_group: dict[str, list[str]], pending: list[str]) -> set[str]:
    descendants: set[str] = set()
    while pending:
        member = pending.pop()
        if member not in descendants:
            descendants.add(member)
            pending.extend(members_by_group.get(member, ()))
    return descendants


class BoardItemLocks:
    """Index locked and generator-owned members separately for a board's copper edits."""

    def __init__(self, board: SExp):
        members_by_group: dict[str, list[str]] = {}
        locked: list[str] = []
        generated: list[str] = []
        # Group definitions belong to the board or a footprint. Nested groups
        # reference one another by UUID in members, independently of file order.
        for owner in (board, *board.find_children("footprint"), *board.find_children("module")):
            for group in owner.children:
                if group.name not in ("group", "generated"):
                    continue
                ids = [
                    str(member).lower()
                    for members in group.find_children("members")
                    for member in members.get_atoms()
                ]
                group_id = _item_id(group)
                if group_id:
                    members_by_group[group_id] = ids
                if is_locally_locked(group):
                    locked.extend(ids)
                if group.name == "generated":
                    generated.extend(ids)

        self._locked_members = _descendants(members_by_group, locked)
        self._generated_members = _descendants(members_by_group, generated)

    def is_locked(self, item: SExp) -> bool:
        """An explicit item unlock still inherits the lock of any enclosing group."""
        return is_locally_locked(item) or (
            bool(self._locked_members) and _item_id(item) in self._locked_members
        )

    def is_generator_owned(self, item: SExp) -> bool:
        """A generator owns every track in its groups, even when it is unlocked."""
        return bool(self._generated_members) and _item_id(item) in self._generated_members
