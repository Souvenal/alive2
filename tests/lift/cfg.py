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
import html
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


def build_combined_cfg_model(
    source_path: str,
    target_path: str,
    instruction_map: dict[str, dict[int, dict[str, object]]],
    debug_file: str,
    function: str,
) -> dict:
    """Build source/target CFG data and relation components for one function."""
    source_arch, source_all = objdump(source_path)
    target_arch, target_all = objdump(target_path, debug_file)
    source_funcs = collect_func_insns(source_all)
    target_funcs = collect_func_insns(target_all)

    return _build_combined_cfg_model(
        source_arch,
        source_funcs,
        target_arch,
        target_funcs,
        instruction_map,
        function,
    )


def _build_combined_cfg_model(
    source_arch: str,
    source_funcs: dict[str, list[dict]],
    target_arch: str,
    target_funcs: dict[str, list[dict]],
    instruction_map: dict[str, dict[int, dict[str, object]]],
    function: str,
) -> dict:
    """Build one model from already-disassembled source and target functions."""

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

    line_to_arm_inst = {
        instruction["dwarf_line"]: arm_inst_id
        for arm_inst_id, instruction in source_instructions.items()
        if is_correlatable_source_instruction(instruction)
    }

    def block_data(
        blocks: list[tuple[str, list[dict]]],
        is_source: bool,
    ) -> list[dict]:
        result = []
        for block_name, instructions in blocks:
            output_instructions = []
            for instruction in instructions:
                output_instruction = {
                    "address": f"{instruction['addr']:x}",
                    "text": instruction["raw"],
                }
                if is_source:
                    output_instruction["arm_inst_id"] = instruction.get(
                        "arm_inst_id"
                    )
                    output_instruction["mc_block"] = instruction.get(
                        "mc_block"
                    )
                else:
                    output_instruction["dwarf_line"] = instruction.get(
                        "dwarf_line"
                    )
                    output_instruction["arm_inst_id"] = line_to_arm_inst.get(
                        instruction.get("dwarf_line")
                    )
                output_instructions.append(output_instruction)
            result.append({
                "id": block_name,
                "instructions": output_instructions,
            })
        return result

    return {
        "function": function,
        "source": {
            "arch": source_arch,
            "blocks": block_data(source_blocks, True),
            "edges": [
                {"source": source, "target": target, "label": label}
                for source, target, label in source_edges
            ],
        },
        "target": {
            "arch": target_arch,
            "blocks": block_data(target_blocks, False),
            "edges": [
                {"source": source, "target": target, "label": label}
                for source, target, label in target_edges
            ],
        },
        "report": report,
        "_source_blocks": source_blocks,
        "_source_edges": source_edges,
        "_target_blocks": target_blocks,
        "_target_edges": target_edges,
    }


def build_combined_cfg_models(
    source_path: str,
    target_path: str,
    instruction_map: dict[str, dict[int, dict[str, object]]],
    debug_file: str,
    functions: list[str] | None = None,
) -> dict[str, dict]:
    """Build correlation models for all requested functions in one objdump pass."""
    source_arch, source_all = objdump(source_path)
    target_arch, target_all = objdump(target_path, debug_file)
    source_funcs = collect_func_insns(source_all)
    target_funcs = collect_func_insns(target_all)

    available = set(instruction_map) & set(source_funcs) & set(target_funcs)
    requested = functions if functions is not None else sorted(available)
    if not requested:
        raise ValueError("no functions are shared by instruction map and binaries")

    models = {}
    for function in requested:
        if function not in instruction_map:
            raise ValueError(f"function '{function}' not found in instruction map")
        if function not in source_funcs:
            raise ValueError(f"function '{function}' not found in source object")
        if function not in target_funcs:
            raise ValueError(f"function '{function}' not found in target binary")
        models[function] = _build_combined_cfg_model(
            source_arch,
            source_funcs,
            target_arch,
            target_funcs,
            instruction_map,
            function,
        )
    return models


def render_combined_cfg_from_model(
    model: dict,
    out_dir: str,
    source_path: str,
    target_path: str,
    fmt: str,
) -> tuple[str, dict]:
    """Render a Graphviz combined CFG from a prepared model."""
    dot_source = build_combined_cfg_dot(
        model["function"],
        model["source"]["arch"],
        model["_source_blocks"],
        model["_source_edges"],
        model["target"]["arch"],
        model["_target_blocks"],
        model["_target_edges"],
        model["report"]["components"],
    )
    dot = require_tool("dot")
    source_stem = Path(source_path).name
    target_stem = Path(target_path).name
    out_name = f"{source_stem}_vs_{target_stem}_{model['function']}.{fmt}"
    out_path = os.path.join(out_dir, out_name)
    result = subprocess.run(
        [dot, f"-T{fmt}", "-o", out_path],
        input=dot_source,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"dot error: {result.stderr.strip()}")

    return out_path, model["report"]


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
    model = build_combined_cfg_model(
        source_path, target_path, instruction_map, debug_file, function
    )
    return render_combined_cfg_from_model(
        model, out_dir, source_path, target_path, fmt
    )


