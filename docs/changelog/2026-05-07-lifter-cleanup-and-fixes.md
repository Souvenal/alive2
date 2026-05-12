# 2026-05-07 — lifter cleanup pipeline (caller-saved register change reverted)

## Problem

Shared module output was 9066 lines vs per-function output 646 lines. Root cause: `liftFuncToModule()` skipped `optimize_module()` (lifter.cpp:153 TODO), so register init boilerplate (132 × alloca i64 + 128 × alloca i128 = 260 lines per function of freeze/store/load) was never cleaned up.

Running `llvm_util::optimize_module(O3)` on the shared module cleaned the boilerplate but **broke semantic correctness**: O3's inliner inlined callees into callers, function-attrs inferred `noreturn` on functions that call `exit()`, and simplifycfg inserted `unreachable` after the first write call, deleting all subsequent game logic. The lifted binary segfaulted at runtime.

Further investigation also attempted to expand caller-saved register invalidation (X9-X15 → X0-X18), but this was **reverted** — original author likely had a specific reason for limiting to X9-X15. The original range caused no observable correctness issues in testing. See §4.

Also `instcombine` was found to delete all calls to defined functions (e.g. `call @print_2d_int`, `call @draw`) from `main`, leaving only `call @write`. Root cause not fully determined, but likely interaction with `fastcc` calling convention and function body visibility.

## Changes

### 1. NEW: `lifter_util/lifter_cleanup.h` + `lifter_util/lifter_cleanup.cpp`

Safe cleanup passes for lifted IR. Runs `function(mem2reg,dce,simplifycfg),globaldce` — no inlining, no IPO, no function-attrs inference. Preserves call structure.

```cpp
namespace lifter {
void cleanup_module(llvm::Module &M);
}
```

- `mem2reg` promotes register allocas (X0-X30, Q0-Q31, SP, FP, NZCV) to SSA, eliminating freeze/store/load boilerplate
- `dce` removes dead instructions (unused register stores/loads)
- `simplifycfg` merges empty basic blocks (SEH_Nop artifacts), cleans up dead branches after `call @exit()`
- `globaldce` removes unused data sections
- **NOT** included: `instcombine` (deletes defined function calls), `function-attrs` (infers noreturn incorrectly)

### 2. `tools/arm-lifter.cpp`

- Added `#include "lifter_util/lifter_cleanup.h"`
- Removed `#include "llvm_util/llvm_optimizer.h"` (no longer needed)
- Removed `--optimize-tgt` CLI option (no longer used)
- Removed `opt_optimize_tgt` argument from `liftFuncToModule()` call
- Added `lifter::cleanup_module(*SharedModule)` call after myalloc→alloca replacement

### 3. `backend_tv/lifter.h` + `backend_tv/lifter.cpp`

- Removed `std::string optimize_tgt` parameter from `liftFuncToModule()`
- Removed outdated TODO comment about per-function optimization

### 4. ~~`backend_tv/arm2llvm.cpp:1202-1203`~~ (REVERTED)

Attempted to fix caller-saved register invalidation by expanding range from X9-X15 to X0-X18. **Reverted** — original author likely had a reason for X9-X15 only. Testing showed no observable issue with X0-X8 left uninvalidated (return value handling covers X0; X1-X8 may be handled elsewhere or not used post-call in practice). Leaving as-is until concrete reproducer found.

```cpp
// Unchanged (original code):
for (unsigned reg = 9; reg <= 15; ++reg)
    invalidateReg(AArch64::X0 + reg, 64);
```

### 5. `CMakeLists.txt`

Added `lifter_util/lifter_cleanup.cpp` to `LIFTER_UTIL_SRCS`.

## Result

| Metric | Before | After |
|--------|--------|-------|
| Lines | 9066 | 1736 |
| alloca i64 | 132 | 15 |
| alloca i128 | 128 | 0 |
| `br i1 poison` | 15 | 0 |
| Broken `unreachable` | 1 | 0 (2合法) |
| Defined function calls | 0（全被删） | 11 |
| `ret i32` in main | 0 | 1 |

15 remaining `alloca i64` are actively used registers (SP, FP, X1, X21, X28 etc.) that are loaded/stored across basic blocks — mem2reg cannot promote them, and this is correct behavior.
