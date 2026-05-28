# Spurious `__stack_chk_guard` global emitted unconditionally

Status: `needs-triage`

## Problem

`streamerwrapper.h:88-96` unconditionally creates a `__stack_chk_guard` global in every `MCFunction`:

```cpp
MCFunction() {
  MCGlobal g{
      .name = "__stack_chk_guard",
      .align = llvm::Align(8),
      .section = ".rodata",
      .data = {'7', '7', '7', '7', '7', '7', '7', '7'},
  };
  MCglobals.push_back(g);
}
```

This emits a fake `@__stack_chk_guard = weak constant [8 x i8] c"77777777"` global in the lifted IR even when the original binary has no stack protector. If the program doesn't reference stack_chk_guard, it's dead code, but:
- It still appears in the output (wastes space, causes unnecessary symbol)
- The fake value "77777777" is wrong if anything does reference it (the real guard is a random canary)

## Resolution note

This is now the **only remaining aspect** of the original issue 19. Name recovery, ArrayType, alignment, and linkage were all resolved by issue 20 (symbol-level string globals via offset-based byte matching).

## Files involved

- `backend_tv/streamerwrapper.h:88-96` — `MCFunction()`: hardcoded `__stack_chk_guard`
