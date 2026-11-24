"""Check one saved TX2 replay board with the pinned CLI and Board 06's own fixture footprints."""

import argparse
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def write_fixture_libraries(staging):
    # This function runs inside the pinned KiCad image. Its only board input
    # comes from the independently re-run generator, never from the candidate.
    import pcbnew

    io = pcbnew.PCB_IO_KICAD_SEXPR()
    board = pcbnew.LoadBoard(str(staging / "pristine.kicad_pcb"))
    names = set()
    for fp in board.GetFootprints():
        nickname = str(fp.GetFPID().GetLibNickname())
        names.add(nickname)
        library = staging / (nickname + ".pretty")
        library.mkdir(exist_ok=True)
        footprint = pcbnew.FOOTPRINT(fp)
        footprint.SetOrientationDegrees(0)
        footprint.SetPosition(pcbnew.VECTOR2I(0, 0))
        footprint.SetReference("REF**")
        for pad in footprint.Pads():
            pad.SetNetCode(0)
        io.FootprintSave(str(library), footprint)

    entries = [
        f'(lib (name "{name}") (type "KiCad") (uri "${{KIPRJMOD}}/{name}.pretty") '
        '(options "") (descr "Board 06 generator"))'
        for name in sorted(names)
    ]
    (staging / "fp-lib-table").write_text(
        "(fp_lib_table (version 7)\n" + "\n".join(entries) + "\n)\n"
    )


def check(board, output):
    from kicad_tools.cli.kicad_toolchain import (
        docker_kicad_cli_prefix,
        finalize_native_drc_report,
        scratch_config_home,
    )

    if output.exists():
        raise FileExistsError(output)
    with tempfile.TemporaryDirectory(prefix="kct-tx2-native-") as tmp, scratch_config_home() as cfg:
        staging = Path(tmp).resolve()
        generator = HERE.parents[1] / "boards/06-diffpair-test/generate_pcb.py"
        (staging / "pristine.kicad_pcb").write_text(
            runpy.run_path(str(generator))["generate_pcb"]()
        )
        saved = staging / "routed.kicad_pcb"
        shutil.copyfile(board, saved)
        for ext in (".kicad_pro", ".kicad_dru"):
            sidecar = board.with_suffix(ext)
            if board.parent == HERE and not sidecar.exists():
                sidecar = HERE / ("routed" + ext)
            shutil.copyfile(sidecar, saved.with_suffix(ext))
        script = Path(__file__).resolve()
        py = docker_kicad_cli_prefix([staging, cfg, script], cfg, entrypoint="python3")
        subprocess.run([*py, str(script), "--write-libraries", str(staging)], check=True)
        cli = docker_kicad_cli_prefix([staging, cfg], cfg)
        version = subprocess.check_output([*cli, "--version"], text=True).strip()
        if version != "10.0.6":
            raise RuntimeError(f"The captured replay needs KiCad 10.0.6, found {version}")
        raw = staging / "report.json"
        subprocess.run(
            [
                *cli,
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-all",
                "--units",
                "mm",
                "--output",
                str(raw),
                str(saved),
            ],
            check=True,
        )
        report = finalize_native_drc_report(raw, source=board.name)
        with output.open("xb") as dest:
            dest.write(raw.read_bytes())
    counts = Counter(item["severity"] for item in report["violations"])
    print(
        json.dumps(
            {
                "board": board.name,
                "kicad": version,
                "violations": counts,
                "unconnected_items": len(report["unconnected_items"]),
            }
        )
    )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--write-libraries":
        write_fixture_libraries(Path(sys.argv[2]))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("board", type=Path)
        parser.add_argument("report", type=Path, help="new JSON report path")
        args = parser.parse_args()
        check(args.board.resolve(), args.report)
