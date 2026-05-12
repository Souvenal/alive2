# Mach-O format not supported

Status: `needs-triage`

## Problem

`mc2llvm.cpp` uses `.starts_with(".rodata")` for constant section detection. Mach-O segment names (e.g., `__TEXT,__const`) don't match this pattern, so the lifter fails on Mach-O object files.

## Impact

Users on macOS who compile with the default target cannot use arm-lifter without cross-compiling to AArch64 Linux ELF.

## Possible approaches

1. Generalize section name matching to support both ELF and Mach-O naming conventions
2. Document the ELF-only restriction and provide a clear error message when Mach-O is detected

## Files involved

- `backend_tv/mc2llvm.cpp` — section detection logic
- `lifter_util/binary_reader.cpp` — ELF parsing (would need Mach-O path)
