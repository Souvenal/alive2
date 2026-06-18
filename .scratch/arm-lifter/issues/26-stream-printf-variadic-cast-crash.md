# STREAM benchmark: crash on printf variadic argument marshalling

Status: `needs-triage`

## Problem

`arm-lifter` crashes with `SIGABRT` (assertion `"Invalid cast!"` in `CastInst::Create`) when lifting `printf` calls with floating-point arguments. The crash happens in `marshallArgs()` while processing variadic arguments for `printf` in the STREAM benchmark's `main()` function.

## Root cause

Two issues found:

### 1. `readFromRegTyped` fails on FP read from wider backing register (FIXED)

`readFromRegTyped(Q0, double)` reads Q0 (128-bit backing) as `i128`, then tries `createBitCast(i128, double)` which LLVM rejects — bitcast requires equal bit widths.

**Fix** (`arm2llvm.cpp:1692-1715`): truncate the backing integer to the target type's bit width before bitcasting, when backing is wider than target.

### 2. Variadic CallInst selection heuristic (deferred)

`variadicCallSites` stores a single CallInst per callee (the one with most args). For `printf` with mixed variadic types (int + double), this may pick a CallInst with different types than the actual assembly instruction. With fix #1 this no longer crashes; marshalled values may differ but the lifter proceeds. A proper per-call-site matching scheme is deferred.

## Solution applied

- `readFromRegTyped()` now handles FP/vector reads from wider backing registers
- `variadicCallSites` max-arg heuristic unchanged

## Remaining work

- STREAM still xfail due to `__sec_23` resolution (separate issue — blob section with no relocation entry)
- Per-call variadic CallInst matching (corrects marshalled values for mixed-type variadic functions)

## Files changed

- `backend_tv/arm2llvm.cpp` — `readFromRegTyped()`: trunc-then-bitcast for FP from wider backing

## Reproduction

```bash
cd tests/lift
uv run python dev.py lift stream
```
