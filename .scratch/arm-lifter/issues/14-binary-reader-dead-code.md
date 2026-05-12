# Clean up binary_reader dead code (Mach-O + DWARF signature extraction)

Status: `ready-for-agent`

## What to build

Remove unused functions from `lifter_util/binary_reader.{h,cpp}`. Two categories of dead code:

1. **Mach-O disassembly workflow** — 6 functions inherited from an early prototype that called `llvm-objdump` to disassemble Mach-O binaries. The current pipeline uses MC APIs via `obj2asm` on ELF files instead. Functions: `disassembleFunction`, `disassembleAllFunctions`, `parseStubSymbols`, `parseFunctionSymbols`, `extractFunctionsFromBinary`, `replaceAddressWithSymbol`. Also the `ExtractedFunction` struct.

2. **DWARF function signature extraction** — `extractFunctionSignatureFromDWARF` and its helper `resolveDIType`. These are unused because the lifter gets function signatures from src-bc, not DWARF. Using DWARF for signatures would even be wrong (src-bc reflects ABI-lowered types; DWARF reflects source-level types — they diverge on aggregate parameters).

## Acceptance criteria

- [ ] `ExtractedFunction` struct removed from `binary_reader.h`
- [ ] Declarations for the 6 Mach-O functions removed from `binary_reader.h`
- [ ] Declarations for `DWARFFunctionSignature`, `extractFunctionSignatureFromDWARF`, and `resolveDIType` removed from `binary_reader.h`
- [ ] Implementations of all above removed from `binary_reader.cpp`
- [ ] `#include "llvm/DebugInfo/DWARF/DWARFContext.h"` and `DWARFDie.h` removed from `binary_reader.cpp` (no longer needed)
- [ ] `docs/changelog/` entry added documenting the removal
- [ ] `cmake --build build --target arm-lifter` succeeds
- [ ] Lifting `tests/lift/Maze_novarargs.o` produces identical output (behavior unchanged)

## Blocked by

None - can start immediately
