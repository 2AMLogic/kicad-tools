#!/usr/bin/env bash
# scripts/ci/jobs/board-02-end-to-end.sh -- board 02 ("charlieplex-led")
# end-to-end gate (Issue #3783). The single copy of the `board-02-end-to-end`
# job's check commands; both .github/workflows/ci.yml's job and
# scripts/ci/local-gate.sh run this file (2am#7561 ruling). Same gate shape as
# boards 00/01; board 02 hard-gates on both the copper-extracted and
# label-based LVS comparators (require_clean=True), which is green since the
# layer-aware _build_segment_chains fix.
#
# PYTHONHASHSEED is pinned so the regen is reproducible across environments
# (Issue #3799; the recipe also re-pins it on its own route subprocess, but
# pinning here covers the whole regen pipeline for belt-and-suspenders
# determinism).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

export PYTHONHASHSEED="42"
export PYTHONUNBUFFERED="1"

step "Build C++ router backend"
uv run kct build-native

step "Regenerate board 02 from recipe (clean tmp directory)"
rm -rf /tmp/board02-ci
mkdir -p /tmp/board02-ci
uv run python boards/02-charlieplex-led/generate_design.py /tmp/board02-ci

step "Stage the candidate export for manifest-consuming checks"
# The recipe leaves its export in manufacturing-candidate/ (release publication
# belongs to package_release.py). kct check's manifest gate, board-metrics and
# check_board_00_e2e.py all read output/manufacturing/, so expose the candidate
# under that name in this disposable staging copy only -- never in the recipe
# output. Staging must precede the first of those checks.
rm -rf /tmp/board02-staging
mkdir -p /tmp/board02-staging/02-charlieplex-led/output
cp -r /tmp/board02-ci/. /tmp/board02-staging/02-charlieplex-led/output/
STAGED_OUT=/tmp/board02-staging/02-charlieplex-led/output
if [ -d "$STAGED_OUT/manufacturing-candidate" ] && [ ! -e "$STAGED_OUT/manufacturing" ]; then
  mv "$STAGED_OUT/manufacturing-candidate" "$STAGED_OUT/manufacturing"
fi
test -f "$STAGED_OUT/manufacturing/manifest.json"

step "Assert kct check Overall PASSED on regenerated routed PCB (staged)"
uv run kct check "$STAGED_OUT/charlieplex_3x3_routed.kicad_pcb" \
  | tee /tmp/board02-ci/check.out
grep -qE '^Overall:[[:space:]]+PASSED' /tmp/board02-ci/check.out

step "Emit board.json via kct board-metrics"
uv run kct board-metrics /tmp/board02-staging/02-charlieplex-led

step "Assert plane-net completion (no floating pads, #4531)"
uv run python scripts/ci/check_net_status.py \
  /tmp/board02-ci/charlieplex_3x3_routed.kicad_pcb

step "Assert artifacts + lvs.json + board.json fields"
uv run python scripts/ci/check_board_00_e2e.py \
  --stem charlieplex_3x3 \
  --routing-only \
  /tmp/board02-staging/02-charlieplex-led
