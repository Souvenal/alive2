# Clean up binary_reader dead code (Mach-O + DWARF)

**Date:** 2026-05-12
**Issue:** 14

## Summary

Removed unused functions from `lifter_util/binary_reader.h` and `lifter_util/binary_reader.cpp`. Two categories of dead code were eliminated:

1. **Mach-O disassembly workflow** — 6 functions inherited from an early prototype that called `llvm-objdump` to disassemble Mach-O binaries. The current pipeline uses MC APIs via `obj2asm` on ELF files instead.

2. **DWARF function signature extraction** — `extractFunctionSignatureFromDWARF` and its helper `resolveDIType`. These were unused because the lifter gets function signatures from src-bc, not DWARF. Using DWARF for signatures would even be wrong (src-bc reflects ABI-lowered types; DWARF reflects source-level types — they diverge on aggregate parameters).

## Deleted code

### From `binary_reader.h`

- Removed `#include <optional>`, `#include <vector>`
- Removed forward declarations: `class FunctionType; class LLVMContext; class Type;`
- Removed `DWARFFunctionSignature` struct and `extractFunctionSignatureFromDWARF` declaration
- Removed `ExtractedFunction` struct
- Removed `getRelocationMap()` declaration
- Removed all Mach-O function declarations: `extractFunctionsFromBinary`, `disassembleFunction`, `disassembleAllFunctions`, `parseStubSymbols`, `parseFunctionSymbols`, `replaceAddressWithSymbol`

### From `binary_reader.cpp`

- Removed 10 unused includes (DWARF, MachO, IR types, raw_ostream, cstdlib, iostream, sstream)
- Removed `isArm64MachO()`, `getArchName()`
- Removed `parseStubSymbols()`, `parseFunctionSymbols()`, `replaceAddressWithSymbol()`
- Removed `extractFunctionsFromBinary()`, `disassembleFunction()`, `disassembleAllFunctions()`
- Removed `getRelocationMap()` (zero external references)
- Removed `resolveDIType()` (anonymous namespace) and `extractFunctionSignatureFromDWARF()`

## Kept code

- `RelocKind` enum and `classifyRelocation()` — used by `obj2asm.cpp`
- `SymbolInfo` struct and `buildSymbolMap()` — used by `arm-lifter.cpp` and `obj2asm.cpp`

## Verification

- `cmake --build build --target arm-lifter` succeeds
- `Maze_novarargs` lifted output is behavior-identical (no functional change)
