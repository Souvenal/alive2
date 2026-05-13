# Strip ARM-specific `target_memN` attributes from lifted IR

**Date**: 2026-05-14
**Issue**: 17 (closed)

## Summary

When source C code is compiled with `-O1` (or above), the AArch64 backend annotates function `memory()` attributes with target-specific memory location names (`target_mem0`, `target_mem1`). These propagate into the lifted `.ll` output via the source bitcode function declarations, causing x86 recompilation to fail.

## Background

`zig cc -O1 -target aarch64-linux` produces bitcode where functions carry:

```
attributes #3 = { ... memory(read, inaccessiblemem: none,
                              target_mem0: none, target_mem1: none) ... }
```

`target_mem0` / `target_mem1` are AArch64-backend-specific and unknown to the x86 parser. O0 is unaffected since the backend skips the relevant analysis.

## Change

**`tools/arm-lifter.cpp`**: After stripping `NoCreateUndefOrPoison`, added a second cleanup pass that promotes `target_mem0`/`target_mem1` locations to the aggregate `Other` ModRef value. The `.ll` printer only omits locations whose ModRef matches `Other`, so promoting rather than zeroing them is essential — `getWithoutLoc()` sets NoModRef, which would still be printed.

**`tests/lift/conftest.py`**: Changed default `CFLAGS` from `-O0` to `-O1` to exercise optimizations that trigger ARM-specific attributes.

## Files changed

- `tools/arm-lifter.cpp` — new `target_memN` cleanup loop in `runLifter()`
- `tests/lift/conftest.py` — default CFLAGS: `-O0` → `-O1`

## Test

```bash
uv run pytest tests/lift -v -k struct_test  # PASSED (was failing at -O1 before fix)
uv run pytest tests/lift -v                # Full suite (known xfails unchanged)
```

## Related

Issue #04 — `nocreateundeforpoison` workaround, same class of problem (backend-specific attributes in output IR).
