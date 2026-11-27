"""Run with KiCad 10.0.6's pcbnew Python to regenerate oracle.json.

Each pattern has an isolated Probe class: membership cannot be masked by
another assignment or by precedence. This script does not load/save projects.

The capture runs on the repository's CI-pinned image
(``kicad/kicad@sha256:18693567392b80da435f9fa952ce3a3e534c66eb5a6033f5b9c80aa3b19dd3ec``,
the exact build ``kicad/kicad:10.0`` resolved to when CI pinned it), so the
recorded membership is native evidence from the same release ``kct``'s native
verifications use (issue #10122)::

    docker run --rm -v "$PWD:/work" -w /work \
      kicad/kicad@sha256:18693567392b80da435f9fa952ce3a3e534c66eb5a6033f5b9c80aa3b19dd3ec \
      python3 tests/fixtures/netclass_diagnostics/capture_kicad.py \
      > tests/fixtures/netclass_diagnostics/oracle.json
"""

import json

import pcbnew

version = pcbnew.GetBuildVersion()
if version != "10.0.6":
    raise SystemExit(f"Expected KiCad 10.0.6, got {version}")
patterns = [
    "",
    "USB",
    "USB_*",
    "USB?",
    "USB*",
    "*",
    "?",
    "*USB*",
    "a*b?",
    "USB_D",
    "usb_*",
    "A-B",
    "A B",
    "USB.1",
    r"USB\*",
    "USB[0-9]",
    "^USB$",
    "/USB/",
    "a**",
    # Supply rails and generated names with punctuation (issue #10122).
    "+3.3V",
    "+5V",
    "Net-(U5-CAPP)",
    "Net-U5-CAPP",
    # Regex-any and one-or-more controls distinguishing the union contract
    # from a blanket literal treatment of punctuation.
    "A.B",
    "C+P",
    "A.+B",
    "USB.X",
    # Deliberately unsupported syntax: boundary evidence only.
    "A|B",
    "(AB)*",
    "((A))",
    "A{2}",
    "Net-(U5-CAP)?",
    # Router-generated expressions with a strict end anchor (issue #10333).
    r"^USB\z$",
    r"^A\x2EB\z$",
    r"^\x2B5V\z$",
    r"^é\z$",
]
nets = [
    "é",
    "😀",
    "\n\n",
    "USB\nX",
    "Net-(U1-Pad1)",
    "/sheet/USB",
    "USB_é",
    "USB_😀",
    "USB_\n",
    "USB\n",
    "\n",
    "",
    "USB",
    "USB_D",
    "USB__",
    "USB_",
    "usb_D",
    "XUSB",
    "US",
    "USBB",
    "USB1",
    "USB.1",
    "USB*",
    r"USB\x",
    "USB[0-9]",
    "A-B",
    "A B",
    "ab",
    "aaab",
    "a",
    "b",
    "XYZ",
    "/USB/",
    # Issue #10122 nets: supply spellings, parenthesized and unparenthesized
    # generated spellings, and similarly named negatives.
    "+3.3V",
    "+3x3V",
    "+5V",
    "Net-(U5-CAPP)",
    "Net-U5-CAPP",
    "Net-(U5-CAPX)",
    "Net-U5-CAPX",
    "AXB",
    "AXYB",
    "AB",
    "A.B",
    "CCP",
    "CPP",
    "CP",
    "CCCCCP",
    "C+P",
    "USBX",
    "USB.X",
    "A.B\n",
    "+5V\n",
    "é\n",
    # The wildcard interpretation sees the pattern text itself.
    r"^USB\z$",
    "^USB\\z$\n",
]
cases = []
for pattern in patterns:
    settings = pcbnew.NET_SETTINGS(None, "net_settings")
    settings.SetNetclass("Probe", pcbnew.NETCLASS("Probe"))
    settings.SetNetclassPatternAssignment(pattern, "Probe")
    for net in nets:
        cases.append(
            {
                "pattern": pattern,
                "net": net,
                "matches": settings.GetEffectiveNetClass(net).GetName() == "Probe",
            }
        )
print(json.dumps({"kicad_version": version, "source_tag": "10.0.6", "cases": cases}, indent=2))
