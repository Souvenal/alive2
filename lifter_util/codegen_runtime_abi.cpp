// codegen_runtime_abi: Canonical FunctionTypes for codegen-injected runtime symbols.
//
// Entries are grouped by identical signatures. Derived from compiler-rt builtins
// source (compiler-rt/lib/builtins/) and LLVM's AArch64 target lowering.
//
// See issue 11: .scratch/arm-lifter/issues/11-codegen-injected-symbols-abi-table.md

#include "lifter_util/codegen_runtime_abi.h"

#include "llvm/IR/DerivedTypes.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/Type.h"

using namespace llvm;

namespace lifter {

FunctionType *lookupRuntimeFunctionType(StringRef Name, LLVMContext &C) {
  auto *I32 = Type::getInt32Ty(C);
  auto *I64 = Type::getInt64Ty(C);
  auto *I128 = Type::getInt128Ty(C);
  auto *Float = Type::getFloatTy(C);
  auto *Double = Type::getDoubleTy(C);
  auto *Half = Type::getHalfTy(C);
  auto *Void = Type::getVoidTy(C);
  auto *Ptr = PointerType::get(C, 0);

  // ----- Integer arithmetic helpers -----

  // i64(i64, i64) — 64-bit integer division and modulus
  if (Name == "__udivdi3" || Name == "__umoddi3" || Name == "__divdi3" ||
      Name == "__moddi3")
    return FunctionType::get(I64, {I64, I64}, false);

  // i128(i128, i128) — 128-bit integer division, modulus, multiply
  if (Name == "__udivti3" || Name == "__umodti3" || Name == "__divti3" ||
      Name == "__modti3" || Name == "__multi3")
    return FunctionType::get(I128, {I128, I128}, false);

  // i128(i128, i32) — 128-bit shift helpers
  if (Name == "__ashlti3" || Name == "__ashrti3" || Name == "__lshrti3")
    return FunctionType::get(I128, {I128, I32}, false);

  // ----- Float-to-integer conversion helpers -----

  // i64(double) — double to 64-bit integer (signed and unsigned)
  if (Name == "__fixdfdi" || Name == "__fixunsdfdi")
    return FunctionType::get(I64, {Double}, false);

  // i64(float) — float to 64-bit integer (signed and unsigned)
  if (Name == "__fixsfdi" || Name == "__fixunssfdi")
    return FunctionType::get(I64, {Float}, false);

  // i32(double) — double to 32-bit signed/unsigned integer
  if (Name == "__fixdfsi" || Name == "__fixunsdfsi")
    return FunctionType::get(I32, {Double}, false);

  // i32(float) — float to 32-bit signed/unsigned integer
  if (Name == "__fixsfsi" || Name == "__fixunssfsi")
    return FunctionType::get(I32, {Float}, false);

  // ----- Integer-to-float conversion helpers -----

  // float(i64) — signed/unsigned 64-bit to float
  if (Name == "__floatundisf" || Name == "__floatdisf")
    return FunctionType::get(Float, {I64}, false);

  // double(i64) — signed/unsigned 64-bit to double
  if (Name == "__floatundidf" || Name == "__floatdidf")
    return FunctionType::get(Double, {I64}, false);

  // float(i32) — unsigned 32-bit to float
  if (Name == "__floatunsisf")
    return FunctionType::get(Float, {I32}, false);

  // double(i32) — unsigned 32-bit to double
  if (Name == "__floatunsidf")
    return FunctionType::get(Double, {I32}, false);

  // i128(double) — double to 128-bit integer
  if (Name == "__fixdfti" || Name == "__fixunsdfti")
    return FunctionType::get(I128, {Double}, false);

  // i128(float) — float to 128-bit integer
  if (Name == "__fixsfti" || Name == "__fixunssfti")
    return FunctionType::get(I128, {Float}, false);

  // double(i128) — 128-bit integer to double
  if (Name == "__floattidf" || Name == "__floatuntidf")
    return FunctionType::get(Double, {I128}, false);

  // float(i128) — 128-bit integer to float
  if (Name == "__floattisf" || Name == "__floatuntisf")
    return FunctionType::get(Float, {I128}, false);

  // ----- Float format conversion helpers -----

  // double(float) — float to double extension
  if (Name == "__extendsfdf2")
    return FunctionType::get(Double, {Float}, false);

  // float(double) — double to float truncation
  if (Name == "__truncdfsf2")
    return FunctionType::get(Float, {Double}, false);

  // float(half) — half to float extension
  if (Name == "__extendhfsf2")
    return FunctionType::get(Float, {Half}, false);

  // half(double) — double to half truncation
  if (Name == "__truncdfhf2")
    return FunctionType::get(Half, {Double}, false);

  // half(float) — float to half truncation
  if (Name == "__truncsfhf2")
    return FunctionType::get(Half, {Float}, false);

  // half(float) — variant: half(float)
  if (Name == "__gnu_h2f_ieee")
    return FunctionType::get(Float, {Half}, false);

  // float(half) — variant
  if (Name == "__gnu_f2h_ieee")
    return FunctionType::get(Half, {Float}, false);

  // double(half) — half to double extension
  if (Name == "__extendhfdf2")
    return FunctionType::get(Double, {Half}, false);

  // ----- Float arithmetic helpers (software float) -----

  // double(double, double) — double arithmetic
  if (Name == "__adddf3" || Name == "__subdf3" || Name == "__muldf3" ||
      Name == "__divdf3")
    return FunctionType::get(Double, {Double, Double}, false);

  // float(float, float) — float arithmetic
  if (Name == "__addsf3" || Name == "__subsf3" || Name == "__mulsf3" ||
      Name == "__divsf3")
    return FunctionType::get(Float, {Float, Float}, false);

  // float(float) — float negation
  if (Name == "__negsf2")
    return FunctionType::get(Float, {Float}, false);

  // double(double) — double negation
  if (Name == "__negdf2")
    return FunctionType::get(Double, {Double}, false);

  // ----- Float comparison helpers -----

  // i32(double, double) — double comparison (returns -1/0/1)
  if (Name == "__eqdf2" || Name == "__nedf2" || Name == "__gtdf2" ||
      Name == "__gedf2" || Name == "__ltdf2" || Name == "__ledf2" ||
      Name == "__unorddf2" || Name == "__cmpdf2")
    return FunctionType::get(I32, {Double, Double}, false);

  // i32(float, float) — float comparison (returns -1/0/1)
  if (Name == "__eqsf2" || Name == "__nesf2" || Name == "__gtsf2" ||
      Name == "__gesf2" || Name == "__ltsf2" || Name == "__lesf2" ||
      Name == "__unordsf2" || Name == "__cmpsf2")
    return FunctionType::get(I32, {Float, Float}, false);

  // ----- AArch64-specific -----

  // void(ptr, ptr) — AArch64 data cache range synchronization
  if (Name == "__aarch64_sync_cache_range")
    return FunctionType::get(Void, {Ptr, Ptr}, false);

  // void(ptr, i64, i64) — AArch64 cache operation by set/way (not common)
  // Not included unless hit in practice.

  return nullptr;
}

} // namespace lifter
