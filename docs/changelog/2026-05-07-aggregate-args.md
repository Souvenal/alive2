# Support Aggregate (Array/Struct) Function Arguments

## Files Changed

- `backend_tv/mc2llvm.cpp` — Replaced blanket `exit(-1)` for array/struct args with `isSupportedAggregateType()` recursive checker. Allows aggregates whose elements/fields are integers or pointers.
- `backend_tv/mc2llvm.h` — Added `ArrayType` and `StructType` branches to `getBitWidth()` using `DataLayout::getTypeSizeInBits()`.
- `backend_tv/arm2llvm.cpp` — Added aggregate decomposition in both entry argument binding and `marshallArgs()`:
  - `flattenAggregate()` — Recursively expands arrays/structs into (Type, index-path) pairs.
  - `extractByIndices()` / `insertByIndices()` — Chain `ExtractValueInst` / `InsertValueInst` with nested indices.
  - `extendToI64()` / `truncateFromI64()` — Convert between scalar types and `i64` for X-register storage.
  - Entry binding: aggregate args skip `enforceSExtZExt()` and decompose each element into X-registers or stack slots.
  - `marshallArgs()`: reads elements from X-registers/stack and reassembles into aggregate via `insertvalue`.
- `backend_tv/arm2llvm.h` — Declared new aggregate helper methods.
- `CLAUDE.md` — Updated Known Limitations entry #9 to mark aggregate args as fixed.

## Rationale

AArch64 AAPCS64 calling convention flattens small structs (≤ 16 bytes) passed by value into register-sized units. Clang represents this as LLVM array types like `[2 x i64]`. The lifter previously rejected all aggregate arguments with `exit(-1)`, blocking functions like `get_count(NamedCount nc)`.

The fix treats aggregates as a sequence of scalar elements, each consuming one register/stack slot — matching the calling convention's flattening semantics.

## Impact

- `struct_test.c` — `get_count()` and `name_length()` now lift successfully (both take `[2 x i64]`).
- General support for `[N x T]` where `T` ∈ {i8, i16, i32, i64, ptr}.
- Nested structs are supported via recursive flattening.
- Float/vector elements in aggregates remain unsupported (HFA/HVA path not implemented).

## Verification

```bash
cd tests/lift
make lift          # struct_test.c: 9 functions lifted, no array error
```

Lifted IR for `get_count` shows correct decomposition:
```llvm
%2 = extractvalue [2 x i64] %0, 0
store i64 %2, ptr %X0
%3 = extractvalue [2 x i64] %0, 1
store i64 %3, ptr %X1
```

Function body then loads from X0 and truncates to i32, matching the original `return nc.count` semantics.
