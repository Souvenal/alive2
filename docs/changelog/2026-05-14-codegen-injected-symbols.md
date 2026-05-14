# Codegen-injected runtime symbol ABI table

**Date**: 2026-05-14
**Issue**: 11 (closed)

## Summary

Added a hardcoded `name → FunctionType` table for compiler-rt builtins and other runtime symbols that the LLVM backend injects during IR→assembly lowering. These symbols appear in the `.o` but are absent from `src-bc`, previously causing the lifter to crash with "ERROR: global symbol '...' not found".

## Background

When the AArch64 backend lowers operations like `udiv i128`, it emits a call to a compiler-rt helper (e.g., `__udivti3`). This call appears in the assembly text (`.o` file) but the function declaration never appears in the bitcode (`.bc` file) because the frontend sees `udiv i128` as an IR instruction, not a function call. The lifter's `lazyAddGlobal()` had no way to resolve these symbols and crashed.

## Changes

**New files:**
- `lifter_util/codegen_runtime_abi.h` — declares `lookupRuntimeFunctionType()`
- `lifter_util/codegen_runtime_abi.cpp` — ~60 symbol → FunctionType entries, grouped by identical signatures

**Modified files:**
- `backend_tv/mc2llvm.cpp` — `lazyAddGlobal()` consults the ABI table before the fatal error
- `backend_tv/arm2llvm.cpp` — three i128 marshalling fixes required by the table's `i128(i128, i128)` types for `__*ti3` helpers
- `CMakeLists.txt` — added `codegen_runtime_abi.cpp` to `LIFTER_UTIL_SRCS`
- `tests/lift/cases/compiler_rt_int128.c` — new test case using `unsigned __int128` division
- `tests/lift/test_lift.py` — `compiler_rt_int128` classified as `must_pass`

### i128 marshalling (chain fix)

The ABI table correctly creates `i128(i128, i128)` for `__udivti3` / `__umodti3`, but `marshallArgs()` asserted `argTy->getIntegerBitWidth() <= 64`. On AAPCS64, 128-bit integers are passed in register pairs (X0:X1, X2:X3) and returned in X0 (lo) + X1 (hi). Three fixes:

1. **`marshallArgs()`**: i128 args read from two consecutive GPRs, combined as `(hi << 64) | zext(lo)`
2. **`doCall()` return**: i128 result split as `trunc(RV, i64)` → X0, `trunc(lshr(RV, 64), i64)` → X1
3. **`enforceSExtZExt()`**: handle integer types with width > 64 bits

## Test

```bash
uv run pytest tests/lift -v -k compiler_rt_int128  # PASSED
uv run pytest tests/lift -v                         # 8 passed, 2 xfailed (no regressions)
```

| Check | Result |
|-------|--------|
| Existing tests (Maze, struct_test, minirepro, etc.) | All pass |
| New `compiler_rt_int128` test | Lifts, recompiles to x86_64, QEMU output matches ARM64 ref |
| Lifted IR | `declare i128 @__udivti3(i128, i128)` — correct type |
