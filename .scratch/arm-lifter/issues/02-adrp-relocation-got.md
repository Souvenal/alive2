# ADRP relocation symbolization: complex GOT patterns unsupported

Status: `needs-triage`

## Problem

Basic ADRP+LDR pairs are handled, but complex GOT (Global Offset Table) access patterns may not be correctly symbolized. This affects access to global variables and functions resolved through the dynamic linker.

## Impact

Programs with non-trivial global data access may fail to lift or produce incorrect IR with unresolved addresses.

## Files involved

- `lifter_util/obj2asm.cpp` — relocation symbolization
- `backend_tv/mc2llvm.cpp` — address materialization
