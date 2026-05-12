# Phase 5: Data section optimization (.long/.quad, string detection)

Status: `ready-for-agent`

## What to build

Optimize data section emission in `lifter_util/obj2asm.cpp`'s `convertDataSections`. Currently every byte in `.rodata`, `.data`, etc. is emitted as a separate `.byte` directive. This produces bloated `.ll` files (many thousands of lines) and slows down downstream compilation.

Three improvements:

1. **Alignment-based chunking**: For consecutive bytes that align to 4-byte or 8-byte boundaries, emit `.long` (4 bytes) or `.quad` (8 bytes) instead of four or eight `.byte` directives. This is a pure cosmetic/performance improvement and must not change the lift semantics.

2. **String detection**: When a sequence of null-terminated printable ASCII bytes is found in `.rodata`, emit as `.asciz` instead of raw `.byte`/`.long`. This improves IR readability and makes string contents visible to LLVM's optimization passes.

3. **Tail padding**: Remaining bytes that don't form a full `.long`/`.quad` stay as `.byte` (current behavior).

## Acceptance criteria

- [ ] `Obj2Asm::convertDataSections` detects 4-byte and 8-byte alignable runs and emits `.long`/`.quad` when possible
- [ ] Null-terminated printable ASCII sequences are detected and emitted as `.asciz`
- [ ] Maze_novarargs lifted IR is noticeably shorter (fewer lines) than before
- [ ] Maze_novarargs recompiled x86_64 binary behaves identically under lima (+ qemu-x86_64) — stdout + exit code unchanged
- [ ] All existing must-pass test cases still pass after optimization

## Blocked by

None - can start immediately
