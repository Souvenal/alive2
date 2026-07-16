#!/usr/bin/env python3
"""Visualize control flow graph (CFG) via Graphviz.

Supports LLVM IR (.ll), assembly (.s), and object files (.o).

For .s/.o: assembles with llvm-mc (for .s), then disassembles with
llvm-objdump to get structured instruction listing.  Basic blocks are
determined from branch targets and branch instructions.

Usage:
    cd tests/lift && uv run python cfg.py output/Maze.lifted.ll
    cd tests/lift && uv run python cfg.py output/maze_novarargs.o
    cd tests/lift && uv run python cfg.py output/maze_novarargs.nodbg.s
    cd tests/lift && uv run python cfg.py output/maze_novarargs.o -f main
    cd tests/lift && uv run python cfg.py output/maze_novarargs.o --open
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from asm_diff import parse_instruction_map


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        print(f"Error: '{name}' not found in PATH", file=sys.stderr)
        sys.exit(1)
    return path


# ── LLVM IR (.ll) pipeline ──────────────────────────────────────────

def handle_ir(path: str, out_dir: str, fmt: str, func_filter: str | None) -> list[str]:
    """Use opt -passes=dot-cfg → dot.  Returns rendered image paths."""
    opt = require_tool("opt")
    dot = require_tool("dot")

    with tempfile.TemporaryDirectory(prefix="cfg_") as tmpdir:
        result = subprocess.run(
            [opt, "-passes=dot-cfg", os.path.abspath(path), "-disable-output"],
            cwd=tmpdir, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            sys.exit(1)

        dot_files = sorted(Path(tmpdir).glob(".*.dot"))
        if func_filter:
            dot_files = [d for d in dot_files if func_filter in Path(d).name]

        if not dot_files:
            tag = f" matching '{func_filter}'" if func_filter else ""
            print(f"No functions found{tag} in {path}")
            return []

        stem = Path(path).name
        rendered = []
        for dot_file in dot_files:
            func_name = Path(dot_file.name.lstrip(".")).stem
            out_name = f"{stem}_{func_name}.{fmt}"
            out_path = os.path.join(out_dir, out_name)
            r = subprocess.run(
                [dot, f"-T{fmt}", str(dot_file), "-o", out_path],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                print(f"  ✓ {os.path.relpath(out_path)} ({os.path.getsize(out_path)} bytes)")
                rendered.append(out_path)
            else:
                print(f"  ✗ {func_name}: {r.stderr.strip()}")

        return rendered


# ── Machine-code (.o / .s) pipeline ─────────────────────────────────
#
#   .s  ──llvm-mc──→  .o  ──llvm-objdump──→  disassembly listing
#   .o  ───────────────────────────────────→  (same)
#
# Then parse the listing into basic blocks and edges, feed to dot(1).

# ── Architecture profiles ───────────────────────────────────────────
#
# Each profile describes how to identify branch/call/ret instructions
# for a given target triple.

ARCH_PROFILES = {
    'aarch64': {
        'uncond': {'b', 'b.al', 'b.nv'},
        'indirect': {'br'},
        'cond': {'b.eq','b.ne','b.gt','b.ge','b.lt','b.le',
                 'b.hi','b.hs','b.lo','b.ls',
                 'b.mi','b.pl','b.vs','b.vc',
                 'b.cc','b.cs',
                 'cbz','cbnz','tbz','tbnz'},
        'call': {'bl', 'blr'},
        'ret': {'ret'},
    },
    'x86-64': {
        'uncond': {'jmp', 'jmpq'},
        'indirect': set(),
        'cond': {'je','jne','jg','jge','jl','jle',
                 'ja','jae','jb','jbe',
                 'js','jns','jo','jno','jp','jnp',
                 'jcxz','jecxz','loop','loope','loopne'},
        'call': {'call', 'callq'},
        'ret': {'ret', 'retq'},
    },
}

# Regexes against llvm-objdump -d output.
RE_ADDR = re.compile(r'^\s+([0-9a-f]+):\s+')
RE_FUNC_HEADER = re.compile(r'<(\S+)>:\s*$')
RE_FILE_FMT = re.compile(r'file format (\S+)')
RE_SOURCE_LINE = re.compile(r'^;\s+(.+):(\d+)\s*$')
# For objdump output, branch target looks like "0x160" (hex address).
RE_BRANCH_TARGET = re.compile(r'\b(?:0x)?([0-9a-f]+)\b')


def detect_arch(file_fmt: str) -> str:
    """Map llvm-objdump file format string to an ARCH_PROFILES key."""
    fmt = file_fmt.lower()
    if 'aarch64' in fmt or 'arm64' in fmt:
        return 'aarch64'
    if 'x86-64' in fmt or 'x86_64' in fmt or 'amd64' in fmt:
        return 'x86-64'
    return 'x86-64'  # fallback — common default


def parse_objdump_output(
    output: str, debug_file: str | None = None
) -> tuple[str, list[dict]]:
    """Parse llvm-objdump output into structured instructions."""
    arch = 'x86-64'
    insns = []
    current_func = None
    current_debug_line = None
    expected_debug_path = os.path.normpath(debug_file) if debug_file else None

    for line in output.splitlines():
        m = RE_FILE_FMT.search(line)
        if m:
            arch = detect_arch(m.group(1))

        m = RE_FUNC_HEADER.search(line)
        if m:
            current_func = m.group(1)
            current_debug_line = None
            continue

        if debug_file:
            m = RE_SOURCE_LINE.match(line)
            if m:
                source_path = os.path.normpath(m.group(1))
                current_debug_line = (
                    int(m.group(2))
                    if source_path == expected_debug_path
                    else None
                )
                continue

        if current_func is None:
            continue

        m = RE_ADDR.match(line)
        if not m:
            continue
        addr = int(m.group(1), 16)

        rest = line[m.end():]
        rest = re.sub(r'\s*//.*', '', rest).strip()

        parts = rest.split(None, 1)
        mnemonic = parts[0] if parts else ''
        operands = parts[1] if len(parts) > 1 else ''

        insns.append({
            'func': current_func,
            'addr': addr,
            'raw': line.strip(),
            'mnemonic': mnemonic,
            'operands': operands,
            'dwarf_line': current_debug_line,
        })

    return arch, insns


def objdump(path: str, debug_file: str | None = None) -> tuple[str, list[dict]]:
    """Run llvm-objdump -d, return (arch, [insn dicts]).

    Each dict: {func, addr, raw, mnemonic, operands, dwarf_line}
    """
    objdump_tool = require_tool("llvm-objdump")
    command = [objdump_tool, "-d", "--no-show-raw-insn"]
    if debug_file:
        command.append("--line-numbers")
    command.append(path)
    r = subprocess.run(
        command,
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        sys.exit(1)

    return parse_objdump_output(r.stdout, debug_file)


def extract_target_addr(insn: dict, arch: str) -> int | None:
    """For branch instructions, extract the target address.  None if not a branch.

    Returns -1 for return instructions (sentinel).
    """
    prof = ARCH_PROFILES.get(arch, ARCH_PROFILES['x86-64'])
    mne = insn['mnemonic']
    ops = insn['operands']

    if mne in prof['ret']:
        return -1

    if mne in prof['indirect']:
        return None

    if mne in prof['uncond'] or mne in prof['cond']:
        if ops.lstrip().startswith('*'):
            return None
        # Strip objdump's <func+offset> annotation so it doesn't
        # shadow the real branch target (e.g. b.gt 0x178 <main+0xe4>
        # would otherwise match 'e4' as the last hex value).
        ops_clean = re.sub(r'<[^>]+>', '', ops)
        # Use findall + last match: for tbz/tbnz the first hex is the
        # bit-position (#0), not the branch target.
        matches = RE_BRANCH_TARGET.findall(ops_clean)
        return int(matches[-1], 16) if matches else None

    if mne in prof['call']:
        return None  # does not end a basic block

    return None


def build_cfg(insns: list[dict], arch: str) -> tuple[list, list]:
    """Given instruction list for ONE function, return (blocks, edges).

    blocks: list of (name, [instruction dict])
    edges:  list of (from_name, to_name, label)
    """
    if not insns:
        return [], []

    prof = ARCH_PROFILES.get(arch, ARCH_PROFILES['x86-64'])
    is_cond = prof['cond'].__contains__
    is_uncond = prof['uncond'].__contains__
    is_indirect = prof['indirect'].__contains__

    # ── 1. Compute basic block boundaries ──
    # Build a set of addresses known as branch targets.
    targets: set[int] = set()
    targets.add(insns[0]['addr'])  # function entry

    for idx, insn in enumerate(insns):
        target = extract_target_addr(insn, arch)
        if target is not None and target >= 0:
            targets.add(target)
        # Fallthrough after a branch is a new block boundary.
        # Only add when there IS a next instruction — for the last instruction
        # in the function, next_addr falls back to its own address, which would
        # make the instruction appear as its own branch target (isolated block).
        if (target == -1
                or is_cond(insn['mnemonic'])
                or is_uncond(insn['mnemonic'])
                or is_indirect(insn['mnemonic'])) and idx + 1 < len(insns):
            targets.add(insns[idx + 1]['addr'])

    # ── 2. Split instructions into blocks ──
    blocks = []   # [(name, [instruction dict])]
    edges = []    # [(from_name, to_name, label)]

    current = []
    start_addr = insns[0]['addr']

    def finish_block(next_start=None):
        nonlocal current, start_addr
        if not current:
            return
        name = f"L{start_addr:x}"
        blocks.append((name, current))
        current = []
        if next_start is not None:
            start_addr = next_start

    for i, insn in enumerate(insns):
        at_target = insn['addr'] in targets and i > 0
        if at_target:
            if current:
                # Previous block falls through to this one if it didn't end
                # with an unconditional branch or return.
                last_mne = current[-1]['mnemonic']
                finish_block(insn['addr'])
                if last_mne not in prof['uncond'] and last_mne not in prof['ret']:
                    edges.append((blocks[-1][0], f"L{insn['addr']:x}", ''))
            else:
                finish_block(insn['addr'])
            start_addr = insn['addr']

        current.append(insn)

        mne = insn['mnemonic']
        target = extract_target_addr(insn, arch)

        if target == -1:  # ret — block ends, no outgoing edge
            finish_block()
            continue

        next_addr = insns[i + 1]['addr'] if i + 1 < len(insns) else insn['addr']

        if is_cond(mne) and target is not None:
            finish_block()
            from_name = blocks[-1][0]
            edges.append((from_name, f"L{target:x}", 'T'))
            edges.append((from_name, f"L{next_addr:x}", 'F'))

        elif is_uncond(mne) or is_indirect(mne):
            finish_block()
            if blocks and target is not None:
                edges.append((blocks[-1][0], f"L{target:x}", 'J'))

    # Flush remaining
    finish_block()

    return blocks, edges


def assemble_to_obj(path: str) -> str:
    """Assemble .s → temp .o.  Returns path to .o (caller must clean up)."""
    mc = require_tool("llvm-mc")
    tmp = tempfile.mktemp(suffix='.o')
    r = subprocess.run(
        [mc, "-filetype=obj", "-triple=aarch64", path, "-o", tmp],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"llvm-mc error: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    return tmp


def collect_func_insns(insns: list[dict]) -> dict[str, list[dict]]:
    """Group instructions by function name."""
    funcs = {}
    for insn in insns:
        funcs.setdefault(insn['func'], []).append(insn)
    return funcs


def dump_cfg_text(blocks, edges, func_name):
    """Dump CFG in a compact text format suitable for LLM analysis."""
    edge_map = {}
    for src, dst, label in edges:
        edge_map.setdefault(src, []).append((dst, label))

    entry = blocks[0][0] if blocks else None
    print(f"\n=== Function: {func_name} ===")
    print(f"Blocks: {len(blocks)}, Edges: {len(edges)}")
    if entry:
        print(f"Entry: {entry}")

    for name, binsns in blocks:
        print(f"\n--- Block {name} ---")
        for insn in binsns:
            print(f"  {insn['raw']}")
        outs = edge_map.get(name, [])
        if outs:
            print("  [edges]")
            for dst, label in outs:
                lbl = f" ({label})" if label else " (fallthrough)"
                print(f"    -> {dst}{lbl}")
        else:
            print("  [terminal — no outgoing edges]")


def is_correlatable_source_instruction(instruction: dict) -> bool:
    """Exclude parser infrastructure that has no source instruction meaning."""
    if not instruction.get("asm", "").strip():
        return False
    if instruction.get("opcode") == "SEH_Nop":
        return False
    if (instruction.get("mc_block") == "entry"
            and instruction.get("opcode") == "B"):
        return False
    return True


def build_block_relations(
    blocks: list[tuple[str, list[dict]]],
    source_instructions: dict[int, dict[str, object]],
) -> dict:
    """Build an unweighted many-to-many source/target block relation."""
    line_to_source = {}
    source_blocks: dict[str, set[int]] = {}

    for arm_inst_id, instruction in source_instructions.items():
        if not is_correlatable_source_instruction(instruction):
            continue
        dwarf_line = instruction["dwarf_line"]
        source_block = instruction["mc_block"]
        assert isinstance(dwarf_line, int)
        assert isinstance(source_block, str)
        line_to_source[dwarf_line] = (
            arm_inst_id,
            source_block,
            instruction["opcode"],
        )
        source_blocks.setdefault(source_block, set()).add(arm_inst_id)

    relations: dict[tuple[str, str], set[int]] = {}
    matched_source_ids: set[int] = set()
    matched_target_blocks: set[str] = set()

    for target_block, instructions in blocks:
        for instruction in instructions:
            source = line_to_source.get(instruction.get("dwarf_line"))
            if source is None:
                continue
            arm_inst_id, source_block, source_opcode = source
            if (instruction.get("mnemonic", "").startswith("nop")
                    and source_opcode not in {"HINT", "NOP"}):
                continue
            relations.setdefault((source_block, target_block), set()).add(
                arm_inst_id
            )
            matched_source_ids.add(arm_inst_id)
            matched_target_blocks.add(target_block)

    relation_records = [
        {
            "source_block": source_block,
            "target_block": target_block,
            "arm_inst_ids": sorted(arm_inst_ids),
        }
        for (source_block, target_block), arm_inst_ids in sorted(
            relations.items()
        )
    ]

    unmatched_source_blocks = sorted(
        source_block
        for source_block, arm_inst_ids in source_blocks.items()
        if arm_inst_ids.isdisjoint(matched_source_ids)
    )
    unmatched_arm_inst_ids = sorted(
        arm_inst_id
        for arm_inst_ids in source_blocks.values()
        for arm_inst_id in arm_inst_ids
        if arm_inst_id not in matched_source_ids
    )
    unmatched_target_blocks = sorted(
        target_block
        for target_block, _ in blocks
        if target_block not in matched_target_blocks
    )

    return {
        "relations": relation_records,
        "unmatched_source_blocks": unmatched_source_blocks,
        "unmatched_arm_inst_ids": unmatched_arm_inst_ids,
        "unmatched_target_blocks": unmatched_target_blocks,
    }


def build_missing_function_report(
    function: str,
    target_arch: str,
    source_instructions: dict[int, dict[str, object]],
) -> dict:
    """Report a source function that has no target machine-code function."""
    source_blocks: dict[str, list[int]] = {}
    for arm_inst_id, instruction in source_instructions.items():
        if not is_correlatable_source_instruction(instruction):
            continue
        source_block = instruction["mc_block"]
        assert isinstance(source_block, str)
        source_blocks.setdefault(source_block, []).append(arm_inst_id)

    return {
        "relations": [],
        "unmatched_source_blocks": sorted(source_blocks),
        "unmatched_arm_inst_ids": sorted(
            arm_inst_id
            for arm_inst_ids in source_blocks.values()
            for arm_inst_id in arm_inst_ids
        ),
        "unmatched_target_blocks": [],
        "function": function,
        "target_arch": target_arch,
        "target_function_missing": True,
    }


def align_source_blocks_with_instruction_map(
    source_blocks: list[tuple[str, list[dict]]],
    source_instructions: dict[int, dict[str, object]],
) -> dict[int, str]:
    """Align original object instructions to function-local ARM instruction IDs."""
    source_records = [
        (arm_inst_id, instruction)
        for arm_inst_id, instruction in sorted(source_instructions.items())
        if is_correlatable_source_instruction(instruction)
    ]
    object_instructions = [
        (block_name, instruction)
        for block_name, instructions in source_blocks
        for instruction in instructions
    ]

    if len(object_instructions) != len(source_records):
        raise ValueError(
            "source object/map instruction count mismatch: "
            f"object={len(object_instructions)}, map={len(source_records)}"
        )

    arm_inst_to_source_block = {}
    for ((block_name, object_instruction),
         (arm_inst_id, source_record)) in zip(
            object_instructions, source_records, strict=True
        ):
        object_instruction["arm_inst_id"] = arm_inst_id
        object_instruction["mc_block"] = source_record["mc_block"]
        arm_inst_to_source_block[arm_inst_id] = block_name

    return arm_inst_to_source_block


def build_address_block_relations(
    relation_report: dict,
    arm_inst_to_source_block: dict[int, str],
) -> list[dict]:
    """Translate MC-block relations to original address-block relations."""
    relations: dict[tuple[str, str], set[int]] = {}
    for relation in relation_report["relations"]:
        target_block = relation["target_block"]
        for arm_inst_id in relation["arm_inst_ids"]:
            source_block = arm_inst_to_source_block.get(arm_inst_id)
            if source_block is None:
                continue
            relations.setdefault((source_block, target_block), set()).add(
                arm_inst_id
            )

    return [
        {
            "source_block": source_block,
            "target_block": target_block,
            "arm_inst_ids": sorted(arm_inst_ids),
        }
        for (source_block, target_block), arm_inst_ids in sorted(
            relations.items()
        )
    ]


def build_relation_components(address_relations: list[dict]) -> list[dict]:
    """Find connected components in the source/target block relation graph."""
    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {}
    relation_ids: dict[tuple[str, str], set[int]] = {}

    for relation in address_relations:
        source = ("source", relation["source_block"])
        target = ("target", relation["target_block"])
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)
        relation_ids.setdefault(
            (relation["source_block"], relation["target_block"]), set()
        ).update(relation["arm_inst_ids"])

    components = []
    visited: set[tuple[str, str]] = set()
    for seed in sorted(adjacency):
        if seed in visited:
            continue

        pending = [seed]
        source_blocks = set()
        target_blocks = set()
        while pending:
            node = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            side, block_name = node
            if side == "source":
                source_blocks.add(block_name)
            else:
                target_blocks.add(block_name)
            pending.extend(adjacency[node] - visited)

        arm_inst_ids = set()
        for source_block in source_blocks:
            for target_block in target_blocks:
                arm_inst_ids.update(
                    relation_ids.get((source_block, target_block), set())
                )

        components.append({
            "source_blocks": sorted(source_blocks),
            "target_blocks": sorted(target_blocks),
            "arm_inst_ids": sorted(arm_inst_ids),
        })

    return components


def escape_dot_record(text: str) -> str:
    """Escape special characters in a Graphviz record label."""
    return (text.replace('\\', '\\\\')
            .replace('"', '\\"')
            .replace('{', '\\{')
            .replace('}', '\\}')
            .replace('|', '\\|')
            .replace('<', '\\<')
            .replace('>', '\\>'))


def block_dot_label(
    block_name: str,
    instructions: list[dict],
    include_arm_ids: bool = False,
) -> str:
    """Build a Graphviz record label for a machine-code basic block."""
    body_lines = []
    for instruction in instructions:
        prefix = ""
        if include_arm_ids and "arm_inst_id" in instruction:
            prefix = f"ARM #{instruction['arm_inst_id']}  "
        body_lines.append(escape_dot_record(prefix + instruction["raw"]))
    body = '\\l'.join(body_lines) + '\\l'
    return f"{{{escape_dot_record(block_name)}:\\l|{body}}}"


def build_combined_cfg_dot(
    function: str,
    source_arch: str,
    source_blocks: list[tuple[str, list[dict]]],
    source_edges: list[tuple[str, str, str]],
    target_arch: str,
    target_blocks: list[tuple[str, list[dict]]],
    target_edges: list[tuple[str, str, str]],
    components: list[dict],
) -> str:
    """Build one DOT graph containing source and target CFGs."""
    palette = [
        ("#2563eb", "#dbeafe"),
        ("#c2410c", "#ffedd5"),
        ("#15803d", "#dcfce7"),
        ("#7e22ce", "#f3e8ff"),
        ("#a21caf", "#fae8ff"),
        ("#0f766e", "#ccfbf1"),
    ]
    source_ids = {
        block_name: f"source_b{i}"
        for i, (block_name, _) in enumerate(source_blocks)
    }
    target_ids = {
        block_name: f"target_b{i}"
        for i, (block_name, _) in enumerate(target_blocks)
    }
    source_component = {
        block_name: component_id
        for component_id, component in enumerate(components)
        for block_name in component["source_blocks"]
    }
    target_component = {
        block_name: component_id
        for component_id, component in enumerate(components)
        for block_name in component["target_blocks"]
    }

    lines = [
        f'digraph "CFG correlation for {function}" {{',
        '\tgraph [rankdir=TB,compound=true,newrank=true,pad=0.4,'
        'nodesep=0.35,ranksep=1.2];',
        '\tnode [shape=record,fontname="Courier",fontsize=9];',
        '\tedge [fontname="Helvetica",fontsize=9];',
        '\tsubgraph cluster_source {',
        f'\t\tlabel="Original CFG: {function} ({source_arch})";',
        '\t\tcolor="#64748b"; style="rounded"; penwidth=1.5;',
        '\t\tsource_side_anchor '
        '[shape=point,width=0.01,label="",style=invis];',
    ]

    for component_id, component in enumerate(components):
        color, _ = palette[component_id % len(palette)]
        lines.extend([
            f'\t\tsubgraph cluster_source_group_{component_id} {{',
            f'\t\t\tlabel="Group {component_id + 1}";',
            f'\t\t\tcolor="{color}"; style="rounded,dashed"; penwidth=2;',
            f'\t\t\tsource_anchor_{component_id} '
            '[shape=point,width=0.01,label="",style=invis];',
        ])
        for block_name in component["source_blocks"]:
            block = next(
                instructions
                for name, instructions in source_blocks
                if name == block_name
            )
            _, fill = palette[component_id % len(palette)]
            lines.append(
                f'\t\t\t{source_ids[block_name]} '
                f'[label="{block_dot_label(block_name, block, True)}",'
                f'style=filled,fillcolor="{fill}"];'
            )
        lines.append('\t\t}')

    for block_name, instructions in source_blocks:
        if block_name in source_component:
            continue
        lines.append(
            f'\t\t{source_ids[block_name]} '
            f'[label="{block_dot_label(block_name, instructions, True)}",'
            'style=filled,fillcolor="#f1f5f9",color="#94a3b8"];'
        )

    for source, target, label in source_edges:
        if source not in source_ids or target not in source_ids:
            continue
        attrs = f' [label="{label}"]' if label else ""
        lines.append(
            f'\t\t{source_ids[source]} -> {source_ids[target]}{attrs};'
        )
    lines.append('\t}')

    lines.extend([
        '\tsubgraph cluster_target {',
        f'\t\tlabel="Lifted CFG: {function} ({target_arch})";',
        '\t\tcolor="#64748b"; style="rounded"; penwidth=1.5;',
        '\t\ttarget_side_anchor '
        '[shape=point,width=0.01,label="",style=invis];',
    ])
    for component_id, component in enumerate(components):
        color, _ = palette[component_id % len(palette)]
        lines.extend([
            f'\t\tsubgraph cluster_target_group_{component_id} {{',
            f'\t\t\tlabel="Group {component_id + 1}";',
            f'\t\t\tcolor="{color}"; style="rounded,dashed"; penwidth=2;',
            f'\t\t\ttarget_anchor_{component_id} '
            '[shape=point,width=0.01,label="",style=invis];',
        ])
        for block_name in component["target_blocks"]:
            block = next(
                instructions
                for name, instructions in target_blocks
                if name == block_name
            )
            _, fill = palette[component_id % len(palette)]
            lines.append(
                f'\t\t\t{target_ids[block_name]} '
                f'[label="{block_dot_label(block_name, block)}",'
                f'style=filled,fillcolor="{fill}"];'
            )
        lines.append('\t\t}')

    for block_name, instructions in target_blocks:
        if block_name in target_component:
            continue
        lines.append(
            f'\t\t{target_ids[block_name]} '
            f'[label="{block_dot_label(block_name, instructions)}",'
            'style=filled,fillcolor="#f8fafc",color="#94a3b8"];'
        )

    for source, target, label in target_edges:
        if source not in target_ids or target not in target_ids:
            continue
        attrs = f' [label="{label}"]' if label else ""
        lines.append(
            f'\t\t{target_ids[source]} -> {target_ids[target]}{attrs};'
        )
    lines.append('\t}')

    lines.append(
        '\t{ rank=same; source_side_anchor; target_side_anchor; }'
    )
    lines.append(
        '\tsource_side_anchor -> target_side_anchor '
        '[style=invis,weight=100];'
    )
    for component_id, component in enumerate(components):
        color, _ = palette[component_id % len(palette)]
        evidence_count = len(component["arm_inst_ids"])
        lines.append(
            f'\t{{ rank=same; source_anchor_{component_id}; '
            f'target_anchor_{component_id}; }}'
        )
        lines.append(
            f'\tsource_anchor_{component_id} -> target_anchor_{component_id} '
            f'[dir=both,arrowhead=vee,arrowtail=vee,color="{color}",'
            f'fontcolor="{color}",penwidth=2.5,minlen=2,'
            f'label="{evidence_count} ARM IDs",'
            f'ltail=cluster_source_group_{component_id},'
            f'lhead=cluster_target_group_{component_id},constraint=false];'
        )

    lines.append('}')
    return '\n'.join(lines)


def render_combined_cfg(
    source_path: str,
    target_path: str,
    instruction_map: dict[str, dict[int, dict[str, object]]],
    debug_file: str,
    function: str,
    out_dir: str,
    fmt: str,
) -> tuple[str, dict]:
    """Render original and lifted CFGs with framed relation components."""
    source_arch, source_all = objdump(source_path)
    target_arch, target_all = objdump(target_path, debug_file)
    source_funcs = collect_func_insns(source_all)
    target_funcs = collect_func_insns(target_all)

    if function not in source_funcs:
        raise ValueError(f"function '{function}' not found in source object")
    if function not in target_funcs:
        raise ValueError(f"function '{function}' not found in target binary")

    source_blocks, source_edges = build_cfg(
        source_funcs[function], source_arch
    )
    target_blocks, target_edges = build_cfg(
        target_funcs[function], target_arch
    )
    source_instructions = instruction_map[function]
    arm_inst_to_source_block = align_source_blocks_with_instruction_map(
        source_blocks, source_instructions
    )
    report = build_block_relations(target_blocks, source_instructions)
    address_relations = build_address_block_relations(
        report, arm_inst_to_source_block
    )
    components = build_relation_components(address_relations)
    report.update({
        "function": function,
        "source_arch": source_arch,
        "target_arch": target_arch,
        "address_relations": address_relations,
        "components": components,
        "target_function_missing": False,
    })

    dot_source = build_combined_cfg_dot(
        function,
        source_arch,
        source_blocks,
        source_edges,
        target_arch,
        target_blocks,
        target_edges,
        components,
    )
    dot = require_tool("dot")
    source_stem = Path(source_path).name
    target_stem = Path(target_path).name
    out_name = f"{source_stem}_vs_{target_stem}_{function}.{fmt}"
    out_path = os.path.join(out_dir, out_name)
    result = subprocess.run(
        [dot, f"-T{fmt}", "-o", out_path],
        input=dot_source,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"dot error: {result.stderr.strip()}")

    return out_path, report


def dump_block_relations(report: dict) -> None:
    """Print a compact, evidence-carrying relation report."""
    print(f"\n=== Block relations: {report['function']} ===")
    relations = report["relations"]
    if relations:
        for relation in relations:
            print(
                f"  {relation['source_block']} -> "
                f"{relation['target_block']}: "
                f"ARM {relation['arm_inst_ids']}"
            )
    else:
        print("  (no block relations)")

    if report.get("target_function_missing"):
        print("  target function missing")
    if report["unmatched_source_blocks"]:
        print(
            "  unmatched source blocks: "
            + ", ".join(report["unmatched_source_blocks"])
        )
    if report["unmatched_arm_inst_ids"]:
        print(
            "  unmatched ARM instructions: "
            + ", ".join(str(i) for i in report["unmatched_arm_inst_ids"])
        )
    if report["unmatched_target_blocks"]:
        print(
            "  unmatched target blocks: "
            + ", ".join(report["unmatched_target_blocks"])
        )


def handle_obj(path: str, out_dir: str, fmt: str, func_filter: str | None,
               stem: str | None = None, text: bool = False,
               debug_file: str | None = None,
               instruction_map:
               dict[str, dict[int, dict[str, object]]] | None = None
               ) -> tuple[list[str], list[dict]]:
    """Handle machine code via llvm-objdump, CFG parsing, and optional mapping."""
    arch, all_insns = objdump(path, debug_file)
    funcs = collect_func_insns(all_insns)

    if stem is None:
        stem = Path(path).name
    rendered = []
    relation_reports = []
    correlated_functions = set()

    for func_name, insns in funcs.items():
        if instruction_map is not None:
            if func_filter and func_name != func_filter:
                continue
            if func_name not in instruction_map:
                continue
        elif func_filter and func_filter not in func_name:
            continue

        blocks, edges = build_cfg(insns, arch)
        if not blocks:
            print(f"  ~ {func_name}: no basic blocks found")
            continue

        source_instructions = (
            instruction_map[func_name]
            if instruction_map is not None
            else None
        )
        if source_instructions is not None:
            report = build_block_relations(blocks, source_instructions)
            report["function"] = func_name
            report["target_arch"] = arch
            report["target_function_missing"] = False
            relation_reports.append(report)
            correlated_functions.add(func_name)
            dump_block_relations(report)
        elif instruction_map is not None:
            print(f"  ~ {func_name}: not found in instruction map")

        if text:
            dump_cfg_text(blocks, edges, func_name)
            continue

        dot = require_tool("dot")

        # Generate DOT
        lines = []
        lines.append(f'digraph "CFG for \'{func_name}\'" {{')
        lines.append(f'\tlabel="CFG for \'{func_name}\'";')
        lines.append('\tpad=0.5;')
        lines.append('\tnode [shape=record,fontname="Courier",fontsize=10];')
        lines.append('\tedge [fontname="Courier",fontsize=9];')

        def esc(s: str) -> str:
            """Escape special chars for DOT record label."""
            return s.replace('\\', '\\\\') \
                    .replace('"', '\\"') \
                    .replace('{', '\\{') \
                    .replace('}', '\\}') \
                    .replace('|', '\\|') \
                    .replace('<', '\\<') \
                    .replace('>', '\\>')

        for i, (bname, binsns) in enumerate(blocks):
            nid = f"b{i}"
            header = esc(bname)
            body = '\\l'.join(esc(insn['raw']) for insn in binsns) + '\\l'
            label = f"{{{header}:\\l|{body}}}"
            color = "#3d50c3ff" if i == 0 else "#b70d28ff"
            fill = "#d6524470" if i == 0 else "#b70d2870"
            lines.append(f'\t{nid} [shape=record,label="{label}",color="{color}",style=filled,fillcolor="{fill}"];')

        for src, dst, label in edges:
            # Resolve block names to IDs
            src_id = None
            dst_id = None
            for i, (bname, _) in enumerate(blocks):
                if bname == src:
                    src_id = f"b{i}"
                if bname == dst:
                    dst_id = f"b{i}"
            if src_id and dst_id:
                if label:
                    lines.append(f'\t{src_id} -> {dst_id} [label="{label}",fontsize=9];')
                else:
                    lines.append(f'\t{src_id} -> {dst_id};')

        lines.append('}')

        out_name = f"{stem}_{func_name}.{fmt}"
        out_path = os.path.join(out_dir, out_name)
        dot_source = '\n'.join(lines)

        r = subprocess.run(
            [dot, f"-T{fmt}", "-o", out_path],
            input=dot_source, capture_output=True, text=True,
        )
        if r.returncode == 0:
            print(f"  ✓ {os.path.relpath(out_path)} ({os.path.getsize(out_path)} bytes)")
            rendered.append(out_path)
        else:
            print(f"  ✗ {func_name}: dot error: {r.stderr.strip()}")

    if instruction_map is not None:
        requested_functions = (
            {func_filter} if func_filter else set(instruction_map)
        )
        for func_name in sorted(requested_functions - correlated_functions):
            report = build_missing_function_report(
                func_name, arch, instruction_map[func_name]
            )
            relation_reports.append(report)
            dump_block_relations(report)

    return rendered, relation_reports


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Visualize CFG from .ll, .s, or .o files"
    )
    parser.add_argument("input", help="Path to .ll, .s, or .o file")
    parser.add_argument(
        "-f", "--function",
        help="Function substring; exact name with --asm-map",
    )
    parser.add_argument("-o", "--output-dir", default="output/cfg",
                        help="Output directory (default: output/cfg)")
    parser.add_argument("--fmt", default="svg",
                        help="Output format (svg/png/pdf; default: svg)")
    parser.add_argument("--text", action="store_true",
                        help="Dump CFG as text instead of rendering an image")
    parser.add_argument("--open", action="store_true",
                        help="Open rendered image(s) (macOS)")
    parser.add_argument("--asm-map",
                        help="Instruction map JSON for block correlation")
    parser.add_argument("--relations-json",
                        help="Write block relation report JSON to this path")
    parser.add_argument(
        "--source-object",
        help="Original object file; render a combined source/target CFG",
    )

    args = parser.parse_args()

    if args.relations_json and not args.asm_map:
        parser.error("--relations-json requires --asm-map")
    if args.source_object and not args.asm_map:
        parser.error("--source-object requires --asm-map")
    if args.source_object and not args.function:
        parser.error("--source-object requires --function")

    in_path = os.path.abspath(args.input)
    if not os.path.isfile(in_path):
        print(f"Error: file not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    out_dir = args.output_dir
    os.makedirs(out_dir, exist_ok=True)

    ext = Path(in_path).suffix.lower()
    clean_obj = None  # temp file to clean up
    debug_file = None
    instruction_map = None
    relation_reports = []

    if args.asm_map:
        debug_file, instruction_map = parse_instruction_map(args.asm_map)
        if args.function and args.function not in instruction_map:
            parser.error(
                f"function '{args.function}' not found in instruction map"
            )

    if args.source_object:
        source_path = os.path.abspath(args.source_object)
        if not os.path.isfile(source_path):
            parser.error(f"source object not found: {source_path}")
        try:
            combined_path, report = render_combined_cfg(
                source_path,
                in_path,
                instruction_map,
                debug_file,
                args.function,
                out_dir,
                args.fmt,
            )
        except (ValueError, RuntimeError) as error:
            parser.error(str(error))
        rendered = [combined_path]
        relation_reports = [report]
        print(f"Combined CFG: {combined_path}")

    elif ext == '.ll':
        if args.asm_map:
            print("Error: --asm-map requires machine-code input",
                  file=sys.stderr)
            sys.exit(1)
        if args.text:
            print("Error: --text is not yet supported for .ll files", file=sys.stderr)
            sys.exit(1)
        count = sum(1 for l in open(in_path) if l.startswith("define "))
        print(f"IR file: {in_path} ({count} functions)")
        rendered = handle_ir(in_path, out_dir, args.fmt, args.function)

    elif ext == '.s':
        print(f"Assembly: {in_path}")
        if not args.text:
            print("  → Assembling to .o with llvm-mc ...")
        clean_obj = assemble_to_obj(in_path)
        rendered, relation_reports = handle_obj(
            clean_obj, out_dir, args.fmt, args.function,
            stem=Path(in_path).name, text=args.text,
            debug_file=debug_file, instruction_map=instruction_map,
        )

    elif ext == '.o' or args.asm_map:
        rendered, relation_reports = handle_obj(
            in_path, out_dir, args.fmt, args.function, text=args.text,
            debug_file=debug_file, instruction_map=instruction_map,
        )

    else:
        print(f"Error: unsupported extension '{ext}' (use .ll, .s, or .o)", file=sys.stderr)
        sys.exit(1)

    if clean_obj and os.path.exists(clean_obj):
        os.unlink(clean_obj)

    if args.relations_json:
        relations_path = Path(args.relations_json)
        relations_path.parent.mkdir(parents=True, exist_ok=True)
        relations_path.write_text(
            json.dumps(
                {"version": 1, "functions": relation_reports},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Relations JSON: {relations_path}")

    if not rendered:
        sys.exit(0)

    if args.open:
        for img in rendered:
            subprocess.run(["open", img])
        print(f"Opened {len(rendered)} image(s).")


if __name__ == "__main__":
    main()
