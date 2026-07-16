#!/usr/bin/env python3
"""ARM64 to target instruction mapping via an arm-lifter instruction map.

Usage:
  python asm_diff.py <asm-map.json> <lifted_x86_64> [--fn <name>] [--all]
"""

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict


def parse_instruction_map(
    map_path: str,
) -> tuple[str, dict[str, dict[int, dict[str, object]]]]:
    """Read schema-v1 instruction map data emitted by arm-lifter."""
    try:
        with open(map_path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as error:
        sys.exit(f"Cannot read instruction map '{map_path}': {error}")
    except json.JSONDecodeError as error:
        sys.exit(f"Invalid instruction map JSON '{map_path}': {error}")

    if not isinstance(data, dict) or data.get("version") != 1:
        sys.exit(f"Unsupported instruction map schema in '{map_path}'")

    debug_file = data.get("debug_file")
    functions = data.get("functions")
    if not isinstance(debug_file, str) or not isinstance(functions, list):
        sys.exit(f"Malformed instruction map '{map_path}'")

    arm: dict[str, dict[int, dict[str, object]]] = {}
    for function in functions:
        if not isinstance(function, dict):
            sys.exit(f"Malformed function record in '{map_path}'")
        name = function.get("name")
        instructions = function.get("instructions")
        if not isinstance(name, str) or not isinstance(instructions, list):
            sys.exit(f"Malformed function record in '{map_path}'")
        if name in arm:
            sys.exit(f"Duplicate function '{name}' in '{map_path}'")
        by_id: dict[int, dict[str, object]] = {}
        dwarf_lines: set[int] = set()
        for instruction in instructions:
            if not isinstance(instruction, dict):
                sys.exit(f"Malformed instruction record in '{map_path}'")
            arm_inst_id = instruction.get("arm_inst_id")
            dwarf_line = instruction.get("dwarf_line")
            mc_block = instruction.get("mc_block")
            opcode = instruction.get("opcode")
            asm = instruction.get("asm")
            if (type(arm_inst_id) is not int
                    or type(dwarf_line) is not int
                    or not isinstance(mc_block, str)
                    or not isinstance(opcode, str)
                    or not isinstance(asm, str)):
                sys.exit(f"Malformed instruction record in '{map_path}'")
            if arm_inst_id in by_id:
                sys.exit(
                    f"Duplicate arm_inst_id {arm_inst_id} for '{name}' "
                    f"in '{map_path}'"
                )
            if dwarf_line in dwarf_lines:
                sys.exit(
                    f"Duplicate dwarf_line {dwarf_line} for '{name}' "
                    f"in '{map_path}'"
                )
            by_id[arm_inst_id] = instruction
            dwarf_lines.add(dwarf_line)
        arm[name] = by_id

    return debug_file, arm


def detect_arch(bin_path: str) -> str:
    """Detect target architecture from binary header."""
    result = subprocess.run(
        ["llvm-objdump", "-f", bin_path], capture_output=True, text=True
    )
    m = re.search(r"file format\s+(\S+)", result.stdout)
    return m.group(1) if m else "unknown"


def parse_objdump(
    bin_path: str, debug_file: str
) -> dict[str, dict[int, list[str]]]:
    """Map synthetic DWARF lines in the target binary to target instructions."""
    result = subprocess.run(
        ["llvm-objdump", "-d", "--line-numbers", bin_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"llvm-objdump failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    target: dict[str, dict[int, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    cur_fn = None
    cur_line = None
    debug_line = re.compile(
        rf";\s+(?:.*\/)?{re.escape(debug_file)}:(\d+)"
    )

    for line in result.stdout.splitlines():
        m = re.match(r"([0-9a-f]+) <([^>]+)>:", line)
        if m:
            cur_fn = m.group(2)
            cur_line = None
            continue

        m = debug_line.search(line)
        if m:
            cur_line = int(m.group(1))
            continue

        if cur_line is not None and cur_fn is not None:
            m = re.match(r"\s*([0-9a-f]+:\s+.*)", line)
            if m:
                target[cur_fn][cur_line].append(m.group(1).strip())
    return target


def display(
    arm: dict[str, dict[int, dict[str, object]]],
    target: dict[str, dict[int, list[str]]],
    fn_name: str,
    show_all: bool,
    arch: str,
) -> None:
    """Print ARM to target correspondence for one function."""
    arm_insts = arm.get(fn_name, {})
    target_lines = target.get(fn_name, {})

    if not arm_insts:
        print(f"  (no ARM instructions in instruction map for '{fn_name}')")
        return

    print(f"\n{'-' * 70}")
    print(f"  {fn_name}  (to {arch})")
    print(f"{'-' * 70}")

    for arm_inst_id in sorted(arm_insts):
        instruction = arm_insts[arm_inst_id]
        asm = instruction["asm"]
        dwarf_line = instruction["dwarf_line"]
        assert isinstance(asm, str)
        assert isinstance(dwarf_line, int)
        target_insts = target_lines.get(dwarf_line, [])

        is_nop = (not asm or asm.startswith("b\t#") or asm.startswith("b\t."))
        if is_nop and not show_all:
            continue

        if target_insts:
            print(f"ARM #{arm_inst_id:<3} {asm}")
            for target_inst in target_insts:
                print(f"        {target_inst}")
        elif is_nop:
            print(f"ARM #{arm_inst_id:<3} {asm}  (control flow / NOP)")
        else:
            print(f"ARM #{arm_inst_id:<3} {asm}")
            print("        [optimized away]")

    print(f"{'-' * 70}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Map ARM instructions to target instructions using DWARF"
    )
    parser.add_argument("asm_map", help="arm-lifter instruction map JSON")
    parser.add_argument("binary", help="recompiled target binary")
    parser.add_argument("--fn", dest="fn_filter", help="show one function")
    parser.add_argument(
        "--all", action="store_true", help="show control-flow and NOP records"
    )
    args = parser.parse_args()

    debug_file, arm = parse_instruction_map(args.asm_map)
    target = parse_objdump(args.binary, debug_file)
    arch = detect_arch(args.binary)

    if args.fn_filter:
        if args.fn_filter not in arm:
            print(f"Function '{args.fn_filter}' not found in instruction map.")
            print(f"Available: {', '.join(arm.keys())}")
            return
        display(arm, target, args.fn_filter, args.all, arch)
        return

    for fn_name in arm:
        display(arm, target, fn_name, args.all, arch)


if __name__ == "__main__":
    main()
