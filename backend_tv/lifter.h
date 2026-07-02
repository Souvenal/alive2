#pragma once

#include <ostream>
#include <string>

#include "lifter_util/instruction_map.h"
#include "llvm/IR/DIBuilder.h"
#include "llvm/IR/Module.h"
#include "llvm/MC/TargetRegistry.h"

namespace llvm {
class Constant;
class Function;
class VectorType;
} // namespace llvm

class ObjectLiftContext;

namespace lifter {

/*
 * add debug into to an IR file that will help the lifter figure out
 * which LLVM instruction each asm instruction came from
 */
void addDebugInfo(llvm::Function *srcFn,
                  std::unordered_map<unsigned, llvm::Instruction *> &lineMap);

/*
 * lower LLVM IR to textual assembly
 */
std::unique_ptr<llvm::MemoryBuffer>
generateAsm(llvm::Module &, const llvm::Target *Targ, llvm::Triple DefaultTT,
            const char *DefaultCPU, const char *DefaultFeatures);

/*
 * lift textual assembly to LLVM IR. the target arguments should be
 * the same as those used to generate assembly, and lineMap should
 * come from addDebugInfo(). it's fine to pass an empty
 * lineMap. return value is a source/target pair that is ready for an
 * Alive2 refinement check. the source function is returned because it
 * has been rewritten, for example to implement ABI checks.
 */
std::pair<llvm::Function *, llvm::Function *>
liftFunc(llvm::Function *, std::unique_ptr<llvm::MemoryBuffer>,
         std::unordered_map<unsigned, llvm::Instruction *> &lineMap,
         std::string optimize_tgt, std::ostream *out, const llvm::Target *Targ,
         llvm::Triple DefaultTT, const char *DefaultCPU,
         const char *DefaultFeatures);

/*
 * lift textual assembly to LLVM IR, into an existing shared Module.
 * This is the shared Module variant of liftFunc() for arm-lifter.
 * srcFn is passed directly from src-bc (adjustSrc is skipped in
 * this path). ObjCtx provides src-bc global variable lookup priority.
 * Returns the lifted function pointer.
 */
llvm::Function *
liftFuncToModule(llvm::Function *srcFn,
                 std::unique_ptr<llvm::MemoryBuffer> MB,
                 std::unordered_map<unsigned, llvm::Instruction *> &lineMap,
                 std::ostream *out,
                 const llvm::Target *Targ, llvm::Triple DefaultTT,
                 const char *DefaultCPU, const char *DefaultFeatures,
                 llvm::Module &ExternalModule, ObjectLiftContext &ObjCtx,
                 InstructionMapFunction *instructionMap = nullptr);

/*
 * random utility function
 */
inline std::string moduleToString(llvm::Module *M) {
  std::string sss;
  llvm::raw_string_ostream ss(sss);
  M->print(ss, nullptr);
  return sss;
}

} // namespace lifter
