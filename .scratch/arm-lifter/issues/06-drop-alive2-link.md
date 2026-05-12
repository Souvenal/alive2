# Drop unnecessary Alive2 library linkage

Status: `needs-triage`

## Problem

`arm-lifter` still links against `ir`, `smt`, and `Z3` because some `backend_tv` helper code in `mc2llvm.cpp` uses `alive2::` types (e.g., `alive2::util::...`). However, the shared Module path (`liftFuncToModule` + `ObjCtx != nullptr`) skips `adjustSrc()` and `fixupOptimizedTgt()`, so the refinement check and optimization are never executed at runtime.

## What to do

Extract or replace the remaining `alive2::` type usage from `mc2llvm.cpp`, then drop the `ir`/`smt`/`Z3` link from the `arm-lifter` target in CMake.

## Impact

Faster link times, smaller binary, no dependency on Z3 for a tool that doesn't use SMT solving.

## Files involved

- `backend_tv/mc2llvm.cpp` — find and replace `alive2::` usages
- `CMakeLists.txt` — `arm-lifter` target link libraries
