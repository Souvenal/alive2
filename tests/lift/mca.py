#!/usr/bin/env python3
"""llvm-mca analysis for lifted ARM assembly.

Analyzes microarchitecture performance of both source and lifted ARM64
assembly using LLVM's Machine Code Analyzer.

Usage:
    uv run python tests/lift/mca.py <case>                    # analyze both + diff
    uv run python tests/lift/mca.py <case> -k source          # source only
    uv run python tests/lift/mca.py <case> -k lifted          # lifted only
    uv run python tests/lift/mca.py <case> -k diff            # side-by-side comparison
    uv run python tests/lift/mca.py <case> -k both            # both separately
    uv run python tests/lift/mca.py <case> --mcpu=cortex-x2   # custom CPU model
    uv run python tests/lift/mca.py <case> --all-stats        # full stats
    uv run python tests/lift/mca.py <case> --bottleneck       # bottleneck analysis
    uv run python tests/lift/mca.py <case> --json             # JSON output
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from conftest import (
    ARM_LIFTER,
    LLC,
    CASES_DIR,
    CLANG,
    CFLAGS,
    PROJECT_ROOT,
    _to_vm_path,
    _vm_prefix,
    compile_bc_and_o,
    run_command,
    run_in_vm,
)
from test_lift import CASE_MARKS


# --- Regex patterns for parsing llvm-mca summary ---

SUMMARY_RE = re.compile(
    r"Iterations:\s+(\d+)\s+"
    r"Instructions:\s+(\d+)\s+"
    r"Total Cycles:\s+(\d+)\s+"
    r"Total uOps:\s+(\d+)\s*"
)
DISPATCH_RE = re.compile(r"Dispatch Width:\s+(\d+)")
UOPS_PER_CYCLE_RE = re.compile(r"uOps Per Cycle:\s+([\d.]+)")
IPC_RE = re.compile(r"^\s*IPC:\s+([\d.]+)\s*$", re.MULTILINE)
RTHROUGHPUT_RE = re.compile(r"Block RThroughput:\s+([\d.]+)")


# --- Helpers ---

def _extra_cflags(name: str) -> list[str]:
    _, _, extra = CASE_MARKS.get(name, ("must_pass", None, []))
    return extra


def _check_case(name: str) -> Path:
    src = CASES_DIR / f"{name}.c"
    if not src.exists():
        print(f"case '{name}' not found at {src}", file=sys.stderr)
        sys.exit(1)
    return src


def _outdir(base: Path | None) -> Path:
    d = base or Path.cwd() / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_asm(name: str, outdir: Path) -> tuple[Path, Path]:
    """Ensure .nodbg.s and .lifted.s exist. If not, run the full pipeline.

    Returns (nodbg_s, lifted_s).
    """
    nodbg_s = outdir / f"{name}.nodbg.s"
    lifted_s = outdir / f"{name}.lifted.s"

    if nodbg_s.exists() and lifted_s.exists():
        print(f"[mca] using existing .s files in {outdir}", file=sys.stderr)
        return nodbg_s, lifted_s

    print(f"[mca] .s files not found, running full pipeline for '{name}'...",
          file=sys.stderr)
    src = _check_case(name)
    if not Path(ARM_LIFTER).exists():
        print(
            f"ERROR: arm-lifter not found at {ARM_LIFTER}\n"
            f"Build it first: cd {PROJECT_ROOT} && ./build.sh\n"
            f"Or set ARM_LIFTER env var.",
            file=sys.stderr,
        )
        sys.exit(1)

    xcflags = _extra_cflags(name)
    bc, o = compile_bc_and_o(src, outdir, extra_cflags=xcflags)

    # nodbg.ll from source
    nodbg_ll = outdir / f"{name}.nodbg.ll"
    vm_src = _to_vm_path(src)
    r = run_in_vm(
        [CLANG] + CFLAGS + xcflags + ["-g0", "-S", "-emit-llvm", vm_src, "-o",
         str(nodbg_ll)]
    )
    if r.returncode != 0:
        print(f"nodbg compile failed:\n{r.stderr}", file=sys.stderr)
        sys.exit(1)

    # lifted.ll
    lifted_ll = outdir / f"{name}.lifted.ll"
    log = outdir / f"{name}.lift.log"
    cmd = [ARM_LIFTER, str(o), "--src-bc", str(bc), "-o", str(lifted_ll)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    log.write_text(r.stdout + r.stderr)
    if r.returncode != 0:
        print(f"arm-lifter failed (exit {r.returncode}), see {log}",
              file=sys.stderr)
        sys.exit(1)

    # Compile both .ll to .s
    for ll_path, s_path in [(nodbg_ll, nodbg_s), (lifted_ll, lifted_s)]:
        vm_ll = _to_vm_path(ll_path)
        vm_s = _to_vm_path(s_path)
        r = run_in_vm(
            [LLC, "-mtriple=aarch64-linux-gnu", "-O2", vm_ll, "-o", vm_s]
        )
        if r.returncode != 0:
            print(f"LLC failed for {ll_path.name}:\n{r.stderr}", file=sys.stderr)
            sys.exit(1)

    print(f"[mca] {nodbg_s.name}, {lifted_s.name} ready\n", file=sys.stderr)
    return nodbg_s, lifted_s


# --- llvm-mca runner ---

def run_mca(
    s_path: Path,
    label: str,
    mcpu: str = "generic",
    extra_opts: list[str] | None = None,
) -> dict[str, Any]:
    """Run llvm-mca on an assembly file and return parsed results.

    Returns a dict with:
      - label: display name
      - raw: full output text
      - ipc: Instructions Per Cycle (float)
      - total_cycles: Total Cycles (int)
      - block_rt: Block RThroughput (float)
      - dispatch_width: Dispatch Width (int)
      - uops_per_cycle: uOps Per Cycle (float)
      - instruction_count: number of instructions analyzed (int)
      - errors: list of error/warning lines from stderr
    """
    opts = extra_opts or []
    vm_s = _to_vm_path(s_path)
    cmd = (
        _vm_prefix()
        + ["llvm-mca", f"-mtriple=aarch64-linux-gnu", f"-mcpu={mcpu}"]
        + opts
        + [vm_s]
    )

    r = run_command(cmd, timeout=120)

    text = r.stdout
    stderr_lines = [line for line in r.stderr.split("\n") if line.strip()]

    result: dict[str, Any] = {
        "label": label,
        "raw": text,
        "ipc": 0.0,
        "total_cycles": 0,
        "block_rt": 0.0,
        "dispatch_width": 0,
        "uops_per_cycle": 0.0,
        "instruction_count": 0,
        "errors": stderr_lines,
    }

    # Parse key metrics from the text summary block
    if m := SUMMARY_RE.search(text):
        result["instruction_count"] = int(m.group(2))
        result["total_cycles"] = int(m.group(3))

    if m := DISPATCH_RE.search(text):
        result["dispatch_width"] = int(m.group(1))

    if m := IPC_RE.search(text):
        result["ipc"] = float(m.group(1))

    if m := UOPS_PER_CYCLE_RE.search(text):
        result["uops_per_cycle"] = float(m.group(1))

    if m := RTHROUGHPUT_RE.search(text):
        result["block_rt"] = float(m.group(1))

    return result


# --- Presentation ---

def _summary_line(a: dict[str, Any]) -> str:
    """One-line summary of MCA results."""
    label = a["label"].ljust(10)
    parts = [
        f"IPC={a['ipc']:.2f}",
        f"Cycles={a['total_cycles']}",
        f"uOps/cyc={a['uops_per_cycle']:.2f}",
        f"RThrpt={a['block_rt']:.2f}",
        f"Instrs={a['instruction_count']}",
    ]
    return f"  {label}  {'  '.join(parts)}"


def _render_comparison(name: str, source: dict, lifted: dict) -> None:
    """Side-by-side comparison of source vs lifted MCA metrics."""
    print(f"{'─' * 72}")
    print(f"  llvm-mca diff: {name}")
    print(f"{'─' * 72}")

    print(f"  {'Metric':<20} {'Source':>12} {'Lifted':>12} {'Δ':>12}")
    print(f"  {'─' * 56}")

    metrics = [
        ("Instructions",       "instruction_count", "d"),
        ("Total Cycles",       "total_cycles",      "d"),
        ("IPC",                "ipc",                ".2f"),
        ("uOps Per Cycle",     "uops_per_cycle",     ".2f"),
        ("Block RThroughput",  "block_rt",           ".2f"),
        ("Dispatch Width",     "dispatch_width",     "d"),
    ]

    for name_m, key, fmt in metrics:
        sv = source.get(key, 0) or 0
        lv = lifted.get(key, 0) or 0

        def _fmt(v, f):
            if f == "d":
                return f"{v}"
            return f"{v:{f}}"

        s = _fmt(sv, fmt)
        l = _fmt(lv, fmt)

        if isinstance(sv, (int, float)) and isinstance(lv, (int, float)):
            delta = lv - sv
            # Lower is better for: Instructions, Total Cycles, Block RThroughput
            # Higher is better for: IPC, uOps Per Cycle
            if name_m in ("Instructions", "Total Cycles", "Block RThroughput"):
                better = delta <= 0
            else:
                better = delta >= 0

            sign = "+" if delta > 0 else ""
            d = f"{sign}{delta:{fmt}}"
            d += " ✓" if better else " ✗"
        else:
            d = "N/A"

        print(f"  {name_m:<20} {s:>12} {l:>12} {d:>12}")

    print()

    if source.get("errors"):
        print(f"  ⚠  source warnings:")
        for e in source["errors"][:5]:
            print(f"      {e}")
    if lifted.get("errors"):
        print(f"  ⚠  lifted warnings:")
        for e in lifted["errors"][:5]:
            print(f"      {e}")


def _render_separate(results: list[dict[str, Any]]) -> None:
    """Print MCA raw output for each variant."""
    for r in results:
        print(f"{'─' * 72}")
        print(f"  llvm-mca ({r['label']}):")
        print(f"{'─' * 72}")
        print(r["raw"])
        if r["errors"]:
            print(f"  ⚠  warnings:")
            for e in r["errors"][:5]:
                print(f"      {e}")
        print()


def _render_json(results: list[dict[str, Any]]) -> None:
    """Output all data as JSON."""
    output: dict[str, Any] = {
        "results": [
            {
                "label": r["label"],
                "ipc": r["ipc"],
                "total_cycles": r["total_cycles"],
                "uops_per_cycle": r["uops_per_cycle"],
                "block_rthroughput": r["block_rt"],
                "instruction_count": r["instruction_count"],
                "dispatch_width": r["dispatch_width"],
                "warnings": r["errors"],
                "raw": r["raw"],
            }
            for r in results
        ]
    }
    if len(results) == 2:
        r0, r1 = results
        output["comparison"] = {
            "ipc_delta": round(r1["ipc"] - r0["ipc"], 4),
            "cycles_delta": r1["total_cycles"] - r0["total_cycles"],
            "block_rt_delta": round(r1["block_rt"] - r0["block_rt"], 4),
            "instr_delta": r1["instruction_count"] - r0["instruction_count"],
        }
    print(json.dumps(output, indent=2))


# --- Main command ---

def cmd_mca(
    name: str,
    outdir: Path,
    *,
    kind: str = "both",
    mcpu: str = "generic",
    all_stats: bool = False,
    bottleneck: bool = False,
    json_output: bool = False,
) -> None:
    """Run llvm-mca analysis for the given case."""
    nodbg_s, lifted_s = _ensure_asm(name, outdir)

    extra_opts = []
    if all_stats:
        extra_opts.append("--all-stats")
    if bottleneck:
        extra_opts.append("--bottleneck-analysis")

    results = []

    if kind in ("source", "both", "diff"):
        r_source = run_mca(nodbg_s, "source", mcpu=mcpu, extra_opts=extra_opts)
        results.append(r_source)

    if kind in ("lifted", "both", "diff"):
        r_lifted = run_mca(lifted_s, "lifted", mcpu=mcpu, extra_opts=extra_opts)
        results.append(r_lifted)

    if json_output:
        _render_json(results)
        return

    if kind == "diff" and len(results) == 2:
        _render_comparison(name, results[0], results[1])
    else:
        _render_separate(results)

    # Short summary footer
    if results:
        print(f"{'─' * 72}")
        print(f"  Summary ({name}, -mcpu={mcpu}):")
        for r in results:
            print(_summary_line(r))
        print(f"{'─' * 72}")


# --- CLI ---

def main() -> None:
    parser = argparse.ArgumentParser(
        description="llvm-mca analysis for arm-lifter test cases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--outdir", "-o", type=Path, default=None,
        help="output directory (default: output/ under cwd)",
    )
    parser.add_argument(
        "--kind", "-k", choices=["source", "lifted", "both", "diff"],
        default="both",
        help="which assembly to analyze (default: both, separate views)",
    )
    parser.add_argument(
        "--mcpu", default="generic",
        help="CPU model for llvm-mca (default: generic; e.g. cortex-x2,"
             " neoverse-n1)",
    )
    parser.add_argument(
        "--all-stats", action="store_true",
        help="show all hardware statistics (dispatch, register, scheduler,"
             " retire)",
    )
    parser.add_argument(
        "--bottleneck", action="store_true",
        help="enable bottleneck analysis",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="output as JSON",
    )
    parser.add_argument("case", help="test case name (e.g. Maze, minirepro)")

    args = parser.parse_args()
    outdir = _outdir(args.outdir)

    cmd_mca(
        args.case,
        outdir,
        kind=args.kind,
        mcpu=args.mcpu,
        all_stats=args.all_stats,
        bottleneck=args.bottleneck,
        json_output=args.json_output,
    )


if __name__ == "__main__":
    main()
