# Remove dead ASLP code

Status: `needs-triage`

## Problem

The ASLP (ARM Spec Language Processor) bridge code in `backend_tv/aslp/` and its integration points are guarded by `#ifdef BUILD_ASLP` and disabled by default. Since `arm-lifter` always runs with `ASLP=false` and the ASLP server is not used, this code is dead weight.

## What can be removed

- `backend_tv/aslp/` directory entirely
- `#ifdef BUILD_ASLP` blocks in `arm2llvm_insns.cpp`, `arm2llvm.h/cpp`, `mc2llvm.h`
- `bridge` library dependency in CMake
- `BUILD_ASLP` CMake option

The `lifter_interface_llvm` base class has already been moved from `backend_tv/aslp/interface.h` to `backend_tv/interface.h` so `mc2llvm` can compile without ASLP.

## Impact

Cleaner build, faster compile, less code to navigate.
