# STREAM benchmark: crash on printf variadic argument marshalling

Status: `needs-triage`

## Problem

`arm-lifter` crashes with `SIGABRT` (assertion `"Invalid cast!"` in `CastInst::Create`) when lifting `printf` calls with floating-point arguments. The crash happens in `marshallArgs()` while processing variadic arguments for `printf` in the STREAM benchmark's `main()` function.

## Reproduction

```bash
cd tests/lift
uv run python dev.py lift stream
```

## Stack trace

```
Assertion failed: (castIsValid(op, S, Ty) && "Invalid cast!"), function Create, file Instructions.cpp, line 3040.

 #7 llvm::CastInst::Create(...)            libLLVMCore.dylib
 #8 lifter::mc2llvm::createBitCast(...)    mc2llvm.cpp
 #9 lifter::arm2llvm::marshallArgs(...)    arm2llvm.cpp
#10 lifter::arm2llvm::doCall(...)          arm2llvm.cpp
#11 lifter::mc2llvm::doDirectCall()        mc2llvm.cpp
```

The crash occurs when `printf` is called with `double` values using `%f` format specifiers. The variadic argument marshalling attempts an invalid bitcast, likely between incompatible types (e.g., float vs pointer-sized integer) when loading arguments from registers/stack.

## Impact

Blocks any test case that prints floating-point values via variadic functions (printf with `%f`/`%e`/`%g`). This affects STREAM and potentially other benchmark programs.

## Suspected cause

The data layout mismatch warning hints at a root cause:

```
warning: Linking two modules of different data layouts:
  src:  'e-m:e-p270:32:32-p271:32:32-p272:64:64-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32'
  lift: 'e-m:e-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32'
```

The source BC has `p270:32:32-p271:32:32-p272:64:64` (AArch64 ABI pointer address spaces) while the lifted module does not. This may cause the argument marshalling to misidentify register/stack slot types for variadic float arguments.

## Files involved

- `backend_tv/arm2llvm.cpp` — `marshallArgs()`, `doCall()`
- `backend_tv/mc2llvm.cpp` — `createBitCast()`
