# Remove RISC-V code

Status: `ready-for-agent`

## What to build

Remove all RISC-V-related code from the tree. RISC-V code in `backend_tv/` is never reachable (arm-lifter hard-rejects any arch besides `aarch64`) and is classified as removable dead code per ADR-0001.

End-to-end: delete the RISC-V source files, remove CMake's RISC-V LLVM components, excise all `riscv64` branches from C++ code, and verify the build completes cleanly for the arm-lifter target.

## Acceptance criteria

- [ ] `backend_tv/riscv2llvm.cpp`, `riscv2llvm.h`, and `riscv2llvm_insns.cpp` are deleted from the tree
- [ ] `#include "backend_tv/riscv2llvm.h"` is removed from `backend_tv/lifter.cpp` and `backend_tv/arm2llvm.h`
- [ ] All `backend == "riscv64"` branches in `backend_tv/lifter.cpp` are removed (lines 95-96, 138-141)
- [ ] All `backend == "riscv64"` branches in `tools/backend-tv.cpp` (lines 396-409) are removed
- [ ] RISC-V LLVM components (`riscvasmparser`, `riscvcodegen`, `riscvdesc`, `riscvdisassembler`, `riscvinfo`) are removed from the `llvm_map_components_to_libnames` call in `CMakeLists.txt`
- [ ] `docs/changelog/` entry is added documenting the removal (per ADR-0001)
- [ ] `cmake --build build --target arm-lifter` succeeds

## Blocked by

None - can start immediately
