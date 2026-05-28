# Symbol-level compatibility: all strings packed into 1–2 large globals, no individual `@.str.N` globals

Status: `closed`
Category: `enhancement`
Resolution: Resolved by commits 1faf9a81, 38776342, 6c8e7957, 02a4fae3

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

## Approach: One-step offset-based direct global resolution

**Key insight: The split-then-match two-step approach was cargo-culted from the `__sec_N` intermediate representation. There is no need to materialize one MCGlobal per string — the existing `@.str.N` globals already exist in the shared Module (imported by `importSrcBcGlobals()`). mc2llvm can return them directly whenever a text instruction references a matching `(section, offset)` pair.**

The root cause of the problem is simple: mc2llvm doesn't know which source BC global matches which `(section, offset)` pair referenced by text instructions. The fix is to tell it — via a precomputed lookup table — not by splitting data sections in obj2asm.

### How it works (data flow)

```
Old flow (Approach C):
  obj2asm → emitLabel() at every offset → many small MCGlobals → mc2llvm matches → renames → final globals
                                                                   ^^^^^^ waste

New flow (one-step):
  obj2asm (unchanged, blob behavior)          ─→ mc2llvm resolves __sec_5 + 0x2b
  ELF section bytes + .rela.text offsets  ─┐     ┊
  src BC globals (importSrcBcGlobals)     ─┤ → precompute ┊ (section: offset) → @.str.N
                                           ┘     ┊
                                                 ↓
                                       getExprVar() checks map:
                                         found? → return @.str.4 directly
                                         not found? → GEP(i8, @__sec_5, i64 offset)
```

### Key advantages

