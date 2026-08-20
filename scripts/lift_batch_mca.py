#!/usr/bin/env python3
"""Batch-lift and analyze AArch64 ELF .o files using arm-lifter, driven by compile_commands.json.

This script integrates the functionality of lift_compile_commands.py and lift_fragment_mca.py
to provide a complete batch processing workflow with provenance fragment MCA analysis.

Prerequisite:
  The target project MUST be compiled with clang (CC=clang CXX=clang++).
  arm-lifter needs .ll (LLVM textual IR) for symbol resolution, which only
  clang can produce via -emit-llvm. This script validates the compiler on
  startup and aborts with a clear error if non-clang entries are found.

Workflow per entry:
  0. Compile .c -> .o (re-compile to ensure fresh object)
  1. Emit .ll from source (original flags + -S -emit-llvm)
  2. arm-lifter <obj> --src-bc=<ll> -o <obj>.lifted.ll
  3. Recompile .ll -> .o using clang
  4. Run provenance fragment MCA analysis
  5. Generate JSON and optional Markdown reports

Usage:
  python3 scripts/lift_batch_mca.py /path/to/compile_commands.json \
      --arm-lifter=./build/Release/arm-lifter \
      --output-dir=./batch-output

  # Dry-run first (validates compiler, prints commands without running):
  python3 scripts/lift_batch_mca.py cc.json --dry-run

  # Generate Markdown reports:
  python3 scripts/lift_batch_mca.py cc.json --write-md

  # Analyze specific function only:
  python3 scripts/lift_batch_mca.py cc.json --function main
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tests" / "lift"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# Import from lift_fragment_mca.py
from lift_fragment_mca import run_pipeline as _run_pipeline
from lift_fragment_mca import _check_source, _output_dirs, _require_vm_shared_path

# Import from conftest.py
import conftest as _conftest
from conftest import (
    ARM_LIFTER,
    CFLAGS,
    CLANG,
    LLC,
    LLVM_DIS,
    _to_vm_path,
    compile_arm64_binary,
    compile_bc_and_o,
    recompile_arm64,
    recompile_x86_64,
    run_command,
    run_in_vm,
)

# Import lift_fragment_mca module reference (for CFLAGS monkey-patching)
import lift_fragment_mca as _lfm


# Platform detection
IS_DARWIN = sys.platform == "darwin"


def detect_vm_prefix() -> list[str]:
    if IS_DARWIN:
        return ["lima", "--"]
    return []


def parse_args():
    p = argparse.ArgumentParser(
        description="Batch-lift and analyze ELF .o files via arm-lifter from compile_commands.json"
    )
    p.add_argument("compile_commands", help="Path to compile_commands.json")
    p.add_argument(
        "--arm-lifter",
        default=os.environ.get("ARM_LIFTER", str(ARM_LIFTER)),
        help="Path to arm-lifter binary (default: build/Release/arm-lifter)",
    )
    p.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=Path.cwd() / "output",
        help="Output directory (default: ./output)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without running them",
    )
    p.add_argument(
        "--write-md",
        action="store_true",
        help="Also write Markdown reports (default: JSON only)",
    )
    p.add_argument(
        "--function",
        help="Analyze only this function (default: all mapped functions)",
    )
    p.add_argument(
        "--mca-iterations",
        type=int,
        default=100,
        help="Number of MCA iterations (default: 100)",
    )
    p.add_argument(
        "--min-source-instructions",
        type=int,
        default=2,
        help="Minimum source instructions per fragment (default: 2)",
    )
    p.add_argument(
        "--min-target-instructions",
        type=int,
        default=2,
        help="Minimum target instructions per fragment (default: 2)",
    )
    p.add_argument(
        "--mcpu",
        default="generic",
        help="MCA CPU model (default: generic)",
    )
    p.add_argument(
        "--mattr",
        default="",
        help="MCA CPU attributes (default: empty)",
    )
    p.add_argument(
        "--log",
        default="batch_errors.log",
        help="Error log file path (default: batch_errors.log)",
    )
    p.add_argument(
        "--cc",
        default=None,
        help="Override compiler for all steps (default: use compiler from compile_commands)",
    )
    p.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="Keep intermediate build files (default: clean up)",
    )
    return p.parse_args()


def load_db(path: str) -> List[Dict[str, Any]]:
    with open(path) as f:
        return json.load(f)


def get_args(entry: Dict[str, Any]) -> List[str]:
    """Return arguments list from entry (handles both 'arguments' and 'command')."""
    if "arguments" in entry:
        return list(entry["arguments"])
    if "command" in entry:
        return shlex.split(entry["command"])
    raise ValueError(f"Entry has no 'arguments' or 'command': {entry}")


def is_cc1_entry(entry: Dict[str, Any]) -> bool:
    """Detect -cc1 internal compiler invocations (vs clang driver)."""
    args = get_args(entry)
    return "-cc1" in args


def extract_user_flags(entry: Dict[str, Any]) -> List[str]:
    """Extract user-facing flags from a -cc1 entry: -I, -D, -O only."""
    args = get_args(entry)
    out = []
    it = iter(args)
    for a in it:
        if a in ("-I", "-D", "-O"):
            out.append(a)
            try:
                out.append(next(it))
            except StopIteration:
                pass
        elif a.startswith("-I") or a.startswith("-D") or a.startswith("-O"):
            out.append(a)
    return out


def extract_entry_cflags(entry: Dict[str, Any]) -> List[str]:
    """Extract compilation flags from a compile_commands entry.

    Returns flags that affect compilation output (include paths, defines,
    optimization, warnings, standards, etc.) but strips the compiler name,
    source file, -o, -c, and -emit-llvm which are handled separately.
    """
    args = get_args(entry)
    if is_cc1_entry(entry):
        return extract_user_flags(entry)

    # Driver entry: skip compiler (args[0]) and collect everything before
    # the source file, excluding -c, -o (with value), and the source/output.
    src_file = entry.get("file", "")
    src_basename = os.path.basename(src_file) if src_file else ""
    output = entry.get("output")
    if not output:
        for i, a in enumerate(args):
            if a == "-o" and i + 1 < len(args):
                output = args[i + 1]
                break
            if a.startswith("-o") and len(a) > 2:
                output = a[2:]
                break
    output_basename = os.path.basename(output) if output else ""

    out: List[str] = []
    i = 1  # skip compiler
    while i < len(args):
        a = args[i]
        if a == "-c":
            i += 1
            continue
        if a == "-o":
            i += 2  # skip -o and its value
            continue
        if a.startswith("-o") and len(a) > 2:
            i += 1
            continue
        if a == "-emit-llvm" or a == "-S":
            i += 1
            continue
        # Skip the source file argument
        if src_basename and os.path.basename(a) == src_basename:
            i += 1
            continue
        # Skip the output file argument
        if output_basename and os.path.basename(a) == output_basename:
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def validate_clang_compiler(db: List[Dict[str, Any]]) -> bool:
    """Validate that all compile_commands entries use clang."""
    non_clang = []
    for i, entry in enumerate(db):
        args = get_args(entry)
        if not args:
            continue
        compiler = os.path.basename(args[0])
        if "clang" not in compiler:
            src = entry.get("file", "?")
            non_clang.append((i, compiler, src))

    if non_clang:
        print("ERROR: compile_commands.json contains non-clang compiler entries.",
              file=sys.stderr)
        print("arm-lifter requires clang to generate .ll files (-emit-llvm).",
              file=sys.stderr)
        print(f"Found {len(non_clang)} non-clang entries:", file=sys.stderr)
        for idx, compiler, src in non_clang[:10]:
            print(f"  [{idx}] {compiler}  (file: {src})", file=sys.stderr)
        if len(non_clang) > 10:
            print(f"  ... and {len(non_clang) - 10} more", file=sys.stderr)
        print("\nFix: rebuild the target project with CC=clang CXX=clang++",
              file=sys.stderr)
        print("  e.g.: cmake -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ ..",
              file=sys.stderr)
        print("  or:   CC=clang CXX=clang++ make", file=sys.stderr)
        return False
    return True


def run(cmd: List[str], dry_run: bool, cwd: Optional[str] = None, vm_prefix: Optional[List[str]] = None) -> Tuple[bool, Optional[subprocess.CompletedProcess]]:
    if vm_prefix:
        cmd = list(vm_prefix) + list(cmd)
    print(f"  {'[DRY-RUN]' if dry_run else '[RUN]'} {' '.join(shlex.quote(str(x)) for x in cmd)}")
    if dry_run:
        return True, None
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode == 0, r


def log_error(fh, entry_label: str, step: str, result: Optional[subprocess.CompletedProcess]):
    """Write error details to log file."""
    print(f"  FAIL: {step}", file=fh)
    print(f"    entry: {entry_label}", file=fh)
    if result and result.stderr:
        for line in result.stderr.strip().split("\n"):
            print(f"    stderr: {line}", file=fh)
    if result and result.stdout:
        for line in result.stdout.strip().split("\n"):
            print(f"    stdout: {line}", file=fh)
    print(file=fh)
    fh.flush()


def process_entry(
    entry: Dict[str, Any],
    args: argparse.Namespace,
    output_dir: Path,
    log_fh,
    entry_index: int,
    total_entries: int,
) -> Optional[Tuple[str, Path, Path, Path]]:
    """Process a single compile_commands entry.
    
    Returns:
        Tuple of (source_stem, build_dir, report_dir, source_path) on success
        None on failure
    """
    directory = entry.get("directory", ".")
    arguments = get_args(entry)
    
    # Extract output file path
    output = None
    if "output" in entry:
        output = entry["output"]
    else:
        for i, a in enumerate(arguments):
            if a == "-o" and i + 1 < len(arguments):
                output = arguments[i + 1]
                break
            if a.startswith("-o") and len(a) > 2:
                output = a[2:]
                break
    
    entry_label = entry.get("file", "?")
    
    if not output:
        print(f"[{entry_index+1}/{total_entries}] SKIP {entry_label}: no output file")
        return None
    
    src_file = entry.get("file", "")
    if not src_file:
        print(f"[{entry_index+1}/{total_entries}] SKIP {entry_label}: no source file")
        return None
    
    # Resolve source path
    source_path = Path(directory) / src_file if not os.path.isabs(src_file) else Path(src_file)
    if not source_path.exists():
        print(f"[{entry_index+1}/{total_entries}] SKIP {entry_label}: source file not found")
        log_error(log_fh, entry_label, "source file not found", None)
        return None
    
    # Create output subdirectory for this source file
    source_stem = source_path.stem
    source_output_dir = output_dir / source_stem
    build_dir = source_output_dir / "build"
    report_dir = source_output_dir / "report"
    
    # Clean existing outputs
    if source_output_dir.exists():
        import shutil
        shutil.rmtree(source_output_dir)
    
    build_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"\n[{entry_index+1}/{total_entries}] Processing {source_path}")
    
    # Check if arm-lifter exists
    arm_lifter_path = Path(args.arm_lifter)
    if not arm_lifter_path.exists():
        print(f"  ERROR: arm-lifter not found at {arm_lifter_path}")
        log_error(log_fh, entry_label, "arm-lifter not found", None)
        return None
    
    # Check if we need to validate clang compiler
    if not args.cc and not validate_clang_compiler([entry]):
        log_error(log_fh, entry_label, "non-clang compiler", None)
        return None
    
    # Override compiler if specified
    if args.cc:
        cc_parts = shlex.split(args.cc)
        orig = get_args(entry)
        orig = cc_parts + orig[1:]
        if "arguments" in entry:
            entry["arguments"] = orig
        else:
            entry["command"] = " ".join(shlex.quote(a) for a in orig)
    
    try:
        # Extract per-entry compiler flags (-I, -D, -O, etc.) and inject
        # them into the CFLAGS used by _run_pipeline().  Without this,
        # project-specific include paths and defines are lost.
        entry_cflags = extract_entry_cflags(entry)
        merged_cflags = CFLAGS + entry_cflags

        saved_conftest_cflags = _conftest.CFLAGS
        saved_lfm_cflags = _lfm.CFLAGS
        try:
            _conftest.CFLAGS = merged_cflags
            _lfm.CFLAGS = merged_cflags

            print(f"  CFLAGS: {' '.join(merged_cflags)}")

            reports = _run_pipeline(
                source=source_path,
                output_dir=source_output_dir,
                arm_lifter=arm_lifter_path,
                machine_cfg_dump=None,
                mcpu=args.mcpu,
                mattr=args.mattr,
                iterations=args.mca_iterations,
                min_source_instructions=args.min_source_instructions,
                min_target_instructions=args.min_target_instructions,
                function=args.function,
                write_md=args.write_md,
            )
        finally:
            _conftest.CFLAGS = saved_conftest_cflags
            _lfm.CFLAGS = saved_lfm_cflags

        print(f"  OK → {source_output_dir}")
        return source_stem, build_dir, report_dir, source_path
        
    except Exception as e:
        error_msg = str(e)
        print(f"  FAIL: {error_msg}")
        # Show truncated stderr for compilation errors
        if "failed" in error_msg.lower():
            lines = error_msg.split("\n")
            for line in lines[:10]:
                print(f"    {line}")
            if len(lines) > 10:
                print(f"    ... ({len(lines) - 10} more lines)")
        log_error(log_fh, entry_label, "processing failed", None)
        return None


def generate_batch_summary(
    successful: List[Tuple[str, Path, Path, Path]],
    failed: List[Tuple[str, str]],
    output_dir: Path,
) -> Path:
    """Generate batch summary report."""
    summary_path = output_dir / "batch-summary.txt"
    
    lines = [
        "Batch Lift and MCA Analysis Summary",
        "===================================",
        "",
        f"Total entries processed: {len(successful) + len(failed)}",
        f"Successful: {len(successful)}",
        f"Failed: {len(failed)}",
        "",
    ]
    
    if successful:
        lines.append("Successful entries:")
        for source_stem, build_dir, report_dir, source_path in successful:
            lines.append(f"  - {source_stem} ({source_path})")
        lines.append("")
    
    if failed:
        lines.append("Failed entries:")
        for source_stem, error in failed:
            lines.append(f"  - {source_stem}: {error}")
        lines.append("")
    
    # Collect all improved fragments
    improved_fragments = []
    for source_stem, build_dir, report_dir, source_path in successful:
        improved_summary_path = report_dir / f"{source_stem}.improved-summary.txt"
        if improved_summary_path.exists():
            summary_content = improved_summary_path.read_text(encoding="utf-8")
            if "No improved fragments found." not in summary_content:
                improved_fragments.append((source_stem, summary_content))
    
    if improved_fragments:
        lines.append("Improved fragments found:")
        for source_stem, summary in improved_fragments:
            lines.append(f"\n--- {source_stem} ---")
            lines.append(summary)
    else:
        lines.append("No improved fragments found in any entry.")
    
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path


def main():
    args = parse_args()
    
    # Validate arguments
    if args.mca_iterations <= 0:
        print("ERROR: --mca-iterations must be positive", file=sys.stderr)
        sys.exit(1)
    if args.min_source_instructions <= 0:
        print("ERROR: --min-source-instructions must be positive", file=sys.stderr)
        sys.exit(1)
    if args.min_target_instructions <= 0:
        print("ERROR: --min-target-instructions must be positive", file=sys.stderr)
        sys.exit(1)
    
    # Load compile_commands.json
    try:
        db = load_db(args.compile_commands)
    except Exception as e:
        print(f"ERROR: Failed to load compile_commands.json: {e}", file=sys.stderr)
        sys.exit(1)
    
    print(f"Loaded {len(db)} entries from {args.compile_commands}")
    
    # Create output directory
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Check if we need to validate clang compiler (skip if --cc is provided)
    if not args.cc and not validate_clang_compiler(db):
        sys.exit(1)
    
    # Override compiler in all entries if specified
    if args.cc:
        cc_parts = shlex.split(args.cc)
        for entry in db:
            orig = get_args(entry)
            orig = cc_parts + orig[1:]
            if "arguments" in entry:
                entry["arguments"] = orig
            else:
                entry["command"] = " ".join(shlex.quote(a) for a in orig)
    
    # Deduplicate entries by source file
    seen_sources = set()
    unique_entries = []
    for entry in db:
        src_file = entry.get("file", "")
        if src_file and src_file not in seen_sources:
            seen_sources.add(src_file)
            unique_entries.append(entry)
    
    print(f"Found {len(unique_entries)} unique source files to process")
    
    # Process entries
    successful = []
    failed = []
    log_path = output_dir / args.log
    
    with open(log_path, "w") as log_fh:
        for i, entry in enumerate(unique_entries):
            try:
                result = process_entry(entry, args, output_dir, log_fh, i, len(unique_entries))
                if result:
                    successful.append(result)
                else:
                    src_file = entry.get("file", "?")
                    failed.append((src_file, "processing failed"))
            except Exception as e:
                src_file = entry.get("file", "?")
                print(f"[{i+1}/{len(unique_entries)}] UNEXPECTED ERROR: {e}")
                log_error(log_fh, src_file, "unexpected error", None)
                failed.append((src_file, str(e)))
    
    # Generate summary
    summary_path = generate_batch_summary(successful, failed, output_dir)
    
    print(f"\n{'='*60}")
    print(f"BATCH PROCESSING COMPLETE")
    print(f"{'='*60}")
    print(f"Total: {len(unique_entries)} entries")
    print(f"Successful: {len(successful)}")
    print(f"Failed: {len(failed)}")
    print(f"\nOutput directory: {output_dir}")
    print(f"Summary: {summary_path}")
    print(f"Error log: {log_path}")
    
    if failed:
        print(f"\nFailed entries:")
        for source_stem, error in failed:
            print(f"  - {source_stem}: {error}")
    
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())