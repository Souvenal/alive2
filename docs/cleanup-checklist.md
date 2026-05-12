# Dead Code Cleanup Checklist (Phase 6.2)

## Background

This fork (`object-lifter` branch) only ships the `arm-lifter` tool. The
legacy `backend-tv` tool is no longer built — `CMakeLists.txt` only emits
`alive-tv`, `arm-lifter`, `quick-fuzz`, and `alive-exec`, and
`tools/backend-tv.cpp` is the **only** in-tree caller of
`lifter::liftFunc()`. Consequently, the entire non-`ObjCtx` (legacy) path
through `mc2llvm` is dead at runtime.

To prevent any accidental re-entry of the legacy path, the following
runtime guards have already been installed (see commit history):

| Location | Guard |
|---|---|
| [backend_tv/mc2llvm.cpp](../backend_tv/mc2llvm.cpp) `mc2llvm::run()` entry | `if (!ObjCtx) exit(-1)` with a clear error message |
| [backend_tv/lifter.cpp](../backend_tv/lifter.cpp) `lifter::liftFunc()` entry | unconditional `exit(-1)` with a clear error message |

These guards mean every call site listed below is either statically
unreachable (no caller in any built binary) or runtime-unreachable (path
guarded by `if (ObjCtx)` whose `else` branch can never run because
`ObjCtx` is now required to be non-null).

This document enumerates **every** piece of code that becomes safely
removable. **No deletions should happen without explicit user review of
each item below**, since a few entries (e.g. `riscv2llvm.h` include
chain, link-library impacts) have second-order effects.

Items are grouped by deletion granularity, from coarsest (whole files)
to finest (single `if` branches). Within each group, items are roughly
ordered by safety/independence — the earliest items have the fewest
downstream dependencies.

---

## 1. Whole files

| # | Path | Status | Notes |
|---|---|---|---|
| 1.1 | [tools/backend-tv.cpp](../tools/backend-tv.cpp) | Not built; only caller of `lifter::liftFunc()` | Safe to delete once items 2.x below are also removed. Single source of dependency on the legacy path. |
| 1.2 | [backend_tv/riscv2llvm.cpp](../backend_tv/riscv2llvm.cpp) | Not in `BACKEND_TV_SRCS` ([CMakeLists.txt:208-226](../CMakeLists.txt#L208)); contains ~98 method definitions | Already excluded from the build. The `.cpp` file is dead from the linker's perspective. |
| 1.3 | [backend_tv/riscv2llvm_insns.cpp](../backend_tv/riscv2llvm_insns.cpp) | Not in `BACKEND_TV_SRCS` | Same as above. |
| 1.4 | [backend_tv/riscv2llvm.h](../backend_tv/riscv2llvm.h) | Only `#include`d by [backend_tv/lifter.cpp:21](../backend_tv/lifter.cpp#L21) | Cannot be deleted until item 2.1 (`lifter::liftFunc`) is removed; that's the only translation unit using `riscv2llvm` symbolically. |

---

## 2. Function-level deletions in `backend_tv`

| # | Symbol | Declaration | Definition | Notes |
|---|---|---|---|---|
| 2.1 | `lifter::liftFunc()` | [backend_tv/lifter.h:43](../backend_tv/lifter.h#L43) | [backend_tv/lifter.cpp:75-114](../backend_tv/lifter.cpp#L75-L114) | Currently guarded by `exit(-1)` at entry. Removing it also lets us drop the `riscv2llvm.h` include and the `riscv2llvm` dispatch (only call site of the riscv lifter). |
| 2.2 | `mc2llvm::adjustSrc()` | [backend_tv/mc2llvm.h:1025](../backend_tv/mc2llvm.h#L1025) | [backend_tv/mc2llvm.cpp:1181](../backend_tv/mc2llvm.cpp#L1181) | Only called from `mc2llvm::run()`'s now-dead `else` branch (item 4.3). Required by Phase 6.2 plan. |
| 2.3 | `mc2llvm::fixupOptimizedTgt()` | [backend_tv/mc2llvm.h:1026](../backend_tv/mc2llvm.h#L1026) | [backend_tv/mc2llvm.cpp:1277](../backend_tv/mc2llvm.cpp#L1277) | Only called from `lifter::liftFunc()` (item 2.1). Required by Phase 6.2 plan. |

---

## 3. Constructor-level deletions

The "shared Module" (arm-lifter) path uses the second constructor of
`mc2llvm` and `arm2llvm`; the first constructor exists solely for the
legacy path.

| # | Symbol | Location | Notes |
|---|---|---|---|
| 3.1 | `mc2llvm::mc2llvm(...)` original ctor | [backend_tv/mc2llvm.h:95-112](../backend_tv/mc2llvm.h#L95-L112) | Creates a fresh `LiftedModule`, does not take `ObjCtx`. Only used by `lifter::liftFunc()` (item 2.1). Keep the second ctor at [mc2llvm.h:118-136](../backend_tv/mc2llvm.h#L118-L136). Required by Phase 6.2 plan. |
| 3.2 | `arm2llvm` original ctor (whichever one matches the legacy `mc2llvm` ctor) | [backend_tv/arm2llvm.h](../backend_tv/arm2llvm.h) (verify before deleting) | Same rationale as 3.1 — only the shared-Module overload should remain. |
| 3.3 | `riscv2llvm` constructors | [backend_tv/riscv2llvm.h](../backend_tv/riscv2llvm.h) | Subsumed by item 1.4 once the file is removable. |

---

## 4. `if (ObjCtx)` branches in `mc2llvm.cpp` (collapse to unconditional)

After items 2 and 3 are gone, `ObjCtx` is guaranteed non-null at runtime
(and the field itself can be migrated to a reference — see item 5.1).
The following `if (ObjCtx) { ... } else { ... }` blocks can then be
flattened to keep only the `if`-branch.

| # | Location | Current shape | Action |
|---|---|---|---|
| 4.1 | [backend_tv/mc2llvm.cpp:46-55](../backend_tv/mc2llvm.cpp#L46-L55) | `__stack_chk_fail` lookup wrapped in `if (ObjCtx)` | Remove the `if`; lookup is always safe. |
| 4.2 | [backend_tv/mc2llvm.cpp:71-85](../backend_tv/mc2llvm.cpp#L71-L85) | `lazyAddGlobal` ObjCtx-priority lookup block | Remove the surrounding `if (ObjCtx)`; the body becomes unconditional. |
| 4.3 | [backend_tv/mc2llvm.cpp:700-709](../backend_tv/mc2llvm.cpp#L700-L709) | `run()` chooses between extracting ABI metadata vs. calling `adjustSrc(srcFn)` | Delete the `else` branch (this is the load-bearing branch that uses item 2.2). Keep the metadata extraction unconditional. |
| 4.4 | [backend_tv/mc2llvm.cpp:759-765](../backend_tv/mc2llvm.cpp#L759-L765) | Reuse existing `@llvm.assert` declaration | Remove the surrounding `if (ObjCtx)`; the lookup short-circuit is always desired. |
| 4.5 | [backend_tv/mc2llvm.cpp:774-797](../backend_tv/mc2llvm.cpp#L774-L797) | `run()` chooses between reusing existing function declaration vs. unconditional `Function::Create(...)` | Delete the `else` branch. The shared-Module logic is the only correct one. |
| 4.6 | [backend_tv/mc2llvm.cpp:822-828](../backend_tv/mc2llvm.cpp#L822-L828) | Reuse existing `@myalloc` declaration | Remove the surrounding `if (ObjCtx)`. |
| 4.7 | [backend_tv/mc2llvm.cpp:894-903](../backend_tv/mc2llvm.cpp#L894-L903) | `if (!ObjCtx)` skip-condition for `verifyModule(*LiftedModule, ...)` | Delete the entire block. Module verification at this point is unsafe in shared-Module mode anyway (other functions may not be lifted yet). |

---

## 5. Field / type-level refactors (optional, follow-on)

Once items 3 and 4 land, `ObjCtx` is structurally non-null. The
following changes turn that runtime invariant into a compile-time one.

| # | Symbol | Location | Action | Notes |
|---|---|---|---|---|
| 5.1 | `mc2llvm::ObjCtx` field | [backend_tv/mc2llvm.h:85](../backend_tv/mc2llvm.h#L85) (`ObjectLiftContext *ObjCtx{nullptr}`) | Change to `ObjectLiftContext &ObjCtx` (reference member, mandatory ctor argument). | Eliminates the run-time guard at [mc2llvm.cpp:694-705](../backend_tv/mc2llvm.cpp#L694-L705). Touches every `ObjCtx->...` call site (rewrite as `ObjCtx.`). |
| 5.2 | `class ObjectLiftContext;` forward decl | [backend_tv/mc2llvm.h:34](../backend_tv/mc2llvm.h#L34) | Replace with `#include "lifter_util/object_lift_context.h"`. | Required by 5.1 because reference members need the complete type. |

---

## 6. CMakeLists.txt cleanup (after items 1.x land)

| # | Location | Action |
|---|---|---|
| 6.1 | (any future) `riscv*` references in [CMakeLists.txt:303](../CMakeLists.txt#L303) `llvm_map_components_to_libnames` (`riscvasmparser riscvcodegen riscvdesc riscvdisassembler riscvinfo`) | Drop these LLVM components once item 1.4 lands. They aren't otherwise needed. |

---

## 7. Out-of-scope items (don't touch yet)

These appear superficially related but should **not** be cleaned up as
part of this checklist:

- **`mc2llvm` itself depending on `alive2::` types** — [CLAUDE.md known-limitation #6](../CLAUDE.md) still says arm-lifter links against `ir`/`smt`/`Z3` because of these. A grep didn't find any direct `alive2::` mentions in `mc2llvm.cpp` / `mc2llvm.h` / `lifter.cpp`, so this limitation may already be obsolete — confirm independently before changing link libraries in CMakeLists.txt.
- **ASLP code** ([CLAUDE.md known-limitation #5](../CLAUDE.md)) — separately tracked; out of scope for this checklist.
- **`tv/` plugin and `tools/alive*.cpp` tools** — unrelated to the arm-lifter pipeline; do not touch.

---

## 8. Suggested deletion order

To keep each commit independently buildable, removing in this order
minimizes churn:

1. Items **1.1** and **2.1** (`backend-tv.cpp` + `lifter::liftFunc`) — together, since 1.1 is the only caller of 2.1.
2. Item **1.4** (`riscv2llvm.h`) — once nothing `#include`s it.
3. Items **1.2 + 1.3** (orphan `.cpp` files).
4. Items **2.2 + 2.3** (`adjustSrc`, `fixupOptimizedTgt`) — together, since they share callers.
5. Items **3.1 + 3.2 + 3.3** (legacy constructors) — together with 4.3 / 4.5 to keep `run()` consistent.
6. Items **4.1, 4.2, 4.4, 4.6, 4.7** — independent simple flattenings.
7. Items **4.3, 4.5** — done alongside step 5.
8. Items **5.1 + 5.2** — final reference-member migration.
9. Item **6.1** — CMake cleanup.
10. Remove the runtime guards introduced earlier (the `exit(-1)` blocks in `mc2llvm::run()` and `lifter::liftFunc()`).
