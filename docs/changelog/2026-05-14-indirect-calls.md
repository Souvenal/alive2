# Indirect calls (blr xN) support

**Date**: 2026-05-14
**Issue**: 09 (closed)

## Summary

Added fallback path in `arm2llvm::doIndirectCall()` for arm-lifter mode where no debuginfo mapping exists.

## Background

The `addDebugInfo()` function is only called in the legacy `liftFunc()` path, not in `liftFuncToModule()` (the arm-lifter path). This means `lineMap` is always empty, so `getCurLLVMInst()` returns null for all instructions. Direct calls (BL) already had a graceful fallback, but indirect calls (BR/BLR) did not — they exited with `"OOPS: no debuginfo mapping exists"`.

## Change

**`backend_tv/arm2llvm.cpp`**: When `getCurLLVMInst()` is null, the fallback scans `srcFn` for the first indirect `CallInst`, borrows its `FunctionType`, and creates the indirect call. If no indirect call is found in the source function, the original error still fires.

## Files changed

- `backend_tv/arm2llvm.cpp` — new fallback path in `doIndirectCall()`
- `tests/lift/cases/indirect_call.c` — added `main()` with `write()` output (no variadic functions)
- `tests/lift/test_lift.py` — `indirect_call` changed from `xfail` to `must_pass`

## Test

```bash
uv run pytest tests/lift -v -k indirect_call  # PASSED
```

Both ARM64 reference and lifted x86_64 binary produce identical output (`30 47`) and exit code (`77`).