def render_interactive_viewer(
    models: dict[str, dict] | dict,
    out_dir: str,
    source_path: str,
    target_path: str,
    output_path: str | None = None,
) -> str:
    """Write a self-contained HTML block-correlation viewer."""
    if "function" in models:
        models = {models["function"]: models}
    viewer_data = {
        function: {
            "function": model["function"],
            "source": model["source"],
            "target": model["target"],
            "report": model["report"],
        }
        for function, model in models.items()
    }
    default_function = next(iter(viewer_data))
    data_json = json.dumps(viewer_data, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    source_stem = Path(source_path).name
    target_stem = Path(target_path).name
    out_path = output_path or os.path.join(
        out_dir, f"{source_stem}_vs_{target_stem}_cfg.html"
    )
    title = html.escape("CFG correlation")
    document = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light;
  --ink: #172033;
  --muted: #64748b;
  --line: #cbd5e1;
  --canvas: #f8fafc;
  --source: #1d4ed8;
  --target: #c2410c;
  --selected: #fef3c7;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-width: 960px;
  color: var(--ink);
  background: #ffffff;
  font: 14px/1.4 Inter, ui-sans-serif, system-ui, sans-serif;
}
button { font: inherit; }
.topbar {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: 14px 20px;
  border-bottom: 1px solid var(--line);
  background: #ffffff;
}
.title { font-weight: 700; font-size: 16px; }
.function-select { min-width: 150px; margin-left: 8px; padding: 4px 6px; }
.meta { color: var(--muted); font-size: 12px; }
main { padding: 20px; }
#overview { max-width: 1320px; margin: 0 auto; }
.overview-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 10px;
}
.overview-head strong { font-size: 13px; }
.overview-canvas {
  height: 490px;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--canvas);
}
.overview-inner { position: relative; min-width: 100%; min-height: 100%; }
.overview-graphs {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 130px minmax(0, 1fr);
  gap: 12px;
  align-items: stretch;
}
.overview-panel {
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 6px;
  overflow: hidden;
}
.overview-panel header {
  padding: 9px 12px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}
