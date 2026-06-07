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
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


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
        'uncond': {'b'},
        'cond': {'b.eq','b.ne','b.gt','b.ge','b.lt','b.le',
                 'b.hi','b.hs','b.lo','b.ls',
                 'b.mi','b.pl','b.vs','b.vc',
                 'b.cc','b.cs',
                 'cbz','cbnz','tbz','tbnz'},
        'call': {'bl', 'blr'},
        'ret': {'ret'},
    },
    'x86-64': {
        'uncond': {'jmp'},
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


def objdump(path: str) -> tuple[str, list[dict]]:
    """Run llvm-objdump -d, return (arch, [insn dicts]).

    Each dict: {func, addr, raw, mnemonic, operands}
    """
    objdump = require_tool("llvm-objdump")
    r = subprocess.run(
        [objdump, "-d", "--no-show-raw-insn", path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        sys.exit(1)

    arch = 'x86-64'  # default
    insns = []
    current_func = None

    for line in r.stdout.splitlines():
        # File format → architecture detection
        m = RE_FILE_FMT.search(line)
        if m:
            arch = detect_arch(m.group(1))

        # Function header
        m = RE_FUNC_HEADER.search(line)
        if m:
            current_func = m.group(1)
            continue
        if current_func is None:
            continue

        # Instruction line
        m = RE_ADDR.match(line)
        if not m:
            continue
        addr = int(m.group(1), 16)

        # Split instruction text: mnemonic + operands
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
        })

    return arch, insns


def extract_target_addr(insn: dict, arch: str) -> int | None:
    """For branch instructions, extract the target address.  None if not a branch.

    Returns -1 for return instructions (sentinel).
    """
    prof = ARCH_PROFILES.get(arch, ARCH_PROFILES['x86-64'])
    mne = insn['mnemonic']
    ops = insn['operands']

    if mne in prof['ret']:
        return -1

    if mne in prof['uncond'] or mne in prof['cond']:
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

    blocks: list of (name, [insn_raw])   — raw instruction text per block
    edges:  list of (from_name, to_name, label)
    """
    if not insns:
        return [], []

    prof = ARCH_PROFILES.get(arch, ARCH_PROFILES['x86-64'])
    is_cond = prof['cond'].__contains__
    is_uncond = prof['uncond'].__contains__
    is_ret = prof['ret'].__contains__
    is_call = prof['call'].__contains__

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
        if (target == -1 or is_cond(insn['mnemonic'])) and idx + 1 < len(insns):
            targets.add(insns[idx + 1]['addr'])
        # For unconditional branches: no fallthrough in AArch64.  The next
        # instruction is NOT reachable from this branch; if another branch
        # targets it, it's already in `targets` via that branch.
        # Don't add it here — that would create an isolated dead-code block.

    # ── 2. Split instructions into blocks ──
    blocks = []   # [(name, [raw_insns])]
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
                parts = current[-1].split(None, 2)
                last_mne = parts[1].strip() if len(parts) >= 2 else ''
                finish_block(insn['addr'])
                if last_mne not in prof['uncond'] and last_mne not in prof['ret']:
                    edges.append((blocks[-1][0], f"L{insn['addr']:x}", ''))
            else:
                finish_block(insn['addr'])
            start_addr = insn['addr']

        current.append(insn['raw'])

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

        elif is_uncond(mne) and target is not None:
            finish_block()
            if blocks:
                edges.append((blocks[-1][0], f"L{target:x}", ''))

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
        for raw in binsns:
            print(f"  {raw}")
        outs = edge_map.get(name, [])
        if outs:
            print("  [edges]")
            for dst, label in outs:
                lbl = f" ({label})" if label else " (fallthrough)"
                print(f"    -> {dst}{lbl}")
        else:
            print("  [terminal — no outgoing edges]")


def handle_obj(path: str, out_dir: str, fmt: str, func_filter: str | None,
               stem: str | None = None, text: bool = False) -> list[str]:
    """Handle .o (or .s → .o): llvm-objdump → parse → dot.  Returns rendered paths."""
    arch, all_insns = objdump(path)
    funcs = collect_func_insns(all_insns)

    if stem is None:
        stem = Path(path).name
    rendered = []

    for func_name, insns in funcs.items():
        if func_filter and func_filter not in func_name:
            continue

        blocks, edges = build_cfg(insns, arch)
        if not blocks:
            print(f"  ~ {func_name}: no basic blocks found")
            continue

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
            body = '\\l'.join(esc(s) for s in binsns) + '\\l'
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

    return rendered


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Visualize CFG from .ll, .s, or .o files"
    )
    parser.add_argument("input", help="Path to .ll, .s, or .o file")
    parser.add_argument("-f", "--function", help="Only render this function (substring)")
    parser.add_argument("-o", "--output-dir", default="output/cfg",
                        help="Output directory (default: output/cfg)")
    parser.add_argument("--fmt", default="svg",
                        help="Output format (svg/png/pdf; default: svg)")
    parser.add_argument("--text", action="store_true",
                        help="Dump CFG as text instead of rendering an image")
    parser.add_argument("--open", action="store_true",
                        help="Open rendered image(s) (macOS)")

    args = parser.parse_args()

    in_path = os.path.abspath(args.input)
    if not os.path.isfile(in_path):
        print(f"Error: file not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    out_dir = args.output_dir
    os.makedirs(out_dir, exist_ok=True)

    ext = Path(in_path).suffix.lower()
    clean_obj = None  # temp file to clean up

    if ext == '.ll':
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
        rendered = handle_obj(clean_obj, out_dir, args.fmt, args.function,
                              stem=Path(in_path).name, text=args.text)

    elif ext == '.o':
        rendered = handle_obj(in_path, out_dir, args.fmt, args.function, text=args.text)

    else:
        print(f"Error: unsupported extension '{ext}' (use .ll, .s, or .o)", file=sys.stderr)
        sys.exit(1)

    if clean_obj and os.path.exists(clean_obj):
        os.unlink(clean_obj)

    if not rendered:
        sys.exit(0)

    if args.open:
        for img in rendered:
            subprocess.run(["open", img])
        print(f"Opened {len(rendered)} image(s).")


if __name__ == "__main__":
    main()
