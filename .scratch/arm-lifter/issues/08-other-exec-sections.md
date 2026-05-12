# Disassemble all executable sections, not just `.text`

Status: `needs-triage`

## Problem

`disassembleTextSection()` and `generateFullAsm()` only process `secName == ".text"`, ignoring other executable sections:

- `.init`, `.fini`
- `.text.hot`, `.text.cold`, `.text.unlikely`
- `.plt`

Additionally, `generateDataSections()` only emits `SHT_PROGBITS` sections, skipping:

- `SHT_INIT_ARRAY` / `SHT_FINI_ARRAY` (constructor/destructor function pointers)

## Impact

1. Functions placed in PGO subsections (`.text.hot` etc.) are missing from lifted IR
2. Constructor/destructor function pointers in `.init_array`/`.fini_array` are lost, so recompiled binaries won't call them at startup/shutdown

## Fix required

1. Expand section filtering to `secName.starts_with(".text")` or all `SHF_EXECINSTR` sections
2. Extend `buildTextRelocMap` to cover these sections
3. Add `SHT_INIT_ARRAY`/`SHT_FINI_ARRAY` support in `generateDataSections()`

## Files involved

- `lifter_util/obj2asm.cpp` — `disassembleTextSection()`, `generateFullAsm()`, `generateDataSections()`
- `lifter_util/binary_reader.cpp` — `buildTextRelocMap()`
