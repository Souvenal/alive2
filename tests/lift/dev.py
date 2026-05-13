#!/usr/bin/env python3
"""Ad-hoc dev tool for lifting cases and inspecting results.

Usage:
    uv run python tests/lift/dev.py lift <case>              .c → output/<case>.lifted.ll
    uv run python tests/lift/dev.py full <case>              .c → lift + x86_64 bin + arm64 ref
    uv run python tests/lift/dev.py run <case> <kind>        run existing binary
      kind: arm64 | lifted_x64
    uv run python tests/lift/dev.py clean                    rm -r output/
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from conftest import (
    ARM_LIFTER,
    CASES_DIR,
    compile_arm64_binary,
    compile_bc_and_o,
    get_stdin,
    recompile_x86_64,
    run_aarch64_linux_binary,
    run_x86_64_linux_binary,
)


def _require_arm_lifter() -> None:
    """Exit with a clear message if arm-lifter hasn't been built."""
    if not Path(ARM_LIFTER).exists():
        print(
            f"ERROR: arm-lifter binary not found at: {ARM_LIFTER}\n"
            f"Build it first:\n"
            f"    cd {Path(__file__).resolve().parent.parent.parent} && ./build.sh\n"
            f"Or set ARM_LIFTER env var to the built binary.",
            file=sys.stderr, flush=True,
        )
        sys.exit(1)


def _check_case(name: str) -> Path:
    src = CASES_DIR / f"{name}.c"
    if not src.exists():
        sys.exit(f"case '{name}' not found at {src}")
    return src


def _outdir(base: Path | None) -> Path:
    d = base or Path.cwd() / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lift(o: Path, bc: Path, lifted_ll: Path, log: Path) -> int:
    r = subprocess.run(
        [ARM_LIFTER, str(o), "--src-bc", str(bc), "-o", str(lifted_ll)],
        capture_output=True, text=True,
    )
    log.write_text(r.stdout + r.stderr)
    return r.returncode


def _clean_case(name: str, outdir: Path) -> None:
    for f in outdir.glob(f"{name}*"):
        f.unlink()


def cmd_lift(name: str, outdir: Path) -> None:
    _require_arm_lifter()
    src = _check_case(name)
    _clean_case(name, outdir)
    bc, o = compile_bc_and_o(src, outdir)
    lifted_ll = outdir / f"{name}.lifted.ll"
    log = outdir / f"{name}.lift.log"

    rc = _lift(o, bc, lifted_ll, log)
    if rc != 0:
        print(f"arm-lifter failed (exit {rc})", file=sys.stderr)
        sys.exit(rc)

    print(f"lift      → {lifted_ll}")
    print(f"log       → {log}")


def cmd_full(name: str, outdir: Path) -> None:
    _require_arm_lifter()
    src = _check_case(name)
    _clean_case(name, outdir)
    bc, o = compile_bc_and_o(src, outdir)
    lifted_ll = outdir / f"{name}.lifted.ll"
    log = outdir / f"{name}.lift.log"

    rc = _lift(o, bc, lifted_ll, log)
    if rc != 0:
        print(f"arm-lifter failed (exit {rc}), see {log}", file=sys.stderr)
        sys.exit(rc)

    sub = recompile_x86_64(lifted_ll, outdir)
    ref = compile_arm64_binary(src, outdir)
    print(f"x86_64    → {sub}")
    print(f"arm64 ref → {ref}")
    print(f"lift      → {lifted_ll}")
    print(f"log       → {log}")


def cmd_run(name: str, kind: str, outdir: Path) -> None:
    _check_case(name)

    if kind == "arm64":
        binary = outdir / f"{name}_arm64"
    elif kind == "lifted_x64":
        binary = outdir / f"{name}.lifted_x86_64"

    # Auto-build if binary doesn't exist yet
    if not binary.exists():
        print(f"{binary.name} not found, building...")
        cmd_full(name, outdir)
        print()

    stdin_text = get_stdin(name)

    if kind == "arm64":
        r = run_aarch64_linux_binary(binary, stdin_text=stdin_text)
    else:
        r = run_x86_64_linux_binary(binary, stdin_text=stdin_text)

    print(r.stdout, end="")
    sys.stderr.write(r.stderr)
    print(f"exit code: {r.returncode}")


def cmd_clean(outdir: Path) -> None:
    shutil.rmtree(outdir)
    print(f"rm -r {outdir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="arm-lifter dev tool")
    parser.add_argument(
        "--outdir", "-o", type=Path, default=None,
        help="output directory (default: output/ under cwd)",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lift", help="Compile + lift, output .lifted.ll")
    p.add_argument("case")

    p = sub.add_parser("full", help="Lift + recompile to x86_64")
    p.add_argument("case")

    p = sub.add_parser("run", help="Run an existing binary")
    p.add_argument("case")
    p.add_argument("kind", choices=["arm64", "lifted_x64"])

    sub.add_parser("clean", help="Remove output directory")

    args = parser.parse_args()
    outdir = _outdir(args.outdir)

    if args.cmd == "lift":
        cmd_lift(args.case, outdir)
    elif args.cmd == "full":
        cmd_full(args.case, outdir)
    elif args.cmd == "run":
        cmd_run(args.case, args.kind, outdir)
    elif args.cmd == "clean":
        cmd_clean(outdir)


if __name__ == "__main__":
    main()
