"""Read saved pad connectivity with the selected KiCad Python interpreter."""

import json
import sys
from collections import Counter


def saved_groups(path):
    import pcbnew  # type: ignore[import-not-found]  # Supplied by KiCad's Python.

    board = pcbnew.LoadBoard(path)
    if not board.BuildConnectivity():
        raise RuntimeError("KiCad could not build saved-board connectivity")
    graph = board.GetConnectivity()
    # Use physical pad identities across loads. Legacy boards can receive new
    # native UUIDs on load. A UUID is needed only to distinguish coincident,
    # identically labelled pads on the same layers.
    pads = {}
    identities = {}
    for pad in board.GetPads():
        if not pad.GetNetname() or not pad.GetLayerSet().CuStack():
            continue
        uuid = pad.m_Uuid.AsString()
        if uuid in pads:
            raise ValueError("duplicate native pad UUID")
        position = pad.GetPosition()
        pads[uuid] = pad
        identities[uuid] = (
            pad.GetNetname(),
            pad.GetParentFootprint().GetReference(),
            pad.GetNumber(),
            position.x,
            position.y,
            tuple(pad.GetLayerSet().CuStack()),
        )
    counts = Counter(identities.values())
    remaining = set(pads)
    groups = []
    while remaining:
        uuid = next(iter(remaining))
        pad = pads[uuid]
        # GetConnectedItems follows the whole copper island, including tracks,
        # vias and filled zones, from the named physical pad.
        members = {
            item.m_Uuid.AsString()
            for item in graph.GetConnectedItems(pad)
            if item.Type() == pcbnew.PCB_PAD_T and item.GetNetCode() == pad.GetNetCode()
        }
        if uuid not in members or not members <= remaining:
            raise RuntimeError("incomplete or ambiguous native pad group")
        group = []
        for member in members:
            identity = identities[member]
            key = (*identity, member) if counts[identity] > 1 else identity
            group.append(json.dumps(key))
        groups.append(sorted(group))
        remaining.difference_update(members)
    return sorted(groups)


if __name__ == "__main__":
    print(json.dumps(saved_groups(sys.argv[1])))