.overview-links {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 8px;
}
.overview-link {
  border: 0;
  border-top: 2px dashed var(--group);
  border-bottom: 2px dashed var(--group);
  color: var(--group);
  background: #ffffff;
  padding: 7px 2px;
  font-size: 11px;
  font-weight: 700;
  cursor: pointer;
}
.group-node {
  position: absolute;
  width: 198px;
  min-height: 72px;
  padding: 10px;
  border: 1px solid var(--line);
  border-left: 4px solid var(--group);
  border-radius: 6px;
  color: var(--ink);
  background: var(--group-fill);
  text-align: left;
  cursor: pointer;
}
.group-node:hover { border-color: var(--group); }
.group-node strong { display: block; }
.group-node small { display: block; color: var(--muted); margin-top: 5px; }
.group-node.unmatched { background: #f1f5f9; border-left-color: #64748b; }
.unmatched {
  margin-top: 18px;
  padding: 12px;
  border: 1px dashed var(--line);
  color: var(--muted);
  background: var(--canvas);
}
#detail[hidden], #overview[hidden] { display: none; }
.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-bottom: 14px;
}
.detail-head h2 { margin: 0; font-size: 16px; }
.back {
  border: 1px solid var(--line);
  border-radius: 5px;
  background: #ffffff;
  padding: 7px 10px;
  cursor: pointer;
}
.graphs {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  min-height: 430px;
}
.graph-panel {
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 6px;
  overflow: hidden;
  background: #ffffff;
}
.graph-panel header {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  font-weight: 700;
}
.source-header { color: var(--source); }
.target-header { color: var(--target); }
.graph-canvas {
  position: relative;
  height: 380px;
  overflow: auto;
  background: var(--canvas);
}
.graph-inner { position: relative; min-width: 100%; min-height: 100%; }
.edge-layer { position: absolute; inset: 0; overflow: visible; pointer-events: none; }
.edge-layer path { stroke: #94a3b8; stroke-width: 1.4; fill: none; }
.block-node {
  position: absolute;
  width: 198px;
  min-height: 58px;
  padding: 8px;
  border: 1px solid var(--line);
  border-left: 4px solid var(--side);
  border-radius: 5px;
  color: var(--ink);
  background: #ffffff;
  text-align: left;
  cursor: pointer;
  overflow: hidden;
}
.block-node:hover { border-color: var(--side); }
.block-node.selected { background: var(--selected); box-shadow: 0 0 0 2px var(--side); }
.block-node.related { border-color: var(--group); box-shadow: 0 0 0 2px var(--group); }
.block-node.paired { background: #ecfdf5; box-shadow: 0 0 0 2px #15803d; }
.block-id { display: block; font: 700 12px ui-monospace, SFMono-Regular, Menlo, monospace; }
.block-summary { display: block; margin-top: 4px; color: var(--muted); font-size: 12px; }
.relation-panel {
  margin-top: 16px;
  border-top: 1px solid var(--line);
  padding-top: 14px;
}
.relation-panel h3 { margin: 0 0 8px; font-size: 14px; }
.relation-picker { display: flex; flex-wrap: wrap; gap: 8px; }
.relation-pair {
  border: 1px solid var(--line);
  border-left: 4px solid #15803d;
  border-radius: 5px;
  color: var(--ink);
  background: #ffffff;
  padding: 7px 9px;
  font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, monospace;
  cursor: pointer;
}
.relation-pair:hover, .relation-pair.selected {
  border-color: #15803d;
  background: #ecfdf5;
}
.instruction-panel {
  margin-top: 16px;
  border-top: 1px solid var(--line);
  padding-top: 14px;
}
.instruction-panel h3 { margin: 0 0 8px; font-size: 14px; }
.correlation-list {
  max-height: 300px;
  overflow: auto;
  border: 1px solid var(--line);
  background: #ffffff;
}
.correlation-row {
  display: grid;
  grid-template-columns: 92px minmax(0, 1fr) minmax(0, 1fr);
  border-bottom: 1px solid #e2e8f0;
  font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, monospace;
}
.correlation-row:last-child { border-bottom: 0; }
.correlation-row > span {
  min-width: 0;
  padding: 6px 8px;
  border-right: 1px solid #e2e8f0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.correlation-row > span:last-child { border-right: 0; }
.correlation-row.header {
  position: sticky;
  top: 0;
  z-index: 1;
  color: var(--muted);
  background: #f8fafc;
  font: 700 11px/1.35 Inter, ui-sans-serif, system-ui, sans-serif;
  text-transform: uppercase;
}
.correlation-id { color: #475569; font-weight: 700; }
.correlation-source { background: #eff6ff; }
.correlation-target { background: #fff7ed; }
.instruction-list {
  max-height: 250px;
  overflow: auto;
  border: 1px solid var(--line);
  background: #ffffff;
}
.instruction {
  display: grid;
  grid-template-columns: 88px 1fr;
  gap: 8px;
  padding: 5px 8px;
  border-bottom: 1px solid #e2e8f0;
  font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, monospace;
}
.instruction:last-child { border-bottom: 0; }
.instruction-id { color: #475569; }
.instruction.mapped { background: #eff6ff; }
.related-list { color: var(--muted); font-size: 12px; margin-top: 8px; }
</style>
</head>
<body>
<header class="topbar">
  <div class="title">__TITLE__ <select class="function-select" id="function-select" aria-label="Function"></select></div>
  <div class="meta" id="meta"></div>
</header>
<main>
  <section id="overview">
    <div class="overview-head">
      <strong>Group-level CFG</strong>
      <span class="meta">Blue edges: original CFG. Orange edges: lifted CFG.</span>
    </div>
    <div class="overview-graphs">
      <section class="overview-panel">
        <header>Original CFG groups</header>
        <div class="overview-canvas"><div class="overview-inner" id="overview-source"></div></div>
      </section>
      <div class="overview-links" id="overview-links"></div>
      <section class="overview-panel">
        <header>Lifted CFG groups</header>
        <div class="overview-canvas"><div class="overview-inner" id="overview-target"></div></div>
      </section>
    </div>
    <div class="unmatched" id="unmatched"></div>
  </section>
  <section id="detail" hidden>
    <div class="detail-head">
      <button class="back" id="back">Back to groups</button>
      <h2 id="detail-title"></h2>
      <div class="meta" id="detail-meta"></div>
    </div>
    <div class="graphs">
      <section class="graph-panel">
        <header class="source-header" id="source-title"></header>
        <div class="graph-canvas"><div class="graph-inner" id="source-graph"></div></div>
      </section>
      <section class="graph-panel">
        <header class="target-header" id="target-title"></header>
        <div class="graph-canvas"><div class="graph-inner" id="target-graph"></div></div>
      </section>
    </div>
    <section class="relation-panel">
      <h3 id="relation-title">Select a block to inspect relation pairs</h3>
      <div class="relation-picker" id="relation-picker"></div>
    </section>
    <section class="instruction-panel">
      <h3 id="correlation-title">Select a relation pair to inspect DWARF provenance evidence</h3>
      <div class="correlation-list" id="correlation-list"></div>
      <h3 id="instruction-title">Selected block instructions</h3>
      <div class="instruction-list" id="instruction-list"></div>
      <div class="related-list" id="related-list"></div>
    </section>
  </section>
</main>
<script>
const MODELS = __DATA__;
const FUNCTION_NAMES = Object.keys(MODELS);
let DATA = MODELS[__DEFAULT_FUNCTION_JSON__];
const COLORS = [
  ["#2563eb", "#dbeafe"], ["#c2410c", "#ffedd5"],
  ["#15803d", "#dcfce7"], ["#7e22ce", "#f3e8ff"],
  ["#a21caf", "#fae8ff"], ["#0f766e", "#ccfbf1"]
];
const state = { group: null, selection: null, relation: null };
function text(node, value) { node.textContent = value; }
function blockMap(side) {
  return new Map(DATA[side].blocks.map(block => [block.id, block]));
}
function relatedBlocks(side, id) {
  const out = new Set();
  for (const relation of DATA.report.address_relations) {
    if (side === "source" && relation.source_block === id) out.add(relation.target_block);
    if (side === "target" && relation.target_block === id) out.add(relation.source_block);
  }
  return out;
}
function relationsForBlock(side, id) {
  return DATA.report.address_relations.filter(relation =>
    side === "source"
      ? relation.source_block === id
      : relation.target_block === id
  );
}
function relationForBlocks(left, right) {
  const source = left.side === "source" ? left.id : right.id;
  const target = left.side === "target" ? left.id : right.id;
  return DATA.report.address_relations.find(relation =>
    relation.source_block === source && relation.target_block === target
  ) || null;
}
function idsForBlock(side, block) {
  return [...new Set(block.instructions.map(insn => insn.arm_inst_id).filter(x => x !== null && x !== undefined))];
}
function instructionsForIds(side, blockIds, armId) {
  const blocks = blockMap(side);
  const result = [];
  for (const blockId of blockIds) {
    const block = blocks.get(blockId);
    if (!block) continue;
    for (const instruction of block.instructions) {
      if (instruction.arm_inst_id === armId) {
        result.push(`${blockId}: ${instruction.text}`);
      }
    }
  }
  return result;
}
function relationInstructionCorrespondence(relation) {
  return relation.arm_inst_ids.map(armId => ({
    armId,
    source: instructionsForIds("source", [relation.source_block], armId),
    target: instructionsForIds("target", [relation.target_block], armId),
  }));
}
function addCorrelationRow(root, id, source, target, header = false) {
  const row = document.createElement("div");
  row.className = `correlation-row${header ? " header" : ""}`;
  const cells = [
    ["correlation-id", id],
    ["correlation-source", source],
    ["correlation-target", target],
  ];
  for (const [className, value] of cells) {
    const cell = document.createElement("span");
    cell.className = className;
    text(cell, value);
    row.append(cell);
  }
  root.append(row);
}
function createOverviewGroups() {
  const source = [], target = [];
  DATA.report.components.forEach((component, index) => {
    const [color, fill] = COLORS[index % COLORS.length];
    source.push({
      id: `group-${index}`, kind: "matched", side: "source", index, color, fill,
      label: `Source group ${index + 1}`, blockIds: component.source_blocks,
      armIds: component.arm_inst_ids
    });
    target.push({
      id: `group-${index}`, kind: "matched", side: "target", index, color, fill,
      label: `Lifted group ${index + 1}`, blockIds: component.target_blocks,
      armIds: component.arm_inst_ids
    });
  });
  if (DATA.report.unmatched_source_blocks.length || DATA.report.unmatched_arm_inst_ids.length) {
    source.push({
      id: "unmatched-source", kind: "unmatched", side: "source", color: "#64748b", fill: "#f1f5f9",
      label: "Unmatched source", blockIds: DATA.report.unmatched_source_blocks,
      armIds: DATA.report.unmatched_arm_inst_ids
    });
  }
  if (DATA.report.unmatched_target_blocks.length) {
    target.push({
      id: "unmatched-target", kind: "unmatched", side: "target", color: "#64748b", fill: "#f1f5f9",
      label: "Unmatched lifted", blockIds: DATA.report.unmatched_target_blocks,
      armIds: []
    });
  }
  return { source, target };
}
function groupMembership(groups) {
  const membership = new Map();
  groups.forEach(group => group.blockIds.forEach(block => membership.set(block, group.id)));
  return membership;
}
function groupEdges(side, groups) {
  const membership = groupMembership(groups);
  const edges = new Map();
  for (const edge of DATA[side].edges) {
    const source = membership.get(edge.source);
    const target = membership.get(edge.target);
    if (!source || !target || source === target) continue;
    edges.set(`${source}:${target}`, { source, target });
  }
  return [...edges.values()];
}
function layoutGroupNodes(groups, edges) {
  const level = blockLevels(groups, edges);
  const columns = new Map();
  groups.forEach(group => {
    const current = level.get(group.id);
    if (!columns.has(current)) columns.set(current, []);
    columns.get(current).push(group);
  });
  const positions = new Map();
  let maxY = 0;
  for (const [current, column] of columns) {
    column.forEach((group, index) => {
      const position = { x: current * 230, y: 42 + index * 102 };
      positions.set(group.id, position);
      maxY = Math.max(maxY, position.y + 76);
    });
  }
  return {
    positions,
    width: Math.max(220, (Math.max(0, ...level.values()) + 1) * 230),
    height: Math.max(150, maxY + 24)
  };
}
function makeSvg(width, height) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "edge-layer");
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.innerHTML = `<defs>
    <marker id="overview-source-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#1d4ed8"/></marker>
    <marker id="overview-target-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#c2410c"/></marker>
    <marker id="overview-match-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#475569"/></marker>
  </defs>`;
  return svg;
}
function appendEdge(svg, from, to, color, marker, dashed, label, bidirectional = false) {
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  let x1 = from.x + 198, y1 = from.y + 30, x2 = to.x, y2 = to.y + 30;
  if (bidirectional) {
    x1 += 8;
    x2 -= 8;
  }
  path.setAttribute("d", `M ${x1} ${y1} C ${x1 + 28} ${y1}, ${x2 - 28} ${y2}, ${x2} ${y2}`);
  path.setAttribute("stroke", color);
  path.setAttribute("marker-end", `url(#${marker})`);
  if (bidirectional) path.setAttribute("marker-start", `url(#${marker})`);
  if (dashed) path.setAttribute("stroke-dasharray", "5 4");
  svg.append(path);
  if (label) {
    const textNode = document.createElementNS("http://www.w3.org/2000/svg", "text");
    textNode.setAttribute("x", (x1 + x2) / 2);
    textNode.setAttribute("y", (y1 + y2) / 2 - 7);
    textNode.setAttribute("text-anchor", "middle");
    textNode.setAttribute("fill", color);
    textNode.setAttribute("font-size", "11");
    textNode.textContent = label;
    svg.append(textNode);
  }
}
function renderOverview() {
  const groups = createOverviewGroups();
  const sourceEdges = groupEdges("source", groups.source);
  const targetEdges = groupEdges("target", groups.target);
  renderOverviewSide("overview-source", groups.source, sourceEdges, "#1d4ed8", "overview-source-arrow");
  renderOverviewSide("overview-target", groups.target, targetEdges, "#c2410c", "overview-target-arrow");
  const links = document.querySelector("#overview-links");
  links.replaceChildren();
  DATA.report.components.forEach((component, index) => {
    const [color] = COLORS[index % COLORS.length];
    const link = document.createElement("button");
    link.className = "overview-link";
    link.style.setProperty("--group", color);
    text(link, `<-> ${component.arm_inst_ids.length} IDs`);
    link.addEventListener("click", () => openGroup(index));
    links.append(link);
  });
  const unmatched = document.querySelector("#unmatched");
  text(unmatched,
    `Unmatched source blocks: ${DATA.report.unmatched_source_blocks.length}; ` +
    `unmatched target blocks: ${DATA.report.unmatched_target_blocks.length}; ` +
    `unmatched ARM instructions: ${DATA.report.unmatched_arm_inst_ids.length}.`);
}
function renderOverviewSide(rootId, groups, edges, color, marker) {
  const root = document.querySelector(`#${rootId}`);
  root.replaceChildren();
  const layout = layoutGroupNodes(groups, edges);
  root.style.width = `${layout.width + 24}px`;
  root.style.height = `${Math.max(400, layout.height)}px`;
  const svg = makeSvg(layout.width + 24, Math.max(400, layout.height));
  for (const edge of edges) {
    appendEdge(svg, layout.positions.get(edge.source), layout.positions.get(edge.target), color, marker, false);
  }
  root.append(svg);
  for (const group of groups) addOverviewNode(root, group, layout.positions.get(group.id));
}
function addOverviewNode(root, group, position) {
  const button = document.createElement("button");
  button.className = `group-node ${group.kind === "unmatched" ? "unmatched" : ""}`;
  button.style.left = `${position.x + 12}px`;
  button.style.top = `${position.y}px`;
  button.style.setProperty("--group", group.color);
  button.style.setProperty("--group-fill", group.fill);
  button.title = `Open ${group.label}`;
  button.innerHTML = `<strong>${group.label}</strong><small>${group.blockIds.length} blocks, ${group.armIds.length} ARM IDs</small>`;
  button.addEventListener("click", () => {
    if (group.kind === "matched") openGroup(group.index);
    else openUnmatched(group.side, group.armIds);
  });
  root.append(button);
}
function openGroup(index) {
  state.group = index;
  state.selection = null;
  state.relation = null;
  state.unmatchedIds = null;
  document.querySelector("#overview").hidden = true;
  document.querySelector("#detail").hidden = false;
  const component = DATA.report.components[index];
  text(document.querySelector("#detail-title"), `Group ${index + 1}`);
  text(document.querySelector("#detail-meta"),
    `${component.source_blocks.length} source blocks <-> ${component.target_blocks.length} lifted blocks`);
  text(document.querySelector("#source-title"), `Original CFG (${DATA.source.arch})`);
  text(document.querySelector("#target-title"), `Lifted CFG (${DATA.target.arch})`);
  renderGraph("source", component.source_blocks);
  renderGraph("target", component.target_blocks);
  renderRelationPicker();
  renderInstructions();
}
function resetDetail() {
  state.group = null;
  state.selection = null;
  state.relation = null;
  state.unmatchedIds = null;
  document.querySelector("#detail").hidden = true;
  document.querySelector("#overview").hidden = false;
}
function selectFunction(functionName) {
  DATA = MODELS[functionName];
  resetDetail();
  text(document.querySelector("#meta"), `${DATA.source.arch} -> ${DATA.target.arch}`);
  renderOverview();
}
function openUnmatched(side, armIds) {
  state.group = null;
  state.selection = null;
  state.relation = null;
  state.unmatchedIds = armIds;
  document.querySelector("#overview").hidden = true;
  document.querySelector("#detail").hidden = false;
  text(document.querySelector("#detail-title"), `Unmatched ${side === "source" ? "source" : "lifted"} blocks`);
  text(document.querySelector("#detail-meta"), `${armIds.length} unmatched ARM IDs`);
  text(document.querySelector("#source-title"), `Original CFG (${DATA.source.arch})`);
  text(document.querySelector("#target-title"), `Lifted CFG (${DATA.target.arch})`);
  renderGraph("source", side === "source" ? DATA.report.unmatched_source_blocks : []);
  renderGraph("target", side === "target" ? DATA.report.unmatched_target_blocks : []);
  renderRelationPicker();
  renderInstructions();
}
function blockLevels(blocks, edges) {
  const ids = new Set(blocks.map(block => block.id));
  const outgoing = new Map(blocks.map(block => [block.id, []]));
  const incoming = new Map(blocks.map(block => [block.id, 0]));
  for (const edge of edges) {
    if (!ids.has(edge.source) || !ids.has(edge.target)) continue;
    outgoing.get(edge.source).push(edge.target);
    incoming.set(edge.target, incoming.get(edge.target) + 1);
  }
  const queue = blocks.filter(block => incoming.get(block.id) === 0).map(block => block.id);
  if (!queue.length && blocks.length) queue.push(blocks[0].id);
  const level = new Map(queue.map(id => [id, 0]));
  for (let i = 0; i < queue.length; ++i) {
    const source = queue[i];
    for (const target of outgoing.get(source)) {
      if (!level.has(target)) {
        level.set(target, level.get(source) + 1);
        queue.push(target);
      }
    }
  }
  let fallback = Math.max(0, ...level.values()) + 1;
  for (const block of blocks) if (!level.has(block.id)) level.set(block.id, fallback++);
  return level;
}
function renderGraph(side, allowedIds) {
  const root = document.querySelector(`#${side}-graph`);
  root.replaceChildren();
  const allowed = new Set(allowedIds);
  const blocks = DATA[side].blocks.filter(block => allowed.has(block.id));
  const edges = DATA[side].edges.filter(edge => allowed.has(edge.source) && allowed.has(edge.target));
  const levels = blockLevels(blocks, edges);
  const columns = new Map();
  for (const block of blocks) {
    const level = levels.get(block.id);
    if (!columns.has(level)) columns.set(level, []);
    columns.get(level).push(block);
  }
  const positions = new Map();
  let maxY = 0;
  for (const [level, column] of columns) {
    column.forEach((block, index) => {
      const position = { x: 24 + level * 240, y: 24 + index * 94 };
      positions.set(block.id, position);
      maxY = Math.max(maxY, position.y + 72);
    });
  }
  const width = Math.max(440, 80 + (Math.max(0, ...levels.values()) + 1) * 240);
  root.style.width = `${width}px`;
  root.style.height = `${Math.max(360, maxY + 24)}px`;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "edge-layer");
  svg.setAttribute("width", width);
  svg.setAttribute("height", root.style.height);
  svg.innerHTML = `<defs><marker id="${side}-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#94a3b8"/></marker></defs>`;
  for (const edge of edges) {
    const from = positions.get(edge.source);
    const to = positions.get(edge.target);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const x1 = from.x + 198, y1 = from.y + 28, x2 = to.x, y2 = to.y + 28;
    path.setAttribute("d", `M ${x1} ${y1} C ${x1 + 30} ${y1}, ${x2 - 30} ${y2}, ${x2} ${y2}`);
    path.setAttribute("marker-end", `url(#${side}-arrow)`);
    svg.append(path);
  }
  root.append(svg);
  for (const block of blocks) {
    const position = positions.get(block.id);
    const button = document.createElement("button");
    button.className = "block-node";
    button.style.left = `${position.x}px`;
    button.style.top = `${position.y}px`;
    button.style.setProperty("--side", side === "source" ? "#1d4ed8" : "#c2410c");
    button.dataset.side = side;
    button.dataset.block = block.id;
    button.innerHTML = `<span class="block-id">${block.id}</span><span class="block-summary">${block.instructions.length} instructions, ${idsForBlock(side, block).length} ARM IDs</span>`;
    button.addEventListener("click", () => selectBlock(side, block.id));
    root.append(button);
  }
  updateHighlights();
}
function selectBlock(side, id) {
  const selected = { side, id };
  const relation = state.selection && state.selection.side !== side
    ? relationForBlocks(state.selection, selected)
    : null;
  state.selection = selected;
  state.relation = relation;
  updateHighlights();
  renderRelationPicker();
  renderInstructions();
}
function updateHighlights() {
  document.querySelectorAll(".block-node").forEach(node => {
    node.classList.remove("selected", "related", "paired");
    if (!state.selection) return;
    if (node.dataset.side === state.selection.side && node.dataset.block === state.selection.id) {
      node.classList.add("selected");
      return;
    }
    if (state.relation &&
        node.dataset.side !== state.selection.side &&
        ((node.dataset.side === "source" && node.dataset.block === state.relation.source_block) ||
         (node.dataset.side === "target" && node.dataset.block === state.relation.target_block))) {
      node.classList.add("paired");
      return;
    }
    if (node.dataset.side !== state.selection.side &&
        relatedBlocks(state.selection.side, state.selection.id).has(node.dataset.block)) {
      node.classList.add("related");
    }
  });
}
function sameRelation(left, right) {
  return left && right &&
    left.source_block === right.source_block &&
    left.target_block === right.target_block;
}
function selectRelation(relation) {
  state.relation = relation;
  updateHighlights();
  renderRelationPicker();
  renderInstructions();
}
function renderRelationPicker() {
  const title = document.querySelector("#relation-title");
  const picker = document.querySelector("#relation-picker");
  picker.replaceChildren();
  if (!state.selection) {
    text(title, "Select a block to inspect relation pairs");
    return;
  }
  const relations = relationsForBlock(state.selection.side, state.selection.id);
  text(
    title,
    `Relation pairs for ${state.selection.side === "source" ? "original" : "lifted"} block ${state.selection.id}`,
  );
  for (const relation of relations) {
    const button = document.createElement("button");
    button.className = `relation-pair${sameRelation(relation, state.relation) ? " selected" : ""}`;
    text(
      button,
      `${relation.source_block} <-> ${relation.target_block} (${relation.arm_inst_ids.length} ARM IDs)`,
    );
    button.addEventListener("click", () => selectRelation(relation));
    picker.append(button);
  }
}
function renderInstructions() {
  const correlationTitle = document.querySelector("#correlation-title");
  const correlationList = document.querySelector("#correlation-list");
  const title = document.querySelector("#instruction-title");
  const list = document.querySelector("#instruction-list");
  const related = document.querySelector("#related-list");
  correlationList.replaceChildren();
  list.replaceChildren();
  if (!state.selection) {
    if (state.unmatchedIds && state.unmatchedIds.length) {
      text(correlationTitle, "No direct target instruction correspondence");
      addCorrelationRow(correlationList, "ARM ID", "Original", "Lifted", true);
      addCorrelationRow(
        correlationList,
        "Unmatched",
        `${state.unmatchedIds.length} ARM IDs have no target block evidence.`,
        "No direct mapped target instruction.",
      );
      text(title, "Unmatched ARM instructions");
      const allSource = DATA.source.blocks.flatMap(block => block.instructions);
      for (const instruction of allSource.filter(insn => state.unmatchedIds.includes(insn.arm_inst_id))) {
        const row = document.createElement("div");
        row.className = "instruction";
        row.innerHTML = `<span class="instruction-id"></span><span></span>`;
        text(row.children[0], `ARM #${instruction.arm_inst_id}`);
        text(row.children[1], instruction.text);
        list.append(row);
      }
      text(related, "These instructions have no direct target block evidence.");
      return;
    }
    text(correlationTitle, "Select a relation pair to inspect DWARF provenance evidence");
    text(title, "Select a block to inspect instructions");
    text(related, "");
    return;
  }
  const block = blockMap(state.selection.side).get(state.selection.id);
  if (state.relation) {
    const correspondences = relationInstructionCorrespondence(state.relation);
    text(
      correlationTitle,
      `DWARF provenance evidence: ${state.relation.source_block} <-> ${state.relation.target_block}`,
    );
    addCorrelationRow(correlationList, "ARM ID", "Original", "Lifted", true);
    for (const correspondence of correspondences) {
      addCorrelationRow(
        correlationList,
        `ARM #${correspondence.armId}`,
        correspondence.source.join("\\n") || "(no instruction found)",
        correspondence.target.join("\\n") || "(no instruction found)",
      );
    }
  } else {
    text(correlationTitle, "Select a relation pair to inspect DWARF provenance evidence");
    addCorrelationRow(correlationList, "Relation pair", "Original", "Lifted", true);
    addCorrelationRow(
      correlationList,
      "Waiting",
      "Select a relation pair above or click a highlighted opposite block.",
      "The table is limited to that one relation pair.",
    );
  }
  text(title, `${state.selection.side === "source" ? "Original" : "Lifted"} block ${block.id}`);
  for (const instruction of block.instructions) {
    const row = document.createElement("div");
    row.className = "instruction";
    if (instruction.arm_inst_id !== null && instruction.arm_inst_id !== undefined) row.classList.add("mapped");
    const id = instruction.arm_inst_id === null || instruction.arm_inst_id === undefined
      ? instruction.address
      : `ARM #${instruction.arm_inst_id}`;
    row.innerHTML = `<span class="instruction-id"></span><span></span>`;
    text(row.children[0], id);
    text(row.children[1], instruction.text);
    list.append(row);
  }
  const opposite = [...relatedBlocks(state.selection.side, block.id)];
  text(related, opposite.length
    ? `Directly related ${state.selection.side === "source" ? "lifted" : "source"} blocks: ${opposite.join(", ")}`
    : "No direct block relation evidence.");
}
document.querySelector("#back").addEventListener("click", () => {
  resetDetail();
});
const functionSelect = document.querySelector("#function-select");
for (const functionName of FUNCTION_NAMES) {
  const option = document.createElement("option");
  option.value = functionName;
  text(option, functionName);
  functionSelect.append(option);
}
functionSelect.value = DATA.function;
functionSelect.addEventListener("change", () => selectFunction(functionSelect.value));
text(document.querySelector("#meta"), `${DATA.source.arch} -> ${DATA.target.arch}`);
renderOverview();
</script>
</body>
</html>
"""
    document = document.replace("__TITLE__", title).replace(
        "__DATA__", data_json
    ).replace("__DEFAULT_FUNCTION_JSON__", json.dumps(default_function))
    Path(out_path).write_text(document, encoding="utf-8")
    return out_path


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
    parser.add_argument(
        "--viewer",
        nargs="?",
        const="",
        metavar="HTML",
        help="Write an interactive self-contained HTML correlation viewer",
    )

    args = parser.parse_args()

    if args.relations_json and not args.asm_map:
        parser.error("--relations-json requires --asm-map")
    if args.source_object and not args.asm_map:
        parser.error("--source-object requires --asm-map")
    if args.source_object and not args.function:
        parser.error("--source-object requires --function")
    if args.viewer is not None and not args.source_object:
        parser.error("--viewer requires --source-object")

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
            model = build_combined_cfg_model(
                source_path,
                in_path,
                instruction_map,
                debug_file,
                args.function,
            )
            combined_path, report = render_combined_cfg_from_model(
                model,
                out_dir,
                source_path,
                in_path,
                args.fmt,
            )
        except (ValueError, RuntimeError) as error:
            parser.error(str(error))
        rendered = [combined_path]
        relation_reports = [report]
        print(f"Combined CFG: {combined_path}")
        if args.viewer is not None:
            viewer_path = (
                os.path.abspath(args.viewer)
                if args.viewer
                else os.path.join(
                    out_dir,
                    f"{Path(source_path).name}_vs_"
                    f"{Path(in_path).name}_{args.function}.html",
                )
            )
            Path(viewer_path).parent.mkdir(parents=True, exist_ok=True)
            viewer_path = render_interactive_viewer(
                model,
                str(Path(viewer_path).parent),
                source_path,
                in_path,
                viewer_path,
            )
            rendered = [viewer_path]
            print(f"Interactive viewer: {viewer_path}")

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
