# Fix: synthetic labels breaking data contiguity (Issue 18)

**Date**: 2026-05-14
**Issue**: 18 (closed)

## Summary

Fixed string and data corruption caused by synthetic `__sec_N` labels splitting contiguous section data into separate `MCGlobal` entries at independent LLVM addresses. `Maze_novarargs`, `Maze`, and `indirect_call` now pass the full pipeline.

## Background

`buildSectionOffsetLabels()` created synthetic `__sec_N` labels for every section+addend relocation in `.rela.text`. `convertDataSections()` called `emitLabel()` at each offset, causing the `MCStreamerWrapper` to flush accumulated bytes into a separate `MCGlobal` per label. Each `MCGlobal` became a distinct LLVM `GlobalVariable` at an independent address.

Functions accessed string data via ADRP+ADD with an immediate offset from the section base — but the base was a small fragment of the original section, and the offset read far past its boundary into adjacent (unrelated) globals.

## Changes

### `lifter_util/obj2asm.cpp`

**`buildSectionOffsetLabels()`**: Reduced to only create labels for section symbols (`.rodata.str1.1`), removing the `reloc.addend != 0` condition. Regular symbols with non-zero addends use `MCBinaryExpr` instead.

**`ELFSymbolizer::tryAddingSymbolicOperand()`**: For section symbols, reference the base (addend=0) synth label wrapped in `MCBinaryExpr(addend)` — produces `__sec_11+25` instead of `__sec_2`. For regular symbols with addend, uses `MCBinaryExpr` directly. Both resolve via GEP in `mc2llvm::getExprVar()`.

**`convertDataSections()`**: 
- Skip ARM mapping symbols (`$a`, `$d`, `$t`, `$x`) when building `secSymMap` — these created unwanted split points
- Skip `emitLabel()` for section-symbol labels at non-zero offsets — section data now flows contiguously into a single `MCGlobal` (the base label at offset 0)
- Emit string data through `P->Streamer->emitBytes()` instead of writing `.asciz` directly to `*P->OS` — the direct write bypassed the `formatted_raw_ostream` wrapper, interleaving labels at wrong positions in the assembly buffer

### `backend_tv/mc2llvm.cpp`

**`MCExprToName()`**: Handle `MCSpecifierExpr(MCBinaryExpr(...))` structure that the AArch64 MC parser produces (was previously missing — only handled `MCBinaryExpr(MCSpecifierExpr(...))`).

**`extractAddend()`**: Unwrap `MCSpecifierExpr` before checking for `MCBinaryExpr`, since the addend now lives inside the binary expression wrapped by the specifier.

## Verification

All O0–O3 and Os optimization levels pass:

```
tests/lift/test_lift.py::test_lift[Maze]              PASSED
tests/lift/test_lift.py::test_lift[Maze_novarargs]    PASSED
tests/lift/test_lift.py::test_lift[indirect_call]     PASSED
tests/lift/test_lift.py::test_lift[minirepro]         PASSED
tests/lift/test_lift.py::test_lift[struct_test]       PASSED
tests/lift/test_lift.py::test_lift[printf_complex]    PASSED
tests/lift/test_lift.py::test_lift[printf_minimal]    PASSED
tests/lift/test_lift.py::test_lift[init_fini_sections] XFAIL (known)
tests/lift/test_lift.py::test_lift[pgo_sections]      XFAIL (known)
```
