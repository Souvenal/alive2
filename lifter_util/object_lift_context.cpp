// ObjectLiftContext: Manages shared LLVM Module state for whole-program lifting.

#include "lifter_util/object_lift_context.h"

#include "lifter_util/obj2asm.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/GlobalVariable.h"
#include "llvm/IR/Module.h"
#include "llvm/Linker/Linker.h"
#include "llvm/Object/ObjectFile.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Transforms/Utils/Cloning.h"

using namespace llvm;
using namespace llvm::object;

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

void ObjectLiftContext::registerSectionLabel(const std::string &label,
                                              const std::string &section) {
  sectionLabelMap[label] = section;
}

GlobalVariable *
ObjectLiftContext::lookupGlobalAtOffset(const std::string &section,
                                         uint64_t offset) const {
  auto it = offsetGlobalMap.find({section, offset});
  if (it != offsetGlobalMap.end())
    return it->second;
  return nullptr;
}

std::string ObjectLiftContext::getSectionLabel(const std::string &label) const {
  auto it = sectionLabelMap.find(label);
  if (it != sectionLabelMap.end())
    return it->second;
  return {};
}

void ObjectLiftContext::buildGlobalOffsetMap(ObjectFile &ELF,
                                              Module &SrcModule) {
  // 1. Index source BC string globals by initializer bytes.
  //    The linker may drop globals with zero uses, so we clone them from
  //    SrcModule directly into SharedModule when not already present.
  std::map<std::string, GlobalVariable *> byteStrMap;
  // Holds zeroinitializer byte strings between iterations (StringRef
  // from a temporary would dangle, so we keep the std::string alive).
  std::string zeroinitBytes;
  for (auto &GV : SrcModule.globals()) {
    if (!GV.hasInitializer())
      continue;
    Constant *Init = GV.getInitializer();
    Type *InitTy = Init->getType();

    StringRef Bytes;
    if (auto *CDS = dyn_cast<ConstantDataSequential>(Init)) {
      // ConstantDataSequential: extract raw bytes directly
      Type *ElemTy = CDS->getElementType();
      if (!ElemTy->isIntegerTy(8))
        continue;
      Bytes = CDS->getRawDataValues();
    } else if (isa<ConstantAggregateZero>(Init) &&
               isa<ArrayType>(InitTy) &&
               InitTy->getArrayElementType()->isIntegerTy(8)) {
      // ConstantAggregateZero (zeroinitializer) [N x i8]: N null bytes.
      // Store in persistent zeroinitBytes so the StringRef stays valid.
      uint64_t NumElems = InitTy->getArrayNumElements();
      if (NumElems == 0)
        continue;
      zeroinitBytes = std::string(NumElems, '\0');
      Bytes = zeroinitBytes;
    } else {
      continue;
    }

    if (Bytes.empty())
      continue;

    // Check if this global already exists in SharedModule (from linker)
    auto *existingGV = SharedModule.getGlobalVariable(GV.getName());
    if (existingGV) {
      byteStrMap[Bytes.str()] = existingGV;
    } else {
      // Clone it into SharedModule — linker may have dropped it
      auto *newGV = new GlobalVariable(
          SharedModule, GV.getValueType(), GV.isConstant(),
          GV.getLinkage(), cast<Constant>(Init), GV.getName());
      newGV->setAlignment(GV.getAlign());
      byteStrMap[Bytes.str()] = newGV;
    }
  }

  // 2. Get .rela.text relocations
  SectionRef textSec;
  bool foundText = false;
  for (auto &Sec : ELF.sections()) {
    Expected<StringRef> secName = Sec.getName();
    if (secName && *secName == ".text") {
      textSec = Sec;
      foundText = true;
      break;
    }
  }
  if (!foundText)
    return;

  auto relocMap = obj2asm::buildTextRelocMap(textSec, ELF);

  // 3. For each relocation with section symbol + non-zero addend,
  //    match section bytes at offset against source BC globals.
  //    Unmatched offsets fall through to GEP into the blob.
  //    Also handle addend=0: match the first (longest prefix) string
  //    in each section, since addend=0 is a base reference to the blob.
  for (auto &[instOffset, reloc] : relocMap) {
    if (reloc.symbolName.empty() || !reloc.symbolName.starts_with("."))
      continue;

    // For addend=0, find the longest matching string at offset 0
    if (reloc.addend == 0) {
      if (offsetGlobalMap.count({reloc.symbolName, 0}))
        continue; // already populated
      // Find the referenced section and scan its first bytes
      StringRef secBytes;
      for (auto &Sec : ELF.sections()) {
        Expected<StringRef> secName = Sec.getName();
        if (secName && *secName == reloc.symbolName) {
          Expected<StringRef> contents = Sec.getContents();
          if (contents)
            secBytes = *contents;
          break;
        }
      }
      if (secBytes.empty())
        continue;
      // Longest prefix match at offset 0 picks the actual first string
      GlobalVariable *bestGV = nullptr;
      size_t bestLen = 0;
      for (auto &[byteStr, gv] : byteStrMap) {
        if (secBytes.starts_with(byteStr) && byteStr.size() > bestLen) {
          bestGV = gv;
          bestLen = byteStr.size();
        }
      }
      if (bestGV)
        offsetGlobalMap[{reloc.symbolName, 0}] = bestGV;
      continue;
    }

    // Find the referenced section and get its content bytes
    StringRef secBytes;
    for (auto &Sec : ELF.sections()) {
      Expected<StringRef> secName = Sec.getName();
      if (secName && *secName == reloc.symbolName) {
        Expected<StringRef> contents = Sec.getContents();
        if (contents)
          secBytes = *contents;
        break;
      }
    }
    if (secBytes.empty())
      continue;

    uint64_t addend = static_cast<uint64_t>(reloc.addend);
    if (addend >= secBytes.size())
      continue;

    // Match section bytes at offset against source BC string initializers
    for (auto &[byteStr, gv] : byteStrMap) {
      if (secBytes.substr(addend).starts_with(byteStr)) {
        offsetGlobalMap[{reloc.symbolName, addend}] = gv;
        break;
      }
    }
  }
}
