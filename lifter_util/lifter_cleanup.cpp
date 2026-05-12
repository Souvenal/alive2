// Copyright (c) 2026-present Souvenal
// Distributed under the MIT license that can be found in the LICENSE file.
//
// Safe cleanup passes for lifted IR — mem2reg, dce, instcombine,
// simplifycfg, globaldce. No inlining, no IPO, no function-attrs
// inference. Preserves the call structure of the original ARM object.

#include "lifter_util/lifter_cleanup.h"

#include "llvm/IR/Module.h"
#include "llvm/Passes/PassBuilder.h"

using namespace llvm;

namespace lifter {

void cleanup_module(llvm::Module &M) {
  LoopAnalysisManager LAM;
  FunctionAnalysisManager FAM;
  CGSCCAnalysisManager CGAM;
  ModuleAnalysisManager MAM;
  PassBuilder PB;
  PB.registerModuleAnalyses(MAM);
  PB.registerCGSCCAnalyses(CGAM);
  PB.registerFunctionAnalyses(FAM);
  PB.registerLoopAnalyses(LAM);
  PB.crossRegisterProxies(LAM, FAM, CGAM, MAM);

  ModulePassManager MPM;
  // mem2reg promotes register allocas to SSA, cleaning up the
  // alloca/freeze/store/load boilerplate the lifter emits for every
  // X and Q register. dce removes dead stores/loads. instcombine
  // simplifies redundant instructions. simplifycfg cleans up dead
  // basic blocks. globaldce removes unused data sections.
  if (auto Err = PB.parsePassPipeline(
          MPM, "function(mem2reg,dce,simplifycfg),globaldce"))
    report_fatal_error(
        Twine("lifter::cleanup_module: bad pass pipeline: ") +
        toString(std::move(Err)));

  MPM.run(M, MAM);
}

} // namespace lifter
