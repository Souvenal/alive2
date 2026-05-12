# Indirect calls (`blr xN`) not supported

Status: `needs-triage`

## Problem

The lifter cannot handle indirect calls via function pointers, which compile to `blr <reg>` on AArch64. The BR/BLR handler in `arm2llvm.cpp` requires a DWARF debug info mapping from the register value to a target function, but this mapping does not exist for function-pointer calls (e.g., callbacks, vtables, state machines).

## Error

```
OOPS: no debuginfo mapping exists
Can't process BR/BLR instruction
```

Followed by `exit(-1)`.

## Impact

Blocks lifting projects that use function pointers heavily (e.g., Coremark's callback-driven state machine).

## Minimal reproducer

`tests/lift/indirect_call.c`

## Possible approaches

1. **Conservative**: model `blr` as an opaque call to an unknown function, preserving register clobber semantics
2. **Heuristic**: track function pointer assignments through registers and build a points-to set
3. **Analysis**: use existing analysis passes (e.g., SVF) to resolve indirect call targets from the LLVM bitcode

## Files involved

- `backend_tv/arm2llvm.cpp` — BR/BLR instruction handler
- `tests/lift/indirect_call.c` — minimal reproducer
