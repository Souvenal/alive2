# 0002 — Shared Module for whole-object lifting

arm-lifter lifts every function in an ELF `.o` into a single `llvm::Module` (the "Shared Module"), rather than creating one Module per function as the upstream `backend_tv` did. This decision centralises global-variable definitions, enables cross-function direct calls in the lifted output, and eliminates the need for per-function `adjustSrc` (refinement) and per-function optimization.

## Decision

- A single `llvm::Module` and `LLVMContext` live for the entire lifetime of one `arm-lifter` invocation.
- `ObjectLiftContext` (`lifter_util/object_lift_context.h`) owns the Shared Module and the symbol-resolution policy.
- `importSrcBcGlobals()` is called once upfront: all globals from the src-bc module (initializers + types) are imported into the Shared Module; src-bc function bodies are stripped and only declarations are retained.
- Each function is lifted by `liftFuncToModule()` (`backend_tv/lifter.cpp`), which reuses the Shared Module instead of creating a fresh one. The lifter finds existing globals via `Module::getGlobalVariable()` and existing declarations via `Module::getFunction()`.
- Cross-function calls in the assembly (e.g. `bl foo`) resolve to the Shared Module's `foo` directly — no `extern fn` stubs.
- The final output is a single `.ll` file containing all lifted functions and all imported globals.

## Considered options

**Per-function module** (upstream alive2 / `backend_tv`):
- Each `liftFunc()` creates a new `LLVMContext` + `Module`
- Every global variable is a fresh truncated copy (only the bytes referenced by that function)
- Cross-function calls must use `extern` declarations — no direct-resolution calls
- `adjustSrc()` is required to align source and target IR for the refinement check
- Pro: clean module lifecycle, low memory per-function
- Con: no cross-function visibility, duplicated globals, requires TV machinery for output assembly

**Shared Module** (this branch):
- All functions in the Shared Module → cross-function calls resolve directly
- Globals have full initializers from src-bc — no truncation
- `adjustSrc()` is unnecessary (no refinement check) and is skipped
- Pro: output is a single self-contained `.ll`, trivial to recompile
- Con: all functions must succeed or none (`exit(-1)` on first failure), memory grows monotonically

## Why not per-function for arm-lifter

arm-lifter's goal is recompilation, not refinement checking. Per-function lifting would require the same shared-globals workarounds (a `ObjectLiftContext`-like mechanism) anyway, but would still produce separate Modules that need to be merged after lifting. The Shared Module eliminates the merge step entirely.

## Consequences

- The first function that fails `liftFuncToModule()` triggers an `exit(-1)` in `runLifter()` — partial output is not meaningful because globals and calls would be incomplete.
- `adjustSrc` and `fixupOptimizedTgt` are dead code in the arm-lifter path (skipped via the `ObjCtx == nullptr` check in `mc2llvm.cpp::run()`). They remain compiled in until issue 06 lands.
- Memory usage scales with the largest function's IR, not with total `.o` size. (The Module holds all lifted functions, but the largest function dominates LLVM IR memory — a 100-function `.o` still fits.)
- `backend_tv/lifter.cpp` exposes two APIs: `liftFunc()` (per-function Module) for upstream compatibility, and `liftFuncToModule()` (shared Module) for arm-lifter. The latter is the one this branch uses.
