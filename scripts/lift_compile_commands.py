#!/usr/bin/env python3
"""
Batch-lift AArch64 ELF .o files using arm-lifter, driven by compile_commands.json.

Workflow per entry:
  1. Emit .bc from source  (original flags + -emit-llvm)
  2. arm-lifter <obj> --src-bc=<bc> -o <obj>.lifted.ll
  3. Recompile .ll → .o using original flags
  4. Link all .lifted.o (optional)

Usage:
  python3 lift_compile_commands.py /path/to/compile_commands.json \\
      --arm-lifter=./build/Release/arm-lifter \\
      --linker="zig cc -target aarch64-linux-gnu" \\
      --link-output=lifted_binary

  # Dry-run first:
  python3 lift_compile_commands.py cc.json --dry-run
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


IS_DARWIN = sys.platform == "darwin"


def detect_vm_prefix() -> list[str]:
    if IS_DARWIN:
        return ["lima", "--"]
    return []


def parse_args():
    p = argparse.ArgumentParser(
        description="Batch-lift ELF .o files via arm-lifter from compile_commands.json"
    )
    p.add_argument("compile_commands", help="Path to compile_commands.json")
    p.add_argument(
        "--arm-lifter",
        default="build/Release/arm-lifter",
        help="Path to arm-lifter binary (default: build/Release/arm-lifter)",
    )
    p.add_argument(
        "--linker",
        default="clang -target aarch64-linux-gnu",
        help="Linker command, e.g. 'clang -target aarch64-linux-gnu' (default: clang -target aarch64-linux-gnu)",
    )
    p.add_argument(
        "--vm-prefix",
        default=None,
        help="Prefix for VM execution (e.g., 'lima --'). Auto-detected on darwin.",
    )
    p.add_argument(
        "--link-output",
        default="a.out",
        help="Output binary for link step (default: a.out)",
    )
    p.add_argument("--dry-run", action="store_true", help="Print commands, don't run")
    p.add_argument("--keep-bc", action="store_true", help="Keep intermediate .bc files")
    p.add_argument(
        "--skip-link",
        action="store_true",
        help="Skip final link step",
    )
    p.add_argument(
        "--log",
        default="lift_errors.log",
        help="Error log file path (default: lift_errors.log)",
    )
    p.add_argument(
        "--cc",
        default=None,
        help="Override compiler for all steps: bc generation + .ll→.o recompilation (default: use compiler from compile_commands)",
    )
    return p.parse_args()


def load_db(path):
    with open(path) as f:
        return json.load(f)


def resolve(dir_, p):
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(dir_, p))


def get_args(entry):
    """Return arguments list from entry (handles both 'arguments' and 'command')."""
    if "arguments" in entry:
        return list(entry["arguments"])
    if "command" in entry:
        return shlex.split(entry["command"])
    raise ValueError(f"Entry has no 'arguments' or 'command': {entry}")


def extract_output(arguments, entry):
    """Extract output file path from -o flag or entry['output']."""
    if "output" in entry:
        return entry["output"]
    for i, a in enumerate(arguments):
        if a == "-o" and i + 1 < len(arguments):
            return arguments[i + 1]
        if a.startswith("-o") and len(a) > 2:
            return a[2:]
    return None


def drop_flag(args, flag, has_value=False):
    """Remove -flag [value] from args list. Returns new list."""
    out = []
    i = 0
    while i < len(args):
        if args[i] == flag:
            i += 1 + has_value
        elif args[i].startswith(flag) and not has_value:
            i += 1
        else:
            out.append(args[i])
            i += 1
    return out


def build_bc_cmd(entry, bc_path):
    """Generate .bc command: original flags + -emit-llvm, output to bc_path."""
    args = get_args(entry)
    compiler = args[0]

    # Drop -o <orig>
    args = drop_flag(args, "-o", has_value=True)
    # Drop -c (we add -emit-llvm -c)
    args = drop_flag(args, "-c")
    # Drop the source file (last positional) — match by basename
    src_file = entry.get("file", "")
    if src_file:
        src_base = os.path.basename(src_file)
        args = [a for a in args if os.path.basename(a) != src_base]

    args.extend(["-emit-llvm", "-c", src_file, "-o", bc_path])
    return args


def build_recompile_cmd(entry, ll_path, obj_path):
    """Recompile .ll → .o using original flags (no -emit-llvm)."""
    args = get_args(entry)

    # Drop -o <orig> and -c (we re-add them cleanly)
    args = drop_flag(args, "-o", has_value=True)
    args = drop_flag(args, "-c")
    # Drop source file reference — match by basename
    src_file = entry.get("file", "")
    if src_file:
        src_base = os.path.basename(src_file)
        args = [a for a in args if os.path.basename(a) != src_base]

    args.extend(["-c", ll_path, "-o", obj_path])
    return args


def run(cmd, dry_run, cwd=None, vm_prefix=None):
    if vm_prefix:
        cmd = list(vm_prefix) + list(cmd)
    print(f"  {'[DRY-RUN]' if dry_run else '[RUN]'} {' '.join(shlex.quote(str(x)) for x in cmd)}")
    if dry_run:
        return True, None
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode == 0, r


def log_error(fh, entry_label, step, result):
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


def main():
    args = parse_args()
    db = load_db(args.compile_commands)
    print(f"Loaded {len(db)} entries from {args.compile_commands}")

    if args.cc:
        # Override compiler in all entries
        cc_parts = shlex.split(args.cc)
        for entry in db:
            orig = get_args(entry)
            # Replace compiler (orig[0]) with new compiler parts
            orig = cc_parts + orig[1:]
            if "arguments" in entry:
                entry["arguments"] = orig
            else:
                entry["command"] = " ".join(shlex.quote(a) for a in orig)

    lifted_objs = []
    ok = 0
    log_path = args.log
    vm_prefix = args.vm_prefix if args.vm_prefix is not None else detect_vm_prefix()

    with open(log_path, "w") as log_fh:
        for i, entry in enumerate(db):
            directory = entry.get("directory", ".")
            arguments = get_args(entry)
            output = extract_output(arguments, entry)
            entry_label = entry.get("file", "?")
            if not output:
                print(f"[{i+1}/{len(db)}] SKIP {entry_label}: no output file")
                continue

            obj_path = resolve(directory, output)
            src_file = entry.get("file", "")
            obj_dir = os.path.dirname(obj_path)
            stem = os.path.splitext(os.path.basename(obj_path))[0]

            bc_path = os.path.join(obj_dir, stem + ".bc")
            ll_path = os.path.join(obj_dir, stem + ".lifted.ll")
            lifted_obj = os.path.join(obj_dir, stem + ".lifted.o")

            print(f"\n[{i+1}/{len(db)}] {obj_path}")

            # Step 1: source → .bc (in VM for cross-arch ELF target)
            print("  [1/3] Emit bitcode")
            bc_cmd = build_bc_cmd(entry, bc_path)
            ok_bc, r_bc = run(bc_cmd, args.dry_run, cwd=directory, vm_prefix=vm_prefix)
            if not ok_bc:
                log_error(log_fh, entry_label, "bitcode generation", r_bc)
                continue

            # Step 2: arm-lifter .o + .bc → .ll (native only)
            print("  [2/3] arm-lifter")
            lifter_cmd = [args.arm_lifter, obj_path, f"--src-bc={bc_path}", "-o", ll_path]
            ok_lift, r_lift = run(lifter_cmd, args.dry_run)
            if not ok_lift:
                log_error(log_fh, entry_label, "arm-lifter", r_lift)
                continue

            if not args.keep_bc and not args.dry_run:
                try:
                    os.unlink(bc_path)
                except OSError:
                    pass

            # Step 3: .ll → .o recompile (in VM for ELF output)
            print("  [3/3] Recompile .ll → .o")
            rc_cmd = build_recompile_cmd(entry, ll_path, lifted_obj)
            ok_rc, r_rc = run(rc_cmd, args.dry_run, cwd=directory, vm_prefix=vm_prefix)
            if not ok_rc:
                log_error(log_fh, entry_label, "recompilation", r_rc)
                continue

            lifted_objs.append(lifted_obj)
            ok += 1
            print(f"  OK → {lifted_obj}")

    # Link (in VM for ELF output)
    if lifted_objs and not args.skip_link:
        print(f"\n--- Link {len(lifted_objs)} lifted objects ---")
        linker_parts = shlex.split(args.linker)
        link_cmd = linker_parts + lifted_objs + ["-o", args.link_output]
        ok_link, r_link = run(link_cmd, args.dry_run, vm_prefix=vm_prefix)
        if not ok_link and not args.dry_run:
            with open(log_path, "a") as f:
                log_error(f, "(link)", "linking", r_link)
            print("  FAIL: linking (see log)")
    elif lifted_objs and not args.skip_link:
        print(f"\nSkipping link. {len(lifted_objs)} lifted objects ready.")

    print(f"\nDone. {ok}/{len(db)} succeeded.")


if __name__ == "__main__":
    main()
