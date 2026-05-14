# Variadic functions (printf) support

**Date**: 2026-05-14
**Issue**: 03 (closed)

## Summary

Fixed variadic function calls (e.g., `printf`) in the arm-lifter. Maze.c now lifts correctly and passes the full pipeline (lift → recompile → behavioral equivalence under QEMU).

## Background

The lifter previously hard-exited on variadic calls (`ERROR: varargs not supported`). Prior phases removed the early exit and added variadic argument marshalling in `marshallArgs()`, relying on a `variadicCallSites` map (populated during a `checkInstSupport` pre-pass on the source IR) to recover the correct variadic arg count.

Phase 3 (Maze.c) exposed a subtle bug: Maze has ~12 `printf` calls with 1–4 arguments each.

## Root cause

`variadicCallSites` was `std::unordered_map<std::string, CallInst*>` — a simple map that overwrites entries per callee name. Only the **last** CallInst per function survived. In Maze's `main()`, the final `printf` call was:
```c
printf("You lose\n");  // 1 argument (format string only)
```

Every prior `printf` call — including the critical `printf("Maze dimensions: %dx%d\n", 11, 7)` — looked up this 1-arg source CallInst and got **zero** variadic arguments. The recompiled x86_64 produced garbage (`Maze dimensions: -2080376496x0`).

## Changes

### `backend_tv/mc2llvm.cpp:1027` — keep max-args CallInst

Changed `variadicCallSites[name] = ci` to only overwrite when the new entry has **more** arguments:

```cpp
auto it = variadicCallSites.find(name);
if (it == variadicCallSites.end() || ci->arg_size() > it->second->arg_size())
  variadicCallSites[name] = ci;
```

This ensures `variadicCallSites["printf"]` points to the call site with the most args (e.g., `printf(@.str, i32, i32, ptr)` with 4 args). Extra variadic args passed to `printf` are safe — C99 §7.19.6.1: excess arguments are evaluated but otherwise ignored.

### `backend_tv/arm2llvm.cpp:1242-1248` — don't truncate variadic scalar args

The stored max-args CallInst may have types `[i32, i32, ptr]` at variadic positions, but an actual call site (e.g., `printf("<%42s>", program)`) passes `[ptr]`. Truncating X1 from a 64-bit pointer to 32-bit `i32` would corrupt the pointer. The fix reads all variadic scalar/pointer args as `i64` (full register width) without truncation or `inttoptr`:

```cpp
// Don't truncate/inttoptr variadic scalar args — the source
// CallInst may be from a different call site with mismatched
// types. i64 is safe: printf reads per format string specifiers.
```

Non-variadic (fixed) params are unaffected — they use the authoritative `FunctionType` from the callee.

## Files changed

- `backend_tv/mc2llvm.cpp` — max-args entry in `checkInstSupport`
- `backend_tv/arm2llvm.cpp` — i64-only variadic arg marshalling
- `tests/lift/test_lift.py` — `Maze` changed from `xfail` to `must_pass` (prior session)
- `tests/lift/cases/Maze.stdin` — test input with 18-move solution

## Test

```bash
uv run pytest tests/lift -v   # 7 passed, 2 xfailed (init_fini_sections, pgo_sections)
```
