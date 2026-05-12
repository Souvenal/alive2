# Codegen-injected symbols missing from src-bc: known-ABI table

Status: `needs-triage`

## Problem

`src-bc` (the bitcode produced by clang's frontend) does not contain symbols that LLVM **codegen** injects later in the pipeline. When the lifter encounters a reference to such a symbol in the `.o`, it cannot find a matching declaration in src-bc and falls back to `ObjectLiftContext::getOrCreateGlobalDecl(Name, Ty)`, which synthesizes an extern declaration. The synthesized declaration's type comes from the lifter's local guess at the call site — it may not match the real ABI of the runtime helper.

## Known categories of injected symbols

| Category | Trigger | Examples |
|---|---|---|
| compiler-rt builtins | soft-float, long-int math, atomics, type conversions | `__udivdi3`, `__divti3`, `__cmpdf2`, `__floatundisf`, `__aarch64_sync_cache_range` |
| stack protector | `-fstack-protector*` | `__stack_chk_guard`, `__stack_chk_fail` |
| stack probe | large stack frames | `__chkstk`, `__chkstk_ms` |
| TLS | thread-local variables | `__tls_get_addr`, `__emutls_get_address` |
| C++ runtime | exceptions, RTTI, dynamic dispatch | `__cxa_throw`, `_Unwind_Resume`, `__gxx_personality_v0` |
| PGO / coverage | `-fprofile-*` | `__llvm_prf_*` |

## Failure mode

The fallback declaration has the wrong type → recompilation produces an x86_64 binary whose ABI for that call site is broken (parameters in the wrong registers, wrong return size, etc.). Failure surfaces in the **validation pipeline** (CONTEXT.md) when the lifted binary diverges from the original under QEMU.

## Proposed fix

Add a hardcoded `name → llvm::FunctionType` / `name → llvm::Type` table for the common offenders, consulted before the lifter's own guess. Sketch:

```cpp
// lifter_util/codegen_runtime_abi.h
namespace lifter {
  // Returns the canonical FunctionType for a known compiler-rt / runtime
  // symbol, or nullptr if the name is not in the table.
  llvm::FunctionType *lookupRuntimeFunctionType(llvm::StringRef Name,
                                                llvm::LLVMContext &Ctx);
}
```

Populate from compiler-rt source (`builtins/README.txt` has the prototypes). Start small — only entries actually triggered by current test cases — and grow as the validation pipeline surfaces more divergences.

## Files involved

- `lifter_util/object_lift_context.{h,cpp}` — consult table before defaulting
- new: `lifter_util/codegen_runtime_abi.{h,cpp}` — the table itself

## Why deferred

This is a patch-fix layer; the root issue ("src-bc is incomplete") cannot be solved in arm-lifter. Build the table opportunistically as real-world tests expose missing entries.
