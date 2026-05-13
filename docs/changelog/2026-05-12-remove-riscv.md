# Remove RISC-V code

**Date:** 2026-05-12
**Issue:** 13

## Summary

Removed all RISC-V-related code from the tree. RISC-V was never reachable in arm-lifter (which hard-rejects any arch besides aarch64) and was classified as removable dead code per ADR-0001.

## Deleted files

- `backend_tv/riscv2llvm.h`
- `backend_tv/riscv2llvm.cpp`
- `backend_tv/riscv2llvm_insns.cpp`
- `backend_tv/note_from_craig_topper.txt`
- `tests/riscv-tv/` (entire directory, ~157 test files)

## Modified files

- `CMakeLists.txt` — Removed 5 RISC-V LLVM components from `llvm_map_components_to_libnames`: `riscvasmparser`, `riscvcodegen`, `riscvdesc`, `riscvdisassembler`, `riscvinfo`
- `backend_tv/lifter.cpp` — Removed `#include "backend_tv/riscv2llvm.h"` and both `riscv64` backend branches in `liftFunc()` and `liftFuncToModule()`
- `backend_tv/arm2llvm.h` — Removed `#include "backend_tv/riscv2llvm.h"`
- `tools/backend-tv.cpp` — Removed `riscv64` backend initialization block; updated error message to "Only aarch64 is supported"
- `lifter_util/binary_reader.cpp` — Removed `riscv64` architecture detection in `getArchName()`
- `tests/lit/lit/formats/alive2test.py` — Removed `.riscv.ll` and `.riscvasm.ll` test file handling

## Verification

`cmake --build build --target arm-lifter` succeeds after removal.
