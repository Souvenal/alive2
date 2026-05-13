# Synthetic `__sec_N` labels break string and array data contiguity

Status: `needs-triage`

## Problem

`Maze_novarargs` (a `must_pass` case) produces different stdout between the reference ARM64 binary and the recompiled x86_64 binary. Two symptoms:

1. **String corruption**: `"Iteration no. 0"` → `"Iteration noPr0"` — the `. ` suffix of `"Iteration no. "` is missing, and `strlen` reads into the adjacent string `"Program the player..."`.

2. **Maze data corruption**: The maze border characters are shifted and interspersed with null bytes, because the draw() function accesses the maze via separate `@__sec_0` / `@__sec_1` globals instead of offsetting from `@maze`.

## Root cause

`lifter_util/obj2asm.cpp` uses synthetic labels (`__sec_N`) to replace all `symbol+addend` and `section+addend` references in the assembly output. This was designed to work around the lifter's inability to handle `MCBinaryExpr` (e.g., `maze+5`) — the lifter can only resolve simple `MCSymbolRefExpr`.

The `buildSectionOffsetLabels()` function creates these labels for all entries in `.rela.text` where `addend != 0` or the symbol starts with `.` (section symbols). The labels are placed at the correct offsets within the assembly's data sections. However, when this assembly is parsed and lifted to LLVM IR, the labels become separate global variables (`@__sec_N`) at independent addresses, breaking the implicit address relationship between the original symbol and the offset.

Two concrete failures:

### Failure 1: Mid-string section+addend references split string data

The `.rodata.str1.1` section contains null-terminated strings. Some are referenced via section+addend (e.g., `.rodata.str1.1 + 0x48` = `"Iteration no. "`). Others are sub-string references from merged strings (e.g., `.rodata.str1.1 + 0x54` = `". "`, which is the suffix of `"Iteration no. "` due to compiler string merging).

`buildSectionOffsetLabels` creates separate `__sec_N` labels for BOTH section+addend references. In `convertDataSections()`, the `.rodata.str1.1` data is split at offset 0x54:
- `__sec_4` at offset 0x48 = `"Iteration no"` (12 bytes, NO null terminator)
- `__sec_X` at offset 0x54 = `". \0"` (3 bytes)

When lifted to LLVM IR, `@__sec_4` is a 12-byte global without a null terminator. `print_str(@__sec_4)` calls `strlen()`, which reads past the global boundary into adjacent memory, yielding undefined behavior. In practice it reads into the next global (`@__sec_5` = `"Program..."`) producing `"Iteration noPr..."`.

### Failure 2: Global symbol+addend references create aliasing globals

The `draw()` function accesses `maze[0][5]` and `maze[1][0]` via `maze+5` and `maze+0xC` relocations. The ELFSymbolizer replaces these with `__sec_0` and `__sec_1` synthetic labels.

In the assembly output, `__sec_0` is correctly placed at maze+5 and `__sec_1` at maze+0xC. But when lifted to LLVM IR, they become independent globals:

```llvm
@maze = global [7 x [11 x i8]] [c"...", ...]         ; 77 bytes
@__sec_0 = global %0 { i8 45, i8 43, ... }            ; 7 bytes (maze+5)
@__sec_1 = global %1 { i8 32, i8 124, ... }           ; 65 bytes (maze+12)
```

In the lifted IR's text section, maze accesses use `@__sec_0` instead of a GEP into `@maze`. At runtime, `@__sec_0` is a separate 7-byte allocation at a different address — it does NOT point into `@maze`. The maze rendering is consequently corrupted.

## Reproduction

```bash
uv run pytest tests/lift -v -k Maze_novarargs
```

The test fails at the stdout comparison step with mismatched output.

## Detailed analysis

### .rodata.str1.1 section layout

```
Offset 0x00: "Your solution <\0"        (ref'd as .rodata.str1.1 + 0)
Offset 0x10: "Blocked!\0"               (.rodata.str1.1 + 0x10)
Offset 0x19: "Maze dimensions: \0"       (.rodata.str1.1 + 0x19)
Offset 0x2B: "Player position: \0"       (.rodata.str1.1 + 0x2B)
Offset 0x3D: ". Action: \0"             (.rodata.str1.1 + 0x3D)
Offset 0x48: "Iteration no. \0"         (.rodata.str1.1 + 0x48)
Offset 0x54: ". "                        (.rodata.str1.1 + 0x54 — substring via compiler merge)
Offset 0x57: "You lose\n\0"             (.rodata.str1.1 + 0x57)
Offset 0x90: "Program the player...\0"   (.rodata.str1.1 + 0x90)
...
```

The `". "` at offset 0x54 is inside `"Iteration no. "` (0x48-0x56). The compiler merged the trailing `". "` of `"Iteration no. "` with the standalone `". "` string used by `print_str(". ")`.

### .data section layout

```
Offset 0x00: maze[0..76] (77 bytes, global OBJECT)
  maze+5  → offset 5   (ref'd in text via ADRP + ADD)
  maze+12 → offset 0xC (ref'd in text via ADRP + ADD + LDR)
```

## Files involved

- `lifter_util/obj2asm.cpp` — `buildSectionOffsetLabels()` (line 116) creates labels for all non-zero-addend relocations; `convertDataSections()` (line 491) splits data at label boundaries; `ELFSymbolizer::tryAddingSymbolicOperand()` (line 229) replaces symbol+addend with synthetic labels in instruction operands
- `lifter_util/obj2asm.h` — `SectionOffsetLabels` type definition
- `backend_tv/mc2llvm.cpp` — `mapExprVar()` only handles `MCSymbolRefExpr`, requiring the synthetic label workaround

## Fix approaches

### Approach A: Don't create synthetic labels for global symbol+addend (e.g., `maze+5`)

Only create synthetic labels for section symbols (`.rodata.str1.1 + N`). For global symbols, keep the original symbol and use a different mechanism to express the addend (e.g., a GEP at the LLVM IR level).

This fixes Failure 2 but not Failure 1.

### Approach B: Mark the `__sec_N` data as overlapping with the parent global

In `mc2llvm.cpp`, when processing `__sec_N` labels whose corresponding addend targets a known global (like `maze`), emit the data as a GEP+bitcast into the parent global rather than as a separate allocation.

This is the most correct fix but requires the lifter to resolve the overlap relationship.

### Approach C: Don't split data at mid-string label boundaries in `convertDataSections()`

In `convertDataSections()`, when a synthetic label falls inside a detectable string region (preceded by printable ASCII and followed by a null terminator), defer the split and emit the full string as one `.asciz` directive.

This fixes Failure 1 but not Failure 2.

### Approach D: Emit overlapping globals using `llvm.used` or section annotations

Use LLVM's `@llvm.global.annotations` or section attributes to ensure `@__sec_N` globals are placed at the correct offset relative to their parent symbol, preserving the address relationship at link time.

Fragile, toolchain-dependent.

### Approach E: Broad overhaul — replace synthetic labels with proper reloc handling

Teach `mapExprVar()` (mc2llvm) to handle `MCBinaryExpr` and `MCExpr` variants, eliminating the need for synthetic labels entirely.

Most thorough fix but requires significant changes to `mc2llvm.cpp`.

## Related

Issue #15 — data section optimization, touches similar code paths.

Blocking issue: `Maze_novarargs` must pass before `must_pass` corpus target (#16) is complete.
