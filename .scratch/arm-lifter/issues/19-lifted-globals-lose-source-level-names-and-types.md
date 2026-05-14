# Lifted global variables lose source-level names and use struct types instead of arrays

Status: `needs-triage`

## Problem

Comparing `Maze.ll` (directly compiled) with `Maze.lifted.ll` reveals that lifted string globals are unnecessarily degraded:

**Original** (`Maze.ll`, line 7):
```llvm
@.str = private unnamed_addr constant [3 x i8] c"%c\00", align 1
```

**Lifted** (`Maze.lifted.ll`, line 24):
```llvm
@__sec_0 = weak constant %0 { i8 37, i8 99, i8 0 }, align 4
```

Four regressions:

| Aspect | Original | Lifted |
|--------|----------|--------|
| Name | `@.str` (LLVM auto-named) | `@__sec_0` (synthetic index) |
| Type | `[3 x i8]` (array) | `%0 { i8, i8, i8 }` (anonymous struct) |
| Linkage | `private unnamed_addr` | `weak` |
| Alignment | `align 1` | `align 4` (section alignment, not value alignment) |

This affects every string constant in the program — 15 globals in Maze alone (lines 24-39 of the lifted IR).

Additionally, a spurious `@__stack_chk_guard` global (line 26) is unconditionally emitted even when the original binary has no stack protector.

## Root cause

### 1. Struct types instead of array types

`mc2llvm.cpp:155` (`lazyAddGlobal()`) unconditionally constructs a `StructType` for every data global:

```cpp
auto *ty = StructType::create(tys);
auto initializer = ConstantStruct::get(ty, vals);
```

When all `RODataItem` elements are homogeneous `char` values, this should emit `ArrayType::get(getIntTy(8), size)` and `ConstantDataArray::get()` instead. The same applies for homogeneous integer arrays.

### 2. `__sec_N` naming

Synthetic labels originate from `obj2asm.cpp:131` and `obj2asm.cpp:935` to work around the lifter's inability to handle `MCBinaryExpr` (see issue #18). These names flow through:

```
obj2asm (__sec_N labels in assembly)
  → MCStreamerWrapper (MCGlobal with __sec_N name)
    → mc2llvm::lazyAddGlobal (LLVM GlobalVariable with __sec_N name)
```

There is no step that maps these synthetic names back to source-level names. However:

- **Source bitcode is already available**: `ObjectLiftContext::importSrcBcGlobals()` (`object_lift_context.cpp:15`) imports ALL globals from `--src-bc` into the shared Module, including string constants with their original names and types.
- **DWARF is available**: The ELF object contains `.debug_info` with `DW_TAG_variable` entries that map source-level variable names to addresses. The existing `binary_reader.cpp` already reads DWARF for function information.

The mapping problem is: given a `__sec_N` that resolves to `.rodata + 0x48`, how do we know it corresponds to `@.str.4` ("Iteration no. %d\n") in the source? DWARF info contains the address → name mapping for these globals.

### 3. `weak` linkage and missing `unnamed_addr`

`mc2llvm.cpp:158` hardcodes `ExternalLinkage`. Every data global gets `weak` (added elsewhere or by the assembly parser). Original IR uses `private unnamed_addr` — `unnamed_addr` allows LLVM to merge duplicate constants, and `private` reflects that these strings are internal to the compilation unit.

### 4. Spurious `__stack_chk_guard`

`streamerwrapper.h:88-96` unconditionally creates a `__stack_chk_guard` global in every `MCFunction`:

```cpp
MCFunction() {
  MCGlobal g{
      .name = "__stack_chk_guard",
      .align = llvm::Align(8),
      .section = ".rodata",
      .data = {'7', '7', '7', '7', '7', '7', '7', '7'},
  };
  MCglobals.push_back(g);
}
```

If no function references `__stack_chk_guard`, it becomes dead after `globaldce`. But when the program does use stack protector (or references the guard variable), this fake 8-byte value is wrong — the real guard value is a random canary set at process startup.

## Files involved

- `backend_tv/mc2llvm.cpp:101-163` — `lazyAddGlobal()`: struct type creation, linkage, alignment
- `backend_tv/streamerwrapper.h:88-96` — `MCFunction()`: hardcoded `__stack_chk_guard`
- `lifter_util/obj2asm.cpp:116-136,893-942` — synthetic label creation
- `lifter_util/object_lift_context.cpp:15-38` — `importSrcBcGlobals()` already imports source globals

## Improvement approaches

### Approach A: Emit ArrayType for homogeneous data (low effort, high impact)

In `lazyAddGlobal()`, before creating a `StructType`, check whether all `RODataItem` elements are `char` (or the same primitive integer type). If so, emit `ArrayType` + `ConstantDataArray` instead.

Fixes the type regression only (not names or linkage).

### Approach B: Use DWARF to recover global variable names (medium effort)

Extend `binary_reader.cpp` to parse `DW_TAG_variable` entries from `.debug_info`, building a map from `(section, offset)` or `address` → source-level name. Pass this map through the pipeline so `lazyAddGlobal()` can use the source name instead of `__sec_N`.

### Approach C: Match src-bc globals by data fingerprint (medium effort)

The src-bc already contains all globals with names and types. When the lifter creates a `__sec_N` global, compare its data bytes against each source global's initializer. If there's a byte-for-byte match, reuse the source global (name, type, linkage) instead of creating a new one.

### Approach D: Remove hardcoded `__stack_chk_guard` (low effort)

Make `__stack_chk_guard` creation conditional — only emit it when the lifted code actually references it. Or, better, import it from src-bc (which already has the correct declaration if stack protector is enabled).

### Approach E: Default to `private unnamed_addr` for non-exported globals

Non-symbol globals that don't appear in the ELF symbol table (like `__sec_N` strings) should use `private` linkage with `unnamed_addr`, matching the original IR.

## Relationship to issue #18

Issue #18 (closed) covered the **correctness** problem: synthetic labels splitting strings at arbitrary boundaries, causing undefined behavior at runtime. The fix was applied in `2026-05-14-synthetic-labels-data-contiguity.md`. This issue covers the **fidelity** problem: even when a global is correctly lifted (no splitting), its name, type, and linkage are unnecessarily degraded.

Fixing approach A (ArrayType) and D (stack_chk_guard) is independent of #18. Approaches B and C (name recovery) were enabled by the #18 fix and can proceed.
