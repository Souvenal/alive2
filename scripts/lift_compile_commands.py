#!/usr/bin/env python3
"""
Batch-lift AArch64 ELF .o files using arm-lifter, driven by compile_commands.json.

Prerequisite:
  The target project MUST be compiled with clang (CC=clang CXX=clang++).
  arm-lifter needs .ll (LLVM textual IR) for symbol resolution, which only
  clang can produce via -emit-llvm. This script validates the compiler on
  startup and aborts with a clear error if non-clang entries are found.

Workflow per entry:
  0. Compile .c -> .o (re-compile in VM to ensure fresh object)
  1. Emit .ll from source (original flags + -S -emit-llvm)
  2. arm-lifter <obj> --src-bc=<ll> -o <obj>.lifted.ll
  3. Recompile .ll -> .o using clang
  4. Link all .lifted.o (optional)

Usage:
  python3 lift_compile_commands.py /path/to/compile_commands.json \\
      --arm-lifter=./build/Release/arm-lifter \\
      --linker="clang -target aarch64-linux-gnu" \\
      --link-output=lifted_binary

  # Dry-run first (validates compiler, prints commands without running):
  python3 lift_compile_commands.py cc.json --dry-run

  # Override compiler (bypasses clang validation):
  python3 lift_compile_commands.py cc.json --cc="clang -target aarch64-linux-gnu"
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


def is_cc1_entry(entry):
    """Detect -cc1 internal compiler invocations (vs clang driver)."""
    args = get_args(entry)
    return "-cc1" in args


def extract_user_flags(entry):
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


def build_object_cmd(entry, obj_path):
    """Generate .o compile command: use clang (supports -emit-llvm)."""
    args = get_args(entry)
    is_cc1 = is_cc1_entry(entry)

    if is_cc1:
        args = ["clang", "-target", "aarch64-linux-gnu"] + extract_user_flags(entry)
    else:
        # Use clang instead of gcc -- -emit-llvm is clang-only
        args = ["clang"] + list(args)[1:]
        args = drop_flag(args, "-o", has_value=True)
        args = drop_flag(args, "-c")
        src_file = entry.get("file", "")
        if src_file:
            src_base = os.path.basename(src_file)
            args = [a for a in args if os.path.basename(a) != src_base]

    src_file = entry.get("file", "")
    args.extend(["-c", src_file, "-o", obj_path])
    return args


def build_bc_cmd(entry, bc_path):
    """Generate .ll command: original flags + -S -emit-llvm, output to bc_path.
    Emits textual IR to avoid BC version mismatch between VM clang and host LLVM."""
    args = get_args(entry)
    is_cc1 = is_cc1_entry(entry)

    if is_cc1:
        args = ["clang", "-target", "aarch64-linux-gnu"] + extract_user_flags(entry)
    else:
        args = ["clang"] + list(args)[1:]
        args = drop_flag(args, "-o", has_value=True)
        args = drop_flag(args, "-c")
        src_file = entry.get("file", "")
        if src_file:
            src_base = os.path.basename(src_file)
            args = [a for a in args if os.path.basename(a) != src_base]

    src_file = entry.get("file", "")
    args.extend(["-S", "-emit-llvm", src_file, "-o", bc_path])
    return args


def build_recompile_cmd(entry, ll_path, obj_path):
    """Recompile .ll → .o using clang (original flags, no -emit-llvm)."""
    args = get_args(entry)
    is_cc1 = is_cc1_entry(entry)

    if is_cc1:
        args = ["clang", "-target", "aarch64-linux-gnu"] + extract_user_flags(entry)
    else:
        args = ["clang"] + list(args)[1:]
        args = drop_flag(args, "-o", has_value=True)
        args = drop_flag(args, "-c")
        src_file = entry.get("file", "")
        if src_file:
            src_base = os.path.basename(src_file)
            args = [a for a in args if os.path.basename(a) != src_base]

    args.extend(["-c", ll_path, "-o", obj_path])
    return args


def validate_clang_compiler(db):
    """Validate that all compile_commands entries use clang.

    arm-lifter requires .ll (LLVM textual IR) files for symbol resolution,
    which can only be produced by clang (-emit-llvm). If the project was
    built with gcc or another compiler, bitcode generation will fail.

    Prints a clear error and returns False if any entry uses a non-clang
    compiler. Callers should abort early.
    """
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

    # Validate that all entries use clang (required for -emit-llvm).
    # Skip validation when --cc override is provided, since the override
    # replaces the compiler in all entries before processing.
    if not args.cc and not validate_clang_compiler(db):
        sys.exit(1)

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
        seen_sources = set()
        for i, entry in enumerate(db):
            directory = entry.get("directory", ".")
            arguments = get_args(entry)
            output = extract_output(arguments, entry)
            entry_label = entry.get("file", "?")
            if not output:
                print(f"[{i+1}/{len(db)}] SKIP {entry_label}: no output file")
                continue

            # Deduplicate: skip entries for source files already processed.
            # Compile_commands.json often has both clang-driver and clang -cc1
            # entries for the same source (e.g. entries 1-6 vs 7-12).
            src_file = entry.get("file", "")
            if src_file in seen_sources:
                print(f"[{i+1}/{len(db)}] SKIP {src_file}: already processed")
                continue
            if src_file:
                seen_sources.add(src_file)

            obj_path = resolve(directory, output)
            src_file = entry.get("file", "")

            # Detect linked executables (output is .exe or has linker sections).
            # The compile_commands may emit all TUs to the same linked output
            # (e.g. -o ./coremark.exe). Derive per-TU .o from source file stem.
            obj_basename = os.path.basename(obj_path)
            obj_is_linked = not obj_basename.endswith(".o") and not obj_basename.endswith(".obj")
            if obj_is_linked and src_file:
                obj_dir = os.path.dirname(
                    resolve(directory, output)
                ) if os.path.isabs(obj_path) else os.path.normpath(
                    os.path.join(directory, os.path.dirname(output))
                )
                stem = os.path.splitext(os.path.basename(src_file))[0]
            elif src_file:
                # For -cc1 entries with temp paths (/tmp/xxx.o), place .o
                # alongside source for shared-filesystem access (arm-lifter
                # runs natively on host, can't see VM /tmp).
                if os.path.dirname(src_file):
                    obj_dir = os.path.join(
                        os.path.dirname(resolve(directory, output)),
                        os.path.dirname(src_file),
                    )
                else:
                    obj_dir = os.path.dirname(resolve(directory, output))
                stem = os.path.splitext(os.path.basename(src_file))[0]
            else:
                obj_dir = os.path.dirname(obj_path)
                stem = os.path.splitext(obj_basename)[0]

            obj_path = os.path.join(obj_dir, stem + ".o")

            bc_path = os.path.join(obj_dir, stem + ".bc")
            ll_path = os.path.join(obj_dir, stem + ".lifted.ll")
            lifted_obj = os.path.join(obj_dir, stem + ".lifted.o")

            print(f"\n[{i+1}/{len(db)}] {obj_path}")

            # Step 0: Compile .c → .o in VM (object may be in VM /tmp)
            print("  [0/4] Compile object")
            oc_cmd = build_object_cmd(entry, obj_path)
            ok_oc, r_oc = run(oc_cmd, args.dry_run, cwd=directory, vm_prefix=vm_prefix)
            if not ok_oc:
                log_error(log_fh, entry_label, "object compilation", r_oc)
                continue

            # Step 1: source → .bc (in VM for cross-arch ELF target)
            print("  [1/4] Emit bitcode")
            bc_cmd = build_bc_cmd(entry, bc_path)
            ok_bc, r_bc = run(bc_cmd, args.dry_run, cwd=directory, vm_prefix=vm_prefix)
            if not ok_bc:
                log_error(log_fh, entry_label, "bitcode generation", r_bc)
                continue

            # Step 2: arm-lifter .o + .bc → .ll (native only)
            print("  [2/4] arm-lifter")
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
            print("  [3/4] Recompile .ll → .o")
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
