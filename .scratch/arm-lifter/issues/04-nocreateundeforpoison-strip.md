# nocreateundeforpoison attribute is stripped as a workaround

Status: `needs-triage`

## Problem

LLVM 20+ intrinsic functions carry the `nocreateundeforpoison` attribute, which older LLVM versions (used by `zig cc`) don't recognize, causing "unterminated attribute group" parse errors.

Current workaround: automatically strip this attribute from all functions before outputting the lifted `.ll` file.

## Impact

The workaround is functional but fragile. If more new attributes are added in future LLVM versions, the same problem will recur.

## Files involved

- `tools/arm-lifter.cpp` — `runLifter()`, the `removeFnAttr(Attribute::NoCreateUndefOrPoison)` loop

## Better fix

Use a version-agnostic approach to strip unknown attributes, or require a matching LLVM version for the toolchain.