| Concern | Old approach (C) | New approach (one-step) |
|---------|-----------------|------------------------|
| `obj2asm` changes | Must split at offsets, modify ELFSymbolizer, add DataAliasMap | **Zero changes** — keep current blob behavior |
| `__sec_N` splitting | Required 1 label per offset | **Not needed** — blob stays intact |
| DataAliasMap | Required for substring references | **Not needed** — substrings don't match any source BC global START, so they naturally fall through to GEP into blob |
| String matching | Must extract bytes from split MCGlobals and compare | Byte comparison done ONCE upfront using ELF raw section bytes |
| Source BC global creation | New globals created in lazyAddGlobal, then dead blob cleanup | Source BC globals **returned directly** — they're already in the shared Module with correct names, types, linkage |
| Unmatched strings | `__sec_N` fallback via blob global | Same: GEP into blob (no regression) |

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      arm-lifter.cpp                              │
│                                                                  │
│  ① importSrcBcGlobals(SrcModule)     → shared module has @.str.N │
│  ② buildGlobalOffsetMap(ELF, SrcMod) → (sec,off) → @.str.N map  │
│  ③ lift functions → mc2llvm checks map in getExprVar()           │
└─────────────────────────────────────────────────────────────────┘
```

**Phase 1: Precompute `(section, offset) → GlobalVariable*` map**

Called once in `arm-lifter.cpp::runLifter()`, after `importSrcBcGlobals()` and before any `liftFuncToModule()` calls:

```cpp
void ObjectLiftContext::buildGlobalOffsetMap(ObjectFile &ELF,
                                              Module &SrcModule) {
  // 1. Index source BC string globals by initializer bytes
  //    Iterate SrcModule.globals(), find those with ConstantDataSequential
  //    initializers of type [N x i8], extract bytes.
  //    → Map: byte_sequence → GlobalVariable*

  // 2. Get .rela.text relocations — section name + addend
  //    Use obj2asm::buildTextRelocMap() (already exists).
  //    For each (section_name, offset):
  //      Read bytes from section at that offset
  //      Look up in source BC byte map
  //      On match: store offsetGlobalMap[(section, offset)] = GlobalVariable*

  // 3. Unmatched offsets are untouched — they'll resolve via GEP into blob
}
```

**Phase 2: Store `__sec_N → section_name` mapping**

In `mc2llvm::lazyAddGlobal()`, when the blob global is created from MCGlobal data, the section name is available as `g.section`. Store it:

```cpp
// In lazyAddGlobal, inside the MCGlobal iteration loop:
if (ObjCtx) {
    ObjCtx->registerSectionLabel(name, g.section);
    // e.g., registerSectionLabel("__sec_5", ".rodata.str1.1")
}
```

**Phase 3: Resolve in `mc2llvm::getExprVar()`**

```cpp
pair<Value*, uint16_t> mc2llvm::getExprVar(const MCExpr *expr) {
    auto [name, specifier] = MCExprToName(expr);  // "__sec_5"
    int64_t addend = extractAddend(expr);           // 0x2b

    Value *globalVar = lookupGlobal(name);          // blob @__sec_5

    // ★ NEW: check if this (section, offset) matches a source BC global
    if (addend != 0 && ObjCtx) {
        string section = ObjCtx->getSectionLabel(name); // "__sec_5" → ".rodata.str1.1"
        auto *matched = ObjCtx->lookupGlobalAtOffset(section, addend);
        if (matched)
            return {matched, specifier};  // Return @.str.4 directly!
    }

    // Fallthrough: GEP into blob (for unmatched offsets and addend=0 references)
    if (addend != 0)
        globalVar = ConstantExpr::getGetElementPtr(getIntTy(8),
                    cast<Constant>(globalVar),
                    ConstantInt::get(getIntTy(64), addend));
    return {globalVar, specifier};
}
```

Files to modify:
| File | Change | Complexity |
|------|--------|------------|
| `lifter_util/object_lift_context.h/cpp` | Add `sectionLabelMap`, `offsetGlobalMap`, `registerSectionLabel()`, `buildGlobalOffsetMap()`, `lookupGlobalAtOffset()` | **Medium** (new methods, straightforward) |
| `backend_tv/mc2llvm.cpp` | `lazyAddGlobal()`: store section label; `getExprVar()`: check offsetGlobalMap | **Small** (5–10 lines each) |
| `tools/arm-lifter.cpp` | Call `buildGlobalOffsetMap()` after `importSrcBcGlobals()` | **Trivial** (1 line) |
| `lifter_util/obj2asm.h/cpp` | Expose `buildTextRelocMap()` for external callers (currently used internally by `convertFullAsm`) | **Trivial** (make public or expose via wrapper) |
| `tests/lift/test_lift.py` | Add structural IR assertions | **Small** |

### Edge cases: substring references (no DataAliasMap needed)

When compiler string merging creates a reference to `(section, offset)` where `offset` falls inside an existing string (not at its start), the byte match will **fail** — because the bytes starting at that offset don't match any source BC global's FULL initializer. These references naturally fall through to `GEP(i8, @__sec_blob, i64 offset)`, preserving correctness.

**DataAliasMap is eliminated entirely.** No alias mechanism needed, no special-case handling for string-interior offsets. The blob global acts as the fallback for any unmatched reference, including substring references.

### Impact on `__stack_chk_guard` prerequisite

The `__stack_chk_guard` spurious global (issue 19) is no longer a prerequisite. Since we're NOT doing content matching on MCGlobals, the synthetic `__stack_chk_guard` MCGlobal never participates in string matching — it's a BSS symbol with different section type, and the precomputed map only covers `.rodata` string offsets.

## Agent Brief

**Category:** enhancement
**Summary:** Recover individual `@.str.N` string globals by precomputing a `(section, offset) → GlobalVariable*` mapping from ELF section bytes and source BC initializers, eliminating the need for `__sec_N` splitting or DataAliasMap.

**Current behavior:**
Lifted IR packs all `.rodata` strings into 1–2 monolithic synthetic globals (e.g., `@__sec_5` with 131 bytes, `@__sec_8` with 177 bytes) using opaque `StructType` and `weak` linkage. Text code references individual strings via `GEP(i8, @__sec_N, i64 <offset>)`. While functionally correct, this breaks partial lifting — the lifted `.o` has different symbol names, count, types, and linkage than what the original compiler produced.

**Desired behavior:**
Lifted IR should contain individual string globals that match the originals, without any intermediate `__sec_N` splitting or DataAliasMap:
- Each `.rodata` string referenced by a text relocation becomes a direct reference to the existing source BC `GlobalVariable*` (e.g., `@.str.4`) in the shared Module
- Each global uses the source BC's `ArrayType` (not synthetic `StructType`) and `private unnamed_addr` linkage
- Strings without source BC matches gracefully fall back to `GEP(i8, @__sec_blob, i64 offset)` with no crash
- Substring references (compiler string merging) automatically fall through to GEP into the blob — no DataAliasMap needed
- `obj2asm` is completely unchanged — the blob behavior from issue 18 is preserved

**Key interfaces:**
- `ObjectLiftContext::buildGlobalOffsetMap(ObjectFile &, Module &)` — precomputes the `(section, offset) → GlobalVariable*` mapping from ELF section bytes and source BC global initializers. Called once before the lifting loop
- `ObjectLiftContext::registerSectionLabel(name, section)` — called by `lazyAddGlobal()` to record the `__sec_N → section_name` mapping (from the MCGlobal's `g.section` field)
- `ObjectLiftContext::lookupGlobalAtOffset(section, offset)` — queried by `getExprVar()` to find a source BC GlobalVariable* matching the referenced section+offset
- `mc2llvm::getExprVar()` — insert `(section, offset)` check before the existing GEP fallthrough
- `mc2llvm::lazyAddGlobal()` — add one line to register the section label when creating a blob global

**Acceptance criteria:**
- [ ] Maze's lifted IR contains individual `@.str.2`, `@.str.3`, etc. globals (not `@__sec_5` packed blob)
- [ ] Each string global uses `ArrayType` (from source BC) not `StructType`
- [ ] Each string global uses `private unnamed_addr` linkage (from source BC)
- [ ] All existing must-pass end-to-end tests pass unchanged (behavioral equivalence preserved)
- [ ] Strings without source BC matches fall back to `GEP(i8, @__sec_blob, i64 offset)` with no crash
- [ ] Substring references (unmatched offsets) automatically use GEP into blob — no DataAliasMap needed
- [ ] A structural pytest assertion validates the number, names, and types of string globals in the lifted IR

**Out of scope:**
- Non-string data sections (`.data`, custom sections) — keep existing contiguous blob behavior
- Mach-O format support
- General instruction count optimization (issue 21, issue 24)
- The `__stack_chk_guard` fix (no longer a prerequisite — BSS globals don't participate in string matching)
- Any changes to `obj2asm` — the blob behavior from issue 18 is preserved intact

> *This was generated by AI during triage.*

## Triage Notes

**Category:** enhancement
**State:** ready-for-agent

**Key decisions (maintainer, 2026-05-28):**
- **Architecture:** One-step offset-based direct global resolution. **No `__sec_N` splitting.** **No DataAliasMap.** `obj2asm` unchanged. Byte matching done UPFRONT using ELF section bytes + source BC initializers
- **DataAliasMap:** Eliminated. Substring references naturally fall through to GEP into blob — no alias mechanism needed
- **Issue 19 relationship:** After this issue, narrow issue 19 to remaining items (`__stack_chk_guard`). String name/type/linkage aspects subsumed by issue 20. `__stack_chk_guard` is no longer a prerequisite to issue 20
- **Test strategy:** Both behavioral (existing e2e QEMU tests) AND structural (pytest assertions on IR shape: count, names, types)

> *This was generated by AI during triage.*

## Relationship to other issues

- **Issue #18** (closed): Fixed the correctness bug (synthetic labels splitting strings). See `docs/changelog/2026-05-14-synthetic-labels-data-contiguity.md`. This issue addresses the fidelity gap that remains after that fix — all strings in 1–2 globals.
- **Issue #19**: Covers type/linkage/alignment quality regressions (`StructType` vs `ArrayType`, `weak` vs `private`). Approach B in this issue subsumes parts of #19 (if we match source globals by content, we get the correct type and linkage for free).
- **Issue #15**: Data section optimization. Touches the same code paths.

## Resolution

### Root cause

The problem spans two layers: **ELF discards per-string symbols** (all string constants are merged into `.rodata.str1.N` sections with no symbol table entries for individual strings), and **mc2llvm had no way to map a `(section, offset)` relocation back to a source BC `@.str.N` global**. The result: obj2asm emits each merged section as one monolithic `MCGlobal`, and mc2llvm creates a single `@__sec_N` blob with `StructType` — all string identity is lost.

### Solution: one-step offset-based resolution (no obj2asm changes)

**Key insight**: Instead of splitting the blob into per-string MCGlobals (which requires changing obj2asm and handling substring aliases), precompute a `(section, offset) → GlobalVariable*` mapping ONCE upfront by matching ELF raw section bytes at relocation addends against source BC global initializers.

**Implementation (4 commits):**

1. **`buildGlobalOffsetMap()`** (`object_lift_context.h/cpp`): Called once after `importSrcBcGlobals()`. Indexes source BC `@.str.N` globals (both `ConstantDataSequential` and `ConstantAggregateZero` zeroinitializer) by their byte content, then iterates `.rela.text` relocations and matches bytes at each section+addend position against the index. Populates `offsetGlobalMap[(section, offset)] = GlobalVariable*`.

2. **`registerSectionLabel()`**: In `lazyAddGlobal()`, when creating an MCGlobal blob, records `__sec_N → section_name` (e.g., `__sec_5 → .rodata.str1.1`) via `ObjCtx`.

3. **`getExprVar()` offset check**: Before the existing GEP fallthrough, checks if the current `(section, addend)` pair (including addend=0 for section base references) has a source BC global match. If found, returns it directly. Unmatched offsets naturally fall through to GEP into the blob.

4. **Dead blob cleanup**: MCGlobal blobs are created with `WeakAnyLinkage` so `globaldce` can remove them. An explicit post-cleanup loop deletes any zero-use `@__sec_N` globals that survived (belts-and-suspenders).

**Zero changes to `obj2asm`** — the blob behavior from issue 18 is preserved. Substring references automatically fall through to GEP (no DataAliasMap needed).

### Files modified

| File | Change |
|------|--------|
| `lifter_util/object_lift_context.h` | Add `sectionLabelMap`, `offsetGlobalMap`, 4 new methods |
| `lifter_util/object_lift_context.cpp` | Implement `buildGlobalOffsetMap()` (~70 LOC) |
| `backend_tv/mc2llvm.cpp` | `registerSectionLabel()` call in `lazyAddGlobal()` (1 line); offset lookup in `getExprVar()` (+12 lines); blob linkage changed to WeakAnyLinkage (1 line) |
| `tools/arm-lifter.cpp` | Call `buildGlobalOffsetMap()` after `importSrcBcGlobals()` (1 line); explicit zero-use blob deletion loop (+12 lines) |
| `tests/lift/test_lift.py` | `verify_lifted_globals()` structural assertions |

### Results

| Metric | Before | After |
|--------|--------|-------|
| Maze IR string globals | 1 blob (`@__sec_5`) + 1 blob (`@__sec_8`) | 7 `@.str.N` + 6 `@str` = 13 individual |
| Maze_novarargs IR string globals | 1 blob (`@__sec_11`) | 15 individual `@.str.N` |
| Blob `@__sec_` references | 6+ | **0** |
| String type | `StructType { i8, i8, ... }` | `[N x i8]` (ArrayType from source BC) |
| Linkage | `weak` (after cleanup) | `weak` (intentional for partial linking) |
| Alignment | section alignment (4) | original alignment from source BC (1) |
| All must-pass tests | 7/7 | 9/9 (no regressions) |

### Also resolves

- **Issue #25** (packed string globals → `.byte` bloat in assembly): Same root cause. Now that lifted IR uses `ArrayType`, llc emits proper `.asciz`/`.ascii` instead of 548 lines of `.byte`.
- **Issue #19** (name/type/alignment regressions): Name, type, and alignment aspects resolved. Only `__stack_chk_guard` remains — narrowed to that.

## Files involved

- `lifter_util/obj2asm.cpp` — `convertDataSections()` (line 499), `buildSectionOffsetLabels()` (line 116), `convertFullAsm()` (line 922)
- `backend_tv/streamerwrapper.h/cpp` — `MCGlobal`, `addConstant()`, `emitLabel()`, `emitBytes()`
- `backend_tv/mc2llvm.cpp` — `lazyAddGlobal()` (line 37), `lookupGlobal()` (line 200), `getExprVar()` (line 377)
- `lifter_util/object_lift_context.h/cpp` — `importSrcBcGlobals()` (line 15), `lookupGlobal()`
- `tests/lift/output/Maze.ll` — reference (original source IR)
- `tests/lift/output/Maze.lifted.ll` — current lifted output
