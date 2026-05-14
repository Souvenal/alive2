# Symbol-level compatibility: all strings packed into 1–2 large globals, no individual `@.str.N` globals

Status: `needs-triage`

## Problem

After the issue #18 fix (data contiguity), lifted IR packs all `.rodata` strings into 1–2 large synthetic globals:

**Original** (`Maze.ll`, from clang):
```llvm
@.str.2 = private unnamed_addr constant [24 x i8] c"Maze dimensions: %dx%d\0A\00"
@.str.3 = private unnamed_addr constant [24 x i8] c"Player position: %dx%d\0A\00"
@.str.4 = private unnamed_addr constant [18 x i8] c"Iteration no. %d\0A\00"
@.str.10 = private unnamed_addr constant [22 x i8] c"Your solution <%42s>\0A\00"
@.str.11 = private unnamed_addr constant [34 x i8] c"Iteration no. %d. Action: %c. %s\0A\00"
```

**Lifted** (`Maze.lifted.ll`, from arm-lifter):
```llvm
@__sec_5 = weak constant %0 { i8 ..., i8 ... }  ; 131 bytes, all .rodata.str1.1 strings
@__sec_8 = weak constant %1 { i8 ..., i8 ... }  ; 177 bytes, all .rodata.str1.4 strings
```

**Why this matters**: Partial lifting — lift one `.c` file, recompile the lifted IR to an `.o`, and link with other non-lifted `.o` files. This requires:
- **Same number of globals** — not packed into 1–2 merged globals
- **Same symbol names** — `@.str.4` not `@__sec_5+offset`
- **Same declarations** — `private unnamed_addr constant [N x i8]` not `weak constant %opaque_struct`

## Root cause analysis

The problem spans two layers: ELF (information loss) and lifter (no recovery).

### Layer 1: ELF discards per-string symbols

During compilation (clang/llc), individual `@.str.N` LLVM globals are codegen'd into mergeable string sections:

```
.rodata.str1.1  (1-byte aligned, 131 bytes): 6 strings concatenated
.rodata.str1.4  (4-byte aligned, 177 bytes): 6 strings concatenated
```

**All per-string symbol entries are stripped.** The ELF symbol table contains only section-level entries:

```
0000000000000000 l    d  .rodata.str1.1  0000000000000000 .rodata.str1.1
0000000000000000 l    d  .rodata.str1.4  0000000000000000 .rodata.str1.4
000000000000002b l       .rodata.str1.1  0000000000000000 $d        ; ARM mapping symbol
```

Text instructions reference individual strings exclusively via **section symbol + addend** relocations:

```
R_AARCH64_ADR_PREL_PG_HI21  .rodata.str1.1 + 0x2b
R_AARCH64_ADD_ABS_LO12_NC   .rodata.str1.1 + 0x2b
```

The addend `0x2b` is the byte offset of `"Maze dimensions: %dx%d\n"` within `.rodata.str1.1`. No string name survives into the ELF.

### Layer 2: `obj2asm` emits data sections as contiguous blobs with a single label

The flow:

1. **`buildSectionOffsetLabels()`** (`obj2asm.cpp:116`) scans `.rela.text` and creates synthetic label entries for each `(section_name, addend)` pair:
   ```
   (".rodata.str1.1", 0x00) → "__sec_0"
   (".rodata.str1.1", 0x09) → "__sec_1"
   (".rodata.str1.1", 0x2b) → "__sec_2"
   ...
   ```

2. **`convertDataSections()`** (`obj2asm.cpp:499`) writes data section bytes. After the #18 fix, only the **base label** (addend=0) is emitted — all non-zero-offset synthetic labels are suppressed to preserve data contiguity:
   ```cpp
   // line 647-653: suppress interior synthetic labels
   bool isSectionLabel = sectionLabels.count(synthIt->second);
   if (!isSectionLabel || offset == 0) {
       P->Streamer->emitLabel(Sym);
   }
   ```

3. **Result**: `emitLabel("__sec_5")` fires once at offset 0, then `emitBytes()` accumulates all 131 bytes of `.rodata.str1.1` into one `MCGlobal` named `__sec_5`. No interior labels split the data.

4. **`mc2llvm::lazyAddGlobal()`** creates a single `StructType { i8, i8, ..., i8 }` global for `__sec_5`.

5. **Text code references** use `GEP(i8, @__sec_5, i64 43)` to point to individual strings within the monolithic global.

### Why the source BC doesn't help (currently)

`ObjectLiftContext::importSrcBcGlobals()` (`object_lift_context.cpp:15`) imports ALL source globals — including `@.str.2`, `@.str.3`, etc. — into the shared Module via `llvm::Linker::linkInModule()`. They sit there with correct names, array types, and initializers.

But they are **never referenced**. The lifted code uses `__sec_N` names because that's what `obj2asm` creates. `lookupGlobal("__sec_5")` searches the shared Module by name — `"__sec_5"` doesn't exist there — so it falls through to MCGlobal-based creation. The source `@.str.4` is orphaned.

## What information IS available for recovery

