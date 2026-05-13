# ARM-specific `target_memN` in `memory()` attribute causes x86 recompile failure

Status: `needs-triage`

## Problem

When source C code is compiled with `-O1` (or above), the AArch64 backend annotates the `memory()` attribute with target-specific memory location names (`target_mem0`, `target_mem1`). These are valid LLVM IR for AArch64 but unrecognized by x86's parser.

The lifted `.ll` IR inherits these attributes from the source bitcode's function declarations. Subsequent recompilation to x86_64 fails with:

```
error: expected memory location (argmem, inaccessiblemem, errnomem) or access kind (none, read, write, readwrite)
```

## Root cause

`zig cc -O1 -target aarch64-linux` produces bitcode where some functions carry:

```
attributes #3 = { ... memory(read, inaccessiblemem: none,
                              target_mem0: none, target_mem1: none) ... }
```

`target_mem0` / `target_mem1` are AArch64-backend-specific memory location kinds defined in LLVM's `AArch64Base.td`. They track TLS and other target-specific memory regions. Only the AArch64 backend knows how to parse them.

arm-lifter copies function declarations (and their attribute groups) from the source Module into the shared output Module. These ARM-specific attributes are not stripped before output, causing parse failures when the IR is compiled for x86_64.

O0 is not affected because at O0 the backend does not perform the analysis required to emit these fine-grained memory location annotations; the `memory()` attribute stays at a coarse level (e.g., `memory(none)`, `memory(argmem: write)`).

This is structurally the same class of problem as Issue #04 (nocreateundeforpoison): the output IR contains attributes that only the producing backend's parser understands.

## Reproduction

1. Edit `tests/lift/conftest.py` to change `-O0` to `-O1` in `CFLAGS`
2. Run: `uv run pytest tests/lift -v -k struct_test`
3. Observe failure at `recompile_x86_64()` step with the parse error above

## Files involved

- `tools/arm-lifter.cpp` — the output-stage cleanup loop around line 308 (currently strips `NoCreateUndefOrPoison` only) needs to also strip target-specific memory location names from the `memory()` attribute

## Fix approach

In `tools/arm-lifter.cpp`, before writing the output `.ll` file, walk all functions and remove custom memory location names from the `memory()` attribute. LLVM's `Attribute::getWithMemoryType()` / `MemoryEffects` API can be used to rebuild the attribute without the target-specific locations.

The existing `NoCreateUndefOrPoison` cleanup loop (line 308-310) is the natural place to add this.

## Alternative

An even broader approach: strip all target-cpu/target-features attributes and reconstruct a minimal set of portable attributes. This would also remove the noise of `"target-features"="+fp-armv8,-a320,..."` in the output IR.

## Related

Issue #04 — nocreateundeforpoison workaround, same structural problem.
