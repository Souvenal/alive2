#pragma once

// codegen_runtime_abi: Canonical FunctionTypes for codegen-injected runtime symbols.
//
// The LLVM backend injects calls to compiler-rt builtins (e.g., __udivti3 for
// 128-bit division) during the IR-to-assembly lowering phase. These symbols
// appear in the .o but are absent from src-bc. This table provides the correct
// FunctionType for known symbols so the lifter can synthesize a properly-typed
// declaration instead of crashing or guessing the wrong type.

namespace llvm {
class FunctionType;
class LLVMContext;
class StringRef;
} // namespace llvm

namespace lifter {

/// Returns the canonical FunctionType for a known compiler-rt/runtime symbol,
/// or nullptr if the name is not recognized.
llvm::FunctionType *lookupRuntimeFunctionType(llvm::StringRef Name,
                                              llvm::LLVMContext &Ctx);

} // namespace lifter