| Layer | Available | Not available |
|-------|-----------|---------------|
| ELF relocations | Exact byte offsets of every referenced string via `.rela.text` addends | String names, types, lengths |
| ELF sections | Raw bytes of `.rodata.str1.1` / `.rodata.str1.4` | Boundaries between strings (except via null bytes and relocation offsets) |
| Source BC | `@.str.N` globals with names, `ArrayType`, initializer bytes | Mapping from `(section, offset)` → `@.str.N` |
| DWARF | `.debug_str` contains string contents; `.rela.debug_info` references section offsets | String-level DWARF DIEs for anonymous constants (compiler typically doesn't emit them) |

**Key insight**: The `.rela.text` addends ARE the string boundaries. Every distinct addend `(section, offset)` that appears in a text relocation points to the start of a string that code references. The gap between consecutive offsets gives the string length. The raw bytes at each offset can be matched against source BC global initializers.

For Maze.o, the `.rodata.str1.1` relocation offsets are:

| Addend | String content |
|--------|---------------|
| `0x00` | `"Blocked!\0"` |
| `0x09` | `"Iteration no. %d. Action: %c. %s\n\0"` |
| `0x2b` | `"Maze dimensions: %dx%d\n\0"` |
| `0x43` | `"Player position: %dx%d\n\0"` |
| `0x5b` | `"Iteration no. %d\n\0"` |
| `0x6d` | `"Your solution <%42s>\n\0"` |
| `0x82` | (end of section, no label needed) |

These correspond 1:1 with the original `@.str.N` globals from the source BC.

## Approaches

### Approach A: Split at relocation offsets in obj2asm (correctness-first)

During `convertDataSections()`, use the `.rela.text` addends to identify string boundaries. Emit each null-terminated string as its own `MCGlobal` (with its own `__sec_N` label) rather than packing all strings into one monolithic global.

**How**: In the string-detection loop (`obj2asm.cpp:722`), when a relocation offset falls at or near a string boundary, emit a label there. The tricky case is **substring references** — when a relocation points inside a string due to compiler string merging (e.g., `". "` at offset `0x54` inside `"Iteration no. "` at `0x48`). For these, record an alias `__sec_X → (parent_global, offset)` via the DataAliasMap mechanism (see issue #18 Phase 2 design).

**Pros**: Gives individual string globals; text references already use `__sec_N+offset` so minimal pipeline changes.
**Cons**: Still has synthetic `__sec_N` names; doesn't recover original types.

### Approach B: Byte-content matching against source BC (name recovery)

In `mc2llvm::lazyAddGlobal()`, when creating a global from MCGlobal data, compare its byte content against source BC global initializers (already imported into the shared Module). If there's an exact byte-for-byte match, use the source global's name, type, and linkage instead of creating a new synthetic one.

**How**: After `importSrcBcGlobals()` imports source globals, iterate them and extract their initializer bytes. For each MCGlobal being lifted, scan the byte content against this map. When a match is found, return the existing source `GlobalVariable*` instead of creating a new one.

**Pros**: Recovers original names and array types; leverages existing infrastructure.
**Cons**: Substring matching is ambiguous (short strings like `"on"` match in many places); MCGlobals with embedded `OffsetSym` (relocations) can't be compared byte-for-byte; requires MCGlobals to already be split per-string (depends on Approach A).

### Approach C: Combined — split then match

1. **In obj2asm**: Split `.rodata` sections into per-string MCGlobals using relocation offsets (Approach A). Each becomes a separate `MCGlobal` with a `__sec_N` name.
2. **In mc2llvm**: Match each split MCGlobal's byte content against source BC globals (Approach B). For matched globals, use the source name and type. For unmatched globals (e.g., runtime-only strings), keep the synthetic name.

**This is the recommended approach.** It gives:
- Individual string globals (not packed)
- Original names and array types (from source BC)
- Graceful fallback for strings without source BC matches

### DataAliasMap for substring references

Compiler string merging can create relocation references into the middle of a string. For example, `". "` is both the suffix of `"Iteration no. "` and a standalone string. The ELF may have relocations pointing to the middle of a merged string. The DataAliasMap (`alias_name → (parent_global, byte_offset)`) records these interior references so they resolve to `GEP(parent, offset)` instead of creating split globals.

## Relationship to other issues

- **Issue #18** (closed): Fixed the correctness bug (synthetic labels splitting strings). See `docs/changelog/2026-05-14-synthetic-labels-data-contiguity.md`. This issue addresses the fidelity gap that remains after that fix — all strings in 1–2 globals.
- **Issue #19**: Covers type/linkage/alignment quality regressions (`StructType` vs `ArrayType`, `weak` vs `private`). Approach B in this issue subsumes parts of #19 (if we match source globals by content, we get the correct type and linkage for free).
- **Issue #15**: Data section optimization. Touches the same code paths.

## Files involved

- `lifter_util/obj2asm.cpp` — `convertDataSections()` (line 499), `buildSectionOffsetLabels()` (line 116), `convertFullAsm()` (line 922)
- `backend_tv/streamerwrapper.h/cpp` — `MCGlobal`, `addConstant()`, `emitLabel()`, `emitBytes()`
- `backend_tv/mc2llvm.cpp` — `lazyAddGlobal()` (line 37), `lookupGlobal()` (line 200), `getExprVar()` (line 377)
- `lifter_util/object_lift_context.h/cpp` — `importSrcBcGlobals()` (line 15), `lookupGlobal()`
- `tests/lift/output/Maze.ll` — reference (original source IR)
- `tests/lift/output/Maze.lifted.ll` — current lifted output
