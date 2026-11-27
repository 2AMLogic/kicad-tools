"""Pin native lock inheritance for board items and their UUID-based groups."""

import pytest

from kicad_tools.core.pcb_item_locks import BoardItemLocks
from kicad_tools.sexp import parse_sexp, parse_string


def test_legacy_nested_group_ids_and_case_insensitive_members():
    doc = parse_sexp(
        """(kicad_pcb
          (via (tstamp aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa) (locked no))
          (group "outer" locked (id root) (members CHILD))
          (group "child" (id child) (members AAAAAAAA-AAAA-4AAA-AAAA-AAAAAAAAAAAA)))"""
    )
    via = doc.find_child("via")
    assert via is not None
    assert BoardItemLocks(doc).is_locked(via)


def test_group_name_is_not_a_lock_and_descendants_do_not_lock_siblings():
    doc = parse_sexp(
        """(kicad_pcb
          (group "locked" (uuid outer) (members a child))
          (group "child" (uuid child) (locked yes) (members b))
          (segment (uuid a))
          (via (uuid b)))"""
    )
    segment, via = doc.find_child("segment"), doc.find_child("via")
    assert segment is not None and via is not None
    locks = BoardItemLocks(doc)
    assert not locks.is_locked(segment)
    assert locks.is_locked(via)


def test_group_membership_cycle_terminates_and_respects_unrelated_items():
    doc = parse_sexp(
        """(kicad_pcb
          (group "a" (uuid a) (members b track))
          (group "b" (uuid b) (locked yes) (members a))
          (segment (uuid track)) (via (uuid unrelated)))"""
    )
    segment, via = doc.find_child("segment"), doc.find_child("via")
    assert segment is not None and via is not None
    locks = BoardItemLocks(doc)
    assert locks.is_locked(segment)
    assert not locks.is_locked(via)


@pytest.mark.parametrize(("flag", "locked"), [("yes", True), ("no", False)])
def test_generated_tracks_and_footprint_group_members(flag, locked):
    doc = parse_sexp(
        f"""(kicad_pcb
          (generated (uuid tuning) (type tuning_pattern) (locked {flag}) (members track))
          (segment (uuid track))
          (footprint "Test:1"
            (pad "1" smd rect (uuid land))
            (group "fixture" (uuid nested) (locked {flag}) (members land))))"""
    )
    segment, pad = doc.find_child("segment"), doc.find("pad")
    assert segment is not None and pad is not None
    locks = BoardItemLocks(doc)
    assert locks.is_locked(segment) is locked
    assert locks.is_locked(pad) is locked
    assert locks.is_generator_owned(segment)
    assert not locks.is_generator_owned(pad)


def test_generator_follows_nested_legacy_ids_through_a_cycle():
    doc = parse_string(
        """(kicad_pcb
          (module "Test"
            (group "inner" (id leaf) (members middle owned))
            (generated (uuid pattern) (members middle))
            (group "outer" (id middle) (members LEAF))
            (segment (tstamp OWNED)))
          (segment (uuid unrelated)))"""
    )
    module = doc.find_child("module")
    assert module is not None
    owned, unrelated = module.find_child("segment"), doc.find_child("segment")
    assert owned is not None and unrelated is not None
    locks = BoardItemLocks(doc)
    assert locks.is_generator_owned(owned)
    assert not locks.is_locked(owned)
    assert not locks.is_generator_owned(unrelated)
