// ObjectLiftContext: Manages shared LLVM Module state for whole-program lifting.

#include "lifter_util/object_lift_context.h"

#include "llvm/IR/GlobalVariable.h"
#include "llvm/IR/Module.h"
#include "llvm/Linker/Linker.h"
#include "llvm/Transforms/Utils/Cloning.h"

using namespace llvm;

ObjectLiftContext::ObjectLiftContext(Module &SharedModule)
    : SharedModule(SharedModule) {}

void ObjectLiftContext::importSrcBcGlobals(Module &SrcModule) {
  // Clone srcModule so we don't modify the original (adjustSrc mutates srcFn,
  // and we need SrcModule intact for subsequent per-function synthetic modules).
  auto Clone = CloneModule(SrcModule);

  // Strip function bodies — we only need declarations for cross-function call
  // resolution. The actual function bodies come from lifting.
  for (auto &F : *Clone)
    if (!F.isDeclaration())
      F.deleteBody();

  // Link the clone into SharedModule. This imports all global variable
  // definitions (with initializers, types, alignment) and function declarations.
  // Using Flags::None (not LinkOnlyNeeded) to pre-import everything, because:
  //  - src-bc is typically small
  //  - pre-importing avoids type mismatches during lazy resolution
  //  - LinkOnlyNeeded would import nothing if SharedModule has no references yet
  Linker Linker(SharedModule);
  if (Linker.linkInModule(std::move(Clone), Linker::Flags::None)) {
    // linkInModule returns true on error
    errs() << "ERROR: ObjectLiftContext::importSrcBcGlobals: "
           << "Linker failed to link src-bc globals into shared module\n";
    exit(-1);
  }
}

GlobalVariable *ObjectLiftContext::lookupGlobal(const std::string &Name) {
  return SharedModule.getGlobalVariable(Name);
}

Constant *ObjectLiftContext::getOrCreateGlobalDecl(const StringRef &Name,
                                                    Type *Ty) {
  return SharedModule.getOrInsertGlobal(Name, Ty);
}
