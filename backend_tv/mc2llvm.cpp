#include "llvm/IR/DIBuilder.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/MC/MCAsmInfo.h"
#include "llvm/MC/MCSymbol.h"

#include <set>

#include "backend_tv/lifter.h"
#include "backend_tv/mc2llvm.h"
#include "lifter_util/object_lift_context.h"
#include "lifter_util/codegen_runtime_abi.h"

#include <regex>

using namespace std;
using namespace llvm;
using namespace lifter;

////////////////////

Function *mc2llvm::copyFunctionToTarget(Function *f, const Twine &name) {
  auto newF = Function::Create(f->getFunctionType(),
                               GlobalValue::LinkageTypes::ExternalLinkage, name,
                               LiftedModule);
  if (f->hasFnAttribute(Attribute::NoReturn))
    newF->addFnAttr(Attribute::NoReturn);
  if (f->hasFnAttribute(Attribute::WillReturn))
    newF->addFnAttr(Attribute::WillReturn);
  if (f->hasRetAttribute(Attribute::NoAlias))
    newF->addRetAttr(Attribute::NoAlias);
  if (f->hasRetAttribute(Attribute::SExt))
    newF->addRetAttr(Attribute::SExt);
  if (f->hasRetAttribute(Attribute::ZExt))
    newF->addRetAttr(Attribute::ZExt);
  auto attr = f->getFnAttribute(Attribute::Memory);
  if (attr.hasAttribute(Attribute::Memory))
    newF->addFnAttr(attr);
  return newF;
}

Constant *mc2llvm::lazyAddGlobal(string newGlobal) {
  *out << "  lazyAddGlobal '" << newGlobal << "'\n";

  {
    auto got = intrinsic_names.find(newGlobal);
    if (got != intrinsic_names.end())
      newGlobal = got->second;
  }

  if (newGlobal == "__stack_chk_fail") {
    if (ObjCtx) {
      auto *existing = LiftedModule->getGlobalVariable("__stack_chk_fail");
      if (existing)
        return existing;
    }
    return new GlobalVariable(*LiftedModule, getIntTy(8), false,
                              GlobalValue::LinkageTypes::ExternalLinkage,
                              nullptr, "__stack_chk_fail");
  }

  // is the global the address of a function?
  if (newGlobal == liftedFn->getName()) {
    // pointer to the function we're lifting into is a special case
    *out << "  yay it's me!\n";
    return liftedFn;
  }

  // ★ ObjectLiftContext lookup: if we're in shared Module mode, check
  // if the global already exists in the shared Module (e.g., imported
  // from src-bc with complete definition). This MUST be checked before
  // copyFunctionToTarget() to avoid LLVM auto-suffixing names (e.g.
  // myalloc → myalloc.2) when importSrcBcGlobals already created the
  // declaration. This takes priority over both synthetic module lookup
  // and MCglobals (which creates truncated globals from assembly data).
  if (ObjCtx) {
    auto *existingGV = ObjCtx->lookupGlobal(newGlobal);
    if (existingGV) {
      *out << "  found global '" << newGlobal
           << "' in shared Module (src-bc definition)\n";
      return existingGV;
    }
    // Also check for function declarations in the shared Module
    auto *existingFn = LiftedModule->getFunction(newGlobal);
    if (existingFn) {
      *out << "  found function '" << newGlobal
           << "' in shared Module\n";
      return existingFn;
    }
  }

  for (auto &f : *srcFn->getParent()) {
    auto name = f.getName();
    if (name != newGlobal)
      continue;
    *out << "  creating function '" << newGlobal << "'\n";
    return copyFunctionToTarget(&f, newGlobal);
  }

  // globals that are definitions in the assembly can be lifted
  // directly. here we have to deal with a mix of literal and
  // symbolic data, and also deal with globals whose initializers
  // reference each other. we support the latter by creating an
  // initializer-free dummy variable right now, and then later
  // creating the actual thing we need
  for (const auto &g : Str->MF.MCglobals) {
    string name{demangle(g.name)};
    if (name != newGlobal)
      continue;
    *out << "creating lifted global " << name << " from the assembly\n";
    auto size = g.data.size();
    *out << "  section = " << g.section << "\n";
    if (ObjCtx)
      ObjCtx->registerSectionLabel(name, g.section);

    vector<Type *> tys;
    vector<Constant *> vals;
    for (unsigned i = 0; i < size; ++i) {
      auto &data = g.data[i];
      if (holds_alternative<char>(data)) {
        tys.push_back(getIntTy(8));
        vals.push_back(ConstantInt::get(getIntTy(8), get<char>(data)));
      } else if (holds_alternative<OffsetSym>(data)) {
        auto s = get<OffsetSym>(data);
        *out << "  it's a symbol named " << s.sym << "\n";
        auto res = LLVMglobals.find(s.sym);
        Constant *con;
        if (res == LLVMglobals.end()) {
          // ok, this global hasn't been lifted yet. but it might
          // already be in the deferred queue, in which case we can
          // use its dummy version.
          bool found = false;
          for (auto [name, _con] : deferredGlobs) {
            if (name == s.sym) {
              found = true;
              con = _con;
              break;
            }
          }
          if (!found) {
            *out << "  breaking recursive loop by deferring " << s.sym << "\n";
            auto *dummy =
                new GlobalVariable(*LiftedModule, getIntTy(8), false,
                                   GlobalValue::LinkageTypes::ExternalLinkage,
                                   nullptr, s.sym + "_tmp");
            deferredGlobs.push_back({.name = demangle(s.sym), .val = dummy});
            con = dummy;
          }
        } else {
          *out << "  found it using regular lookup\n";
          con = res->second;
        }
        Constant *offset = getUnsignedIntConst(s.offset, 64);
        Constant *ptr =
            ConstantExpr::getGetElementPtr(getIntTy(8), con, offset);
        tys.push_back(PointerType::get(Ctx, 0));
        vals.push_back(ptr);
      } else {
        assert(false);
      }
    }
    auto *ty = StructType::create(tys);
    auto initializer = ConstantStruct::get(ty, vals);
    bool isConstant = g.section.starts_with(".rodata") || g.section == ".text";
    auto *glob = new GlobalVariable(*LiftedModule, ty, isConstant,
                                    GlobalValue::LinkageTypes::WeakAnyLinkage,
                                    initializer, name);
    glob->setAlignment(g.align);
    return glob;
  }

  // globals that are only declarations have to be registered in
  // LLVM IR, but they don't show up anywhere in the assembly -- the
  // declaration is implicit. so here we find those and create them
  // in the lifted module
  for (auto &srcFnGlobal : srcFn->getParent()->globals()) {
    if (!srcFnGlobal.isDeclaration())
      continue;
    string name{srcFnGlobal.getName()};
    if (name != newGlobal)
      continue;
    *out << "creating declaration for global variable " << name << "\n";
    *out << "  linkage = " << srcFnGlobal.getLinkage() << "\n";
    auto *glob = new GlobalVariable(*LiftedModule, srcFnGlobal.getValueType(),
                                    srcFnGlobal.isConstant(),
                                    GlobalValue::LinkageTypes::ExternalLinkage,
                                    /*initializer=*/nullptr, name);
    glob->setAlignment(srcFnGlobal.getAlign());
    return glob;
  }

  {
    /*
     * sometimes an intrinsic appears in tgt, but not src. there's
     * no principled way to deal with these, we'll special case them
     * here
     */
    auto got = implicit_intrinsics.find(newGlobal);
    if (got != implicit_intrinsics.end()) {
      auto newF = Function::Create(got->second,
                                   GlobalValue::LinkageTypes::ExternalLinkage,
                                   newGlobal, LiftedModule);
      return newF;
    }
  }

  // If none of the above matched, check the known codegen-injected runtime
  // symbols table (compiler-rt builtins, AArch64 helpers, etc.). These symbols
  // are injected by LLVM's backend during IR→assembly lowering and do not
  // appear in src-bc.
  if (auto *FT = lookupRuntimeFunctionType(newGlobal, Ctx)) {
    *out << "  creating compiler-rt function '" << newGlobal
         << "' from ABI table\n";
    return Function::Create(FT, GlobalValue::LinkageTypes::ExternalLinkage,
                            newGlobal, LiftedModule);
  }

  // Fallback for __sec_N labels that reference sections without assembly data
  // (e.g. BSS sections which emit .comm directives, not progbits data). These
  // labels appear in sectionOffsetLabels because .text has a relocation against
  // .bss+addend, but convertDataSections skips NOBITS sections so no MCGlobal
  // exists. Create a zeroinitializer placeholder; mc2llvm does address
  // arithmetic via GEP anyway.
  if (ObjCtx && newGlobal.starts_with("__sec_")) {
    auto *existingGV = ObjCtx->lookupGlobal(newGlobal);
    if (!existingGV) {
      *out << "  creating zeroinitializer global for section label "
           << newGlobal << "\n";
      existingGV = new GlobalVariable(
          *LiftedModule, getIntTy(8), false,
          GlobalValue::LinkageTypes::InternalLinkage,
          ConstantInt::get(getIntTy(8), 0), newGlobal);
    }
    return existingGV;
  }

  // Fallback for .L_* local labels: create internal zeroinitializer.
  // These are assembler-local branch-target labels that can't be extern
  // function declarations — the linker rejects undefined .L_* symbols.
  if (newGlobal.starts_with(".L_")) {
    *out << "  creating zeroinitializer for local label '"
         << newGlobal << "'\n";
    auto *gv = new GlobalVariable(
        *LiftedModule, getIntTy(8), false,
        GlobalValue::InternalLinkage,
        ConstantInt::get(getIntTy(8), 0), newGlobal);
    return gv;
  }

  // Fallback: create extern function declaration. This covers cross-TU
  // function pointer references (e.g. iterate → ADRP matrix_test) where
  // the function exists in the exe but not in src-bc. We need the pointer
  // value only — the function body is not lifted.
  {
    auto *existing = LiftedModule->getFunction(newGlobal);
    if (!existing) {
      *out << "  creating extern function declaration for '"
           << newGlobal << "' (not in src-bc)\n";
      auto *FT = FunctionType::get(Type::getVoidTy(Ctx), /*isVarArg=*/true);
      existing = Function::Create(FT, GlobalValue::ExternalLinkage,
                                  newGlobal, LiftedModule);
    }
    return existing;
  }

  *out << "ERROR: global symbol '" << newGlobal << "' not found\n";
  exit(-1);
}

Constant *mc2llvm::lookupGlobal(const string &nm) {
  auto name = demangle(nm);
  *out << "lookupGlobal '" << name << "'\n";

  auto glob = LLVMglobals.find(name);
  if (glob != LLVMglobals.end())
    return glob->second;

  auto *g = lazyAddGlobal(name);
  assert(g);
  LLVMglobals[name] = g;

  // lifting this one may have necessitated lifting other variables,
  // which we deferred, and created placeholders instead. fill those
  // in now. this may keep cascading for a while, no problem! our
  // overall goal here is to end up lifting the transitive closure
  // of stuff reachable from the function we're lifting, and nothing
  // else.
  while (!deferredGlobs.empty()) {
    *out << "  processing a deferred global\n";
    auto def = deferredGlobs.at(0);
    deferredGlobs.erase(deferredGlobs.begin());
    auto g2 = lazyAddGlobal(def.name);
    def.val->replaceAllUsesWith(g2);
    def.val->eraseFromParent();
    LLVMglobals[def.name] = g2;
  }

  return g;
}

Value *mc2llvm::getMaskByType(Type *llvm_ty) {
  assert((llvm_ty->isIntegerTy() || llvm_ty->isVectorTy()) &&
         "getMaskByType only handles integer or vector type right now\n");
  Value *mask_value;

  if (llvm_ty->isIntegerTy()) {
    auto W = llvm_ty->getIntegerBitWidth();
    mask_value = getUnsignedIntConst(W - 1, W);
  } else if (llvm_ty->isVectorTy()) {
    VectorType *shift_value_type = ((VectorType *)llvm_ty);
    auto W_element = shift_value_type->getScalarSizeInBits();
    auto numElements = shift_value_type->getElementCount().getFixedValue();
    vector<Constant *> widths;

    // Push numElements x (W_element-1)'s to the vector widths
    for (unsigned i = 0; i < numElements; i++) {
      widths.push_back(
          ConstantInt::get(Ctx, llvm::APInt(W_element, W_element - 1)));
    }

    // Get a ConstantVector of the widths
    mask_value = getVectorConst(widths);
  } else {
    *out << "ERROR: getMaskByType encountered unhandled/unknown type\n";
    exit(-1);
  }

  return mask_value;
}

std::tuple<string, long> mc2llvm::getOffset(const string &var) {
  std::smatch m;
  std::regex offset("^(.*)\\+([0-9]+)$");
  if (std::regex_search(var, m, offset)) {
    auto root = m[1];
    auto offset = m[2];
    *out << "  root = " << root << "\n";
    *out << "  offset = " << offset << "\n";
    return make_tuple(root, stol(offset));
  } else {
    return make_tuple(var, 0);
  }
}

string mc2llvm::demangle(const string &name) {
  assert(name != "");
  if (name.rfind(".L.", 0) == 0) {
    // the assembler has mangled local symbols, which start with a
    // dot, by prefixing them with ".L"; here we demangle
    return name.substr(2);
  } else {
    return name;
  }
}

pair<std::string, uint16_t> mc2llvm::MCExprToName(const MCExpr *expr) {
  auto sr = dyn_cast<MCSymbolRefExpr>(expr);
  if (sr) {
    *out << "raw symbol ref expr\n";
    return make_pair(demangle((string)sr->getSymbol().getName()), NO_SPECIFIER);
  }

  uint16_t specifier = NO_SPECIFIER;
  const MCExpr *inner = expr;

  if (auto spec = dyn_cast<MCSpecifierExpr>(expr)) {
    specifier = spec->getSpecifier();
    inner = spec->getSubExpr();
    // AArch64 MC parser produces MCSpecifierExpr(MCBinaryExpr(symbol, offset))
    // e.g., :pg_hi21:maze+5 or :lo12:maze+5
    if (auto bin = dyn_cast<MCBinaryExpr>(inner)) {
      if (auto s = dyn_cast<MCSpecifierExpr>(bin->getLHS())) {
        specifier = s->getSpecifier();
        inner = s->getSubExpr();
      } else {
        inner = bin->getLHS();
      }
    }
  } else if (auto bin = dyn_cast<MCBinaryExpr>(expr)) {
    // MCBinaryExpr: symbol + constant addend (e.g., "maze+5").
    // In shared Module mode, we handle addend via GEP, so extract
    // the base symbol here. The addend is extracted separately in
    // mapExprVar/getExprVar which call MCExprToName.
    *out << "MCExprToName: got MCBinaryExpr (symbol + addend)\n";
    if (auto s = dyn_cast<MCSpecifierExpr>(bin->getLHS())) {
      specifier = s->getSpecifier();
      inner = s->getSubExpr();
    } else {
      inner = bin->getLHS();
    }
  }

  sr = dyn_cast<MCSymbolRefExpr>(inner);
  if (!sr) {
    *out << "MCExprToName: cannot extract symbol name from expression\n";
    return make_pair("", specifier);
  }
  return make_pair(demangle((string)sr->getSymbol().getName()), specifier);
}

// Extract constant addend from an MCBinaryExpr (e.g., "maze+5" → 5).
// Returns 0 if the expression is not a binary expr or has no constant addend.
static int64_t extractAddend(const MCExpr *expr) {
  // Unwrap MCSpecifierExpr if present (e.g., :pg_hi21:maze+5 or :lo12:maze+5)
  if (auto spec = dyn_cast<MCSpecifierExpr>(expr))
    expr = spec->getSubExpr();
  auto *bin = dyn_cast<MCBinaryExpr>(expr);
  if (!bin)
    return 0;

  // Expect: SymbolRef + Constant  or  SymbolRef - Constant
  if (bin->getOpcode() == MCBinaryExpr::Add) {
    if (auto *ci = dyn_cast<MCConstantExpr>(bin->getRHS()))
      return ci->getValue();
    if (auto *ci = dyn_cast<MCConstantExpr>(bin->getLHS()))
      return ci->getValue();
  } else if (bin->getOpcode() == MCBinaryExpr::Sub) {
    if (auto *ci = dyn_cast<MCConstantExpr>(bin->getRHS()))
      return -ci->getValue();
  }
  return 0;
}

std::string mc2llvm::mapExprVar(const MCExpr *expr) {
  auto [name, specifier] = MCExprToName(expr);
  int64_t addend = extractAddend(expr);

  // In shared Module mode with non-zero addend, we'll generate a GEP
  // later when the ADRP result is used. For now, just resolve the base
  // symbol and store the addend for later use.
  (void)addend; // FIXME: store addend for GEP generation

  if (!lookupGlobal(name)) {
    *out << "\ncan't find global '" << name << "'\n";
    *out << "ERROR: Unknown global in ADRP\n\n";
    exit(-1);
  }

  instExprVarMap[CurInst] = name;
  return name;
}

pair<Value *, uint16_t> mc2llvm::getExprVar(const MCExpr *expr) {
  auto [name, specifier] = MCExprToName(expr);
  int64_t addend = extractAddend(expr);

  *out << "getExprVar = " << name;
  if (addend != 0)
    *out << " + " << addend;
  *out << "\n";

  Value *globalVar = lookupGlobal(name);
  if (!globalVar) {
    *out << "\nERROR: global '" << name << "' not found\n\n";
    exit(-1);
  }

  // Try to resolve (section, offset) to a source BC string global,
  // avoiding GEP into the blob. Handles both addend != 0 (offset into
  // section) and addend == 0 (section base → first string in section).
  if (ObjCtx) {
    std::string section = ObjCtx->getSectionLabel(name);
    if (!section.empty()) {
      if (auto *matched = ObjCtx->lookupGlobalAtOffset(section, (uint64_t)addend)) {
        *out << "  resolved " << name << " + " << addend
             << " to source BC global @" << matched->getName().str() << "\n";
        return {matched, specifier};
      }
    }
  }

  // If there's an addend, generate GEP: getelementptr i8, ptr @base, i64 addend
  if (addend != 0) {
    auto *offsetConst = ConstantInt::get(getIntTy(64), addend);
    auto *baseConst = cast<Constant>(globalVar);
    globalVar = ConstantExpr::getGetElementPtr(getIntTy(8), baseConst, offsetConst);
    *out << "  applied addend " << addend << " via GEP\n";
  }

  return make_pair(globalVar, specifier);
}

Value *mc2llvm::createUSHL(Value *a, Value *b) {
  auto zero = getUnsignedIntConst(0, getBitWidth(b));
  auto c = createICmp(ICmpInst::Predicate::ICMP_SGT, b, zero);
  auto neg = createSub(zero, b);
  auto posRes = createMaskedShl(a, b);
  auto negRes = createMaskedLShr(a, neg);
  return createSelect(c, posRes, negRes);
}

Value *mc2llvm::createSSHL(Value *a, Value *b) {
  auto zero = getUnsignedIntConst(0, getBitWidth(b));
  auto c = createICmp(ICmpInst::Predicate::ICMP_SGT, b, zero);
  auto neg = createSub(zero, b);
  auto posRes = createMaskedShl(a, b);
  auto negRes = createMaskedAShr(a, neg);
  return createSelect(c, posRes, negRes);
}

Value *mc2llvm::rev(Value *in, unsigned eltSize, unsigned amt) {
  assert(eltSize == 8 || eltSize == 16 || eltSize == 32);
  assert(getBitWidth(in) == 64 || getBitWidth(in) == 128);
  if (getBitWidth(in) == 64)
    in = createZExt(in, getIntTy(128));
  Value *rev = getUndefVec(128 / eltSize, eltSize);
  in = createBitCast(in, getVecTy(eltSize, 128 / eltSize));
  for (unsigned i = 0; i < (128 / amt); ++i) {
    auto innerCount = amt / eltSize;
    for (unsigned j = 0; j < innerCount; j++) {
      auto elt = createExtractElement(in, (i * innerCount) + j);
      rev =
          createInsertElement(rev, elt, (i * innerCount) + innerCount - j - 1);
    }
  }
  return rev;
}

Value *mc2llvm::dupElts(Value *v, unsigned numElts, unsigned eltSize) {
  unsigned w = numElts * eltSize;
  assert(w == 64 || w == 128);
  assert(getBitWidth(v) == eltSize);
  Value *res = getUndefVec(numElts, eltSize);
  for (unsigned i = 0; i < numElts; i++)
    res = createInsertElement(res, v, i);
  return res;
}

Value *mc2llvm::concat(Value *a, Value *b) {
  int wa = getBitWidth(a);
  int wb = getBitWidth(b);
  auto wide_a = createZExt(a, getIntTy(wa + wb));
  auto wide_b = createZExt(b, getIntTy(wa + wb));
  auto shifted_a = createRawShl(wide_a, getUnsignedIntConst(wb, wa + wb));
  return createOr(shifted_a, wide_b);
}

Value *mc2llvm::createCheckedUDiv(Value *a, Value *b,
                                  llvm::Value *ifDivByZero) {
  // division by zero is UB for LLVM
  unsigned size = getBitWidth(a);
  unsigned sizeb = getBitWidth(b);
  assert(size == sizeb);

  auto divBB = BasicBlock::Create(Ctx, "", liftedFn);
  auto retBB = BasicBlock::Create(Ctx, "", liftedFn);

  auto intTy = getIntTy(size);
  auto zero = getUnsignedIntConst(0, size);

  auto resPtr = createAlloca(intTy, getUnsignedIntConst(1, 64), "");
  createStore(ifDivByZero, resPtr);
  auto RHSIsZero = createICmp(ICmpInst::Predicate::ICMP_EQ, b, zero);
  createBranch(RHSIsZero, retBB, divBB);

  LLVMBB = divBB;
  auto divResult = createUDiv(a, b);
  createStore(divResult, resPtr);
  createBranch(retBB);

  LLVMBB = retBB;
  auto result = createLoad(intTy, resPtr);
  return result;
}

Value *mc2llvm::createCheckedSDiv(Value *a, Value *b, llvm::Value *ifDivByZero,
                                  llvm::Value *ifOverflow) {
  // division by zero and INT_MIN / -1 is UB for LLVM
  unsigned size = getBitWidth(a);
  unsigned sizeb = getBitWidth(b);
  assert(size == sizeb);

  auto checkOverflowBB = BasicBlock::Create(Ctx, "", liftedFn);
  auto overflowBB = BasicBlock::Create(Ctx, "", liftedFn);
  auto divBB = BasicBlock::Create(Ctx, "", liftedFn);
  auto retBB = BasicBlock::Create(Ctx, "", liftedFn);

  auto intTy = getIntTy(size);
  auto allOnes = getAllOnesConst(size);
  auto zero = getUnsignedIntConst(0, size);

  auto resPtr = createAlloca(intTy, getUnsignedIntConst(1, 64), "");
  createStore(ifDivByZero, resPtr);
  auto RHSIsZero = createICmp(ICmpInst::Predicate::ICMP_EQ, b, zero);
  createBranch(RHSIsZero, retBB, checkOverflowBB);

  LLVMBB = checkOverflowBB;
  auto intMin = getSignedMinConst(size);
  auto LHSIsIntMin = createICmp(ICmpInst::Predicate::ICMP_EQ, a, intMin);
  auto RHSIsAllOnes = createICmp(ICmpInst::Predicate::ICMP_EQ, b, allOnes);
  auto isOverflow = createAnd(LHSIsIntMin, RHSIsAllOnes);
  createBranch(isOverflow, overflowBB, divBB);

  LLVMBB = overflowBB;
  createStore(ifOverflow, resPtr);
  createBranch(retBB);

  LLVMBB = divBB;
  auto divResult = createSDiv(a, b);
  createStore(divResult, resPtr);
  createBranch(retBB);

  LLVMBB = retBB;
  auto result = createLoad(intTy, resPtr);
  return result;
}

Value *mc2llvm::createCheckedURem(Value *a, Value *b,
                                  llvm::Value *ifDivByZero) {
  // division by zero (and thus the remainder) is UB for LLVM
  unsigned size = getBitWidth(a);
  unsigned sizeb = getBitWidth(b);
  assert(size == sizeb);

  auto remBB = BasicBlock::Create(Ctx, "", liftedFn);
  auto retBB = BasicBlock::Create(Ctx, "", liftedFn);

  auto intTy = getIntTy(size);
  auto zero = getUnsignedIntConst(0, size);

  auto resPtr = createAlloca(intTy, getUnsignedIntConst(1, 64), "");
  createStore(ifDivByZero, resPtr);
  auto RHSIsZero = createICmp(ICmpInst::Predicate::ICMP_EQ, b, zero);
  createBranch(RHSIsZero, retBB, remBB);

  LLVMBB = remBB;
  auto remResult = createURem(a, b);
  createStore(remResult, resPtr);
  createBranch(retBB);

  LLVMBB = retBB;
  auto result = createLoad(intTy, resPtr);
  return result;
}

Value *mc2llvm::createCheckedSRem(Value *a, Value *b, llvm::Value *ifDivByZero,
                                  llvm::Value *ifOverflow) {
  // division by zero and INT_MIN / -1 (and thus the remainder) is UB for LLVM
  unsigned size = getBitWidth(a);
  unsigned sizeb = getBitWidth(b);
  assert(size == sizeb);

  auto checkOverflowBB = BasicBlock::Create(Ctx, "", liftedFn);
  auto overflowBB = BasicBlock::Create(Ctx, "", liftedFn);
  auto remBB = BasicBlock::Create(Ctx, "", liftedFn);
  auto retBB = BasicBlock::Create(Ctx, "", liftedFn);

  auto intTy = getIntTy(size);
  auto allOnes = getAllOnesConst(size);
  auto zero = getUnsignedIntConst(0, size);

  auto resPtr = createAlloca(intTy, getUnsignedIntConst(1, 64), "");
  createStore(ifDivByZero, resPtr);
  auto RHSIsZero = createICmp(ICmpInst::Predicate::ICMP_EQ, b, zero);
  createBranch(RHSIsZero, retBB, checkOverflowBB);

  LLVMBB = checkOverflowBB;
  auto intMin = getSignedMinConst(size);
  auto LHSIsIntMin = createICmp(ICmpInst::Predicate::ICMP_EQ, a, intMin);
  auto RHSIsAllOnes = createICmp(ICmpInst::Predicate::ICMP_EQ, b, allOnes);
  auto isOverflow = createAnd(LHSIsIntMin, RHSIsAllOnes);
  createBranch(isOverflow, overflowBB, remBB);

  LLVMBB = overflowBB;
  createStore(ifOverflow, resPtr);
  createBranch(retBB);

  LLVMBB = remBB;
  auto remResult = createSRem(a, b);
  createStore(remResult, resPtr);
  createBranch(retBB);

  LLVMBB = retBB;
  auto result = createLoad(intTy, resPtr);
  return result;
}

void mc2llvm::assertTrue(Value *cond) {
  assert(cond->getType()->getIntegerBitWidth() == 1 && "assert requires i1");
  CallInst::Create(assertDecl, {cond}, "", LLVMBB);
}

void mc2llvm::assertSame(Value *a, Value *b) {
  auto *c = createICmp(ICmpInst::Predicate::ICMP_EQ, a, b);
  assertTrue(c);
}

void mc2llvm::doDirectCall() {
  *out << "in doDirectCall\n";

  auto &op0 = CurInst->getOperand(0);
  assert(op0.isExpr());
  auto [expr, _] = getExprVar(op0.getExpr());
  assert(expr);
  string calleeName = (string)expr->getName();

  *out << " callee is " << calleeName << "\n";

  if (calleeName == "__stack_chk_fail") {
    createTrap();
    return;
  }

  *out << "lifting a direct call, callee is: '" << calleeName << "'\n";
  auto *callee = dyn_cast<Function>(expr);
  assert(callee);

  if (false && callee == liftedFn) {
    *out << "Recursion currently not supported\n\n";
    exit(-1);
  }

  auto llvmInst = getCurLLVMInst();
  CallInst *llvmCI{nullptr};
  if (!llvmInst) {
    *out << "oops, no debuginfo mapping exists\n";
  } else {
    llvmCI = dyn_cast<CallInst>(llvmInst);
    if (!llvmCI)
      *out << "oops, debuginfo gave us something that's not a callinst\n";
  }
  if (!llvmCI && callee->isVarArg()) {
    // Try recovering the source CallInst from the checkInstSupport pre-pass.
    // The arm-lifter tool passes an empty lineMap, so getCurLLVMInst() always
    // returns null for external calls. variadicCallSites is populated during
    // checkInstSupport's iteration of the source function.
    auto it = variadicCallSites.find(calleeName);
    if (it != variadicCallSites.end())
      llvmCI = it->second;
  }

  if (!llvmCI) {
    // No source-side call instruction available (e.g. arm-lifter without
    // source IR). Proceed without attribute info from the source call.
    if (callee->isVarArg())
      *out << "warning: variadic direct call without source CallInst -- "
              "variadic args will not be marshalled\n";
    else
      *out << "warning: no source-side call instruction for direct call\n";

    FunctionCallee FC{callee};
    doCall(FC, llvmCI, calleeName);
    return;
  }

  FunctionCallee FC{callee};
  doCall(FC, llvmCI, calleeName);
}

Instruction *mc2llvm::getCurLLVMInst() {
  return (Instruction *)(CurInst->getLoc().getPointer());
}

void mc2llvm::storeToMemoryValOffset(Value *base, Value *offset, uint64_t size,
                                     Value *val) {
  // Create a GEP instruction based on a byte addressing basis (8 bits)
  // returning pointer to base + offset
  assert(base);
  auto ptr = createGEP(getIntTy(8), base, {offset}, "");

  // Store Value val in the pointer returned by the GEP instruction
  createStore(val, ptr);
}

string mc2llvm::formatInst(const MCInst &I) {
  if (I.getOpcode() == sentinelNOP())
    return "";

  string asmText;
  raw_string_ostream stream(asmText);
  InstPrinter->printInst(&I, 100, "", *STI.get(), stream);
  stream.flush();
  return asmText;
}

void mc2llvm::liftInst(MCInst &I, const string &asmText) {
  *out << "mcinst: " << I.getOpcode() << " = ";
  PrevInst = CurInst;
  CurInst = &I;

  std::string sss;
  llvm::raw_string_ostream ss{sss};
  I.dump_pretty(ss, InstPrinter);
  ss.flush();
  *out << sss << " = " << std::flush;
  *out << asmText;
  *out << std::endl;

  lift(I);
}

void mc2llvm::invalidateReg(unsigned Reg, unsigned Width) {
  auto F = createFreeze(PoisonValue::get(getIntTy(Width)));
  createStore(F, RegFile[Reg]);
}

// create the actual storage associated with a register -- all of its
// asm-level aliases will get redirected here
void mc2llvm::createRegStorage(unsigned Reg, unsigned Width,
                               const string &Name) {
  auto A = createAlloca(getIntTy(Width), getUnsignedIntConst(1, 64), Name);
  auto F = createFreeze(PoisonValue::get(getIntTy(Width)));
  createStore(F, A);
  RegFile[Reg] = A;
}

pair<Function *, Function *> mc2llvm::run() {
  // arm-lifter only path: shared Module mode (ObjCtx non-null) is required.
  // The legacy non-ObjCtx path (originally for backend-tv) is no longer
  // supported in this fork — tools/backend-tv.cpp is not built. Bailing
  // out here avoids partially executing the legacy code paths below.
  if (!ObjCtx) {
    *out << "\nERROR: mc2llvm::run() requires shared Module mode "
            "(ObjCtx must be non-null). The legacy backend-tv path "
            "is no longer supported in this fork.\n";
    out->flush();
    exit(-1);
  }

  // liftedModule->setDataLayout(srcModule->getDataLayout());
  // liftedModule->setTargetTriple(srcModule->getTargetTriple());

  checkSupport(srcFn);
  nameGlobals(srcFn->getParent());
  if (ObjCtx) {
    // Shared Module mode: skip adjustSrc (no refinement check needed).
    // Extract ABI metadata directly from srcFn without mutation.
    auto *origRetTy = srcFn->getReturnType();
    origRetWidth = origRetTy->isVoidTy() ? 0 : DL.getTypeSizeInBits(origRetTy);
    has_ret_attr = srcFn->hasRetAttribute(Attribute::SExt)
                || srcFn->hasRetAttribute(Attribute::ZExt);
  } else {
    srcFn = adjustSrc(srcFn);
  }

  SrcMgr.AddNewSourceBuffer(std::move(MB), llvm::SMLoc());

  InstPrinter =
      Targ->createMCInstPrinter(DefaultTT, 0, *MAI.get(), *MCII.get(), *MRI);
  InstPrinter->setPrintImmHex(true);

  IA = make_unique<MCInstrAnalysis>(MCII.get());

  auto *MCOFI = Targ->createMCObjectFileInfo(*MCCtx.get(), false);
  MCCtx->setObjectFileInfo(MCOFI);

  Str = make_unique<MCStreamerWrapper>(*MCCtx.get(), *IA.get(), *InstPrinter,
                                       *MRI, sentinelNOP(), lineMap, out);
  Str->setUseAssemblerInfoForParsing(true);

  raw_ostream &OSRef = nulls();
  formatted_raw_ostream FOSRef(OSRef);
  Targ->createAsmTargetStreamer(*Str.get(), FOSRef, InstPrinter);

  unique_ptr<MCAsmParser> Parser(
      createMCAsmParser(SrcMgr, *MCCtx.get(), *Str.get(), *MAI.get()));
  assert(Parser);

  unique_ptr<MCTargetAsmParser> TAP(
      Targ->createMCAsmParser(*STI.get(), *Parser, *MCII.get(), MCOptions));
  assert(TAP);
  Parser->setTargetParser(*TAP);

  if (Parser->Run(true)) {
    *out << "\nERROR: AsmParser failed\n";
    exit(-1);
  }

  // Flush any remaining data from the last label in the assembly.
  // addConstant() is called at each emitLabel, but the final label's
  // accumulated data is never flushed because there's no subsequent
  // emitLabel to trigger it.
  Str->addConstant();

  Str->removeEmptyBlocks();
  Str->checkEntryBlock(branchInst());
  Str->generateSuccessors();

  // we'll want this later
  vector<Type *> args{getIntTy(1)};
  FunctionType *assertTy = FunctionType::get(Type::getVoidTy(Ctx), args, false);
  // In shared Module mode, reuse existing @llvm.assert declaration to
  // avoid LLVM auto-suffixing to @llvm.assert.5, @llvm.assert.9, etc.
  if (ObjCtx) {
    assertDecl = LiftedModule->getFunction("llvm.assert");
  }
  if (!assertDecl) {
    assertDecl = Function::Create(assertTy, Function::ExternalLinkage,
                                  "llvm.assert", LiftedModule);
  }
  auto i64 = getIntTy(64);

  // create a fresh function (or reuse existing declaration in shared Module)
  // In shared Module mode, importSrcBcGlobals may have already created a
  // declaration for this function (e.g., @draw). We can't erase it because
  // other globals may reference it. Instead, we create the new function with
  // a temporary name, then replace all uses of the old declaration with the
  // new definition, then erase the old one.
  if (ObjCtx) {
    auto *existingFn = LiftedModule->getFunction(srcFn->getName());
    if (existingFn && existingFn->isDeclaration()) {
      liftedFn =
          Function::Create(srcFn->getFunctionType(),
                           GlobalValue::ExternalLinkage, 0,
                           srcFn->getName() + "$lifted", LiftedModule);
      liftedFn->copyAttributesFrom(srcFn);
      existingFn->replaceAllUsesWith(liftedFn);
      existingFn->eraseFromParent();
      liftedFn->setName(srcFn->getName());
    } else {
      liftedFn =
          Function::Create(srcFn->getFunctionType(),
                           GlobalValue::ExternalLinkage, 0,
                           srcFn->getName(), LiftedModule);
      liftedFn->copyAttributesFrom(srcFn);
    }
  } else {
    liftedFn =
        Function::Create(srcFn->getFunctionType(), GlobalValue::ExternalLinkage,
                         0, srcFn->getName(), LiftedModule);
    liftedFn->copyAttributesFrom(srcFn);
  }

  // create LLVM-side basic blocks
  vector<pair<BasicBlock *, MCBasicBlock *>> BBs;
  {
    long insts = 0;
    for (auto &mbb : Str->MF.BBs) {
      for (auto &inst [[maybe_unused]] : mbb.getInstrs())
        ++insts;
      auto bb = BasicBlock::Create(Ctx, mbb.getName(), liftedFn);
      BBs.push_back(make_pair(bb, &mbb));
    }
    *out << insts << " assembly instructions\n";
    out->flush();
  }

  // default to adding instructions to the entry block
  LLVMBB = BBs[0].first;

  auto *allocTy =
      FunctionType::get(PointerType::get(Ctx, 0), {i64, i64}, false);

  // In shared Module mode, reuse existing @myalloc declaration if
  // importSrcBcGlobals already created one. This avoids LLVM
  // auto-suffixing to @myalloc.2, @myalloc.7, etc.
  if (ObjCtx) {
    myAlloc = LiftedModule->getFunction("myalloc");
  }
  if (!myAlloc) {
    myAlloc = Function::Create(allocTy, GlobalValue::ExternalLinkage, 0,
                               "myalloc", LiftedModule);
  }
  myAllocVH = WeakTrackingVH(myAlloc);
  myAlloc->addRetAttr(Attribute::NonNull);
  AttrBuilder B1(Ctx);
  B1.addAllocKindAttr(AllocFnKind::Alloc);
  B1.addAllocSizeAttr(0, {});
  B1.addAttribute(Attribute::WillReturn);
  myAlloc->addFnAttrs(B1);
  myAlloc->addParamAttr(1, Attribute::AllocAlign);
  myAlloc->addFnAttr("alloc-family", "backend-tv-alloc");
  stackSize = getUnsignedIntConst(stackBytes + (8 * numStackSlots), 64);

  stackMem = CallInst::Create(myAlloc, {stackSize, getUnsignedIntConst(16, 64)},
                              "stack", LLVMBB);

  platformInit();

  // -- Set up DWARF debug info so recompiled code can trace back to ARM asm --
  llvm::DIBuilder DBuilder(*LiftedModule);

  // Module flags: only add if not already present (shared Module may have them)
  if (!LiftedModule->getModuleFlag("Debug Info Version"))
    LiftedModule->addModuleFlag(llvm::Module::Warning, "Debug Info Version",
                                llvm::DEBUG_METADATA_VERSION);
  if (!LiftedModule->getModuleFlag("Dwarf Version"))
    LiftedModule->addModuleFlag(llvm::Module::Warning, "Dwarf Version",
                                llvm::dwarf::DWARF_VERSION);

  auto *DIFile = DBuilder.createFile("arm_asm.s", ".");
  auto *CU = DBuilder.createCompileUnit(
      llvm::dwarf::DW_LANG_C, DIFile, "arm-lifter", false, "", 0);
  auto *SubroutineTy =
      DBuilder.createSubroutineType(DBuilder.getOrCreateTypeArray({}));
  auto *SP = DBuilder.createFunction(
      CU, liftedFn->getName(), llvm::StringRef(), DIFile, 1, SubroutineTy, 0,
      llvm::DINode::FlagPrototyped, llvm::DISubprogram::SPFlagDefinition);
  liftedFn->setSubprogram(SP);

  *out << "\n\nlifting assembly instructions to LLVM\n";

  // Track BBs that existed before lifting. Only BBs created BY lift() will
  // be annotated — platformInit instructions and infrastructure branches
  // (which are in pre-existing BBs) are correctly skipped.
  std::set<llvm::BasicBlock *> preLiftBBs;
  for (auto &bb : *liftedFn)
    preLiftBBs.insert(&bb);

  for (auto &[llvm_bb, mc_bb] : BBs) {
    LLVMBB = llvm_bb;
    MCBB = mc_bb;
    auto &mc_instrs = mc_bb->getInstrs();

    *out << "entering new bb\n";

    for (auto &inst : mc_instrs) {
      llvmInstNum = 0;
      unsigned asmLine = armInstNum + 1; // DWARF line 0 reserved, use 1-based
      auto asmText = formatInst(inst);
      *out << armInstNum << " : about to lift opcode " << inst.getOpcode()
           << " " << (string)InstPrinter->getOpcodeName(inst.getOpcode())
           << "\n";
      if (InstructionMap) {
        InstructionMap->instructions.push_back(
            {.armInstId = armInstNum,
             .dwarfLine = asmLine,
             .mcBlock = mc_bb->getName(),
             .opcode = (string)InstPrinter->getOpcodeName(inst.getOpcode()),
             .opcodeId = inst.getOpcode(),
             .asmText = std::move(asmText)});
        liftInst(inst, InstructionMap->instructions.back().asmText);
      } else {
        liftInst(inst, asmText);
      }
      *out << "    lifted\n";

      // Annotate all BBs created during this lift() with the ARM inst index.
      auto loc = llvm::DILocation::get(Ctx, asmLine, 0, SP);
      for (auto &bb : *liftedFn) {
        if (preLiftBBs.count(&bb))
          continue;                     // old BB: infrastructure, skip
        for (auto &i : bb) {
          if (i.getDebugLoc())
            continue;                   // already annotated (shared BB edge case)
          i.setDebugLoc(loc);
        }
      }

      // Mark all current BBs as "old" for the next lift().
      for (auto &bb : *liftedFn)
        preLiftBBs.insert(&bb);

      ++armInstNum;
    }

    // machine code falls through but LLVM isn't allowed to
    if (!LLVMBB->getTerminator()) {
      auto succs = MCBB->getSuccs().size();
      if (succs == 0) {
        // this should only happen when we have a function with a
        // single, empty basic block, which should only when we
        // started with an LLVM function whose body is something
        // like UNREACHABLE
        doReturn();
      } else if (succs == 1) {
        auto *dst = getBBByName(MCBB->getSuccs()[0]->getName());
        createBranch(dst);
      }
    }
  }

  DBuilder.finalize();

  *out << armInstNum << " assembly instructions\n";

  *out << "encoding counts: ";
  for (auto &[enc, count] : encodingCounts) {
    *out << enc << '=' << count << ',';
  }
  *out << '\n';

  // enabled this if we're emitting broken functions
  if (false)
    LiftedModule->dump();

  // In shared Module mode, skip verifyModule because other functions
  // in the Module may not have been lifted yet, making the module
  // invalid. Verification should be done once all functions are lifted.
  if (!ObjCtx) {
    std::string sss;
    llvm::raw_string_ostream ss(sss);
    if (llvm::verifyModule(*LiftedModule, &ss)) {
      *out << sss << "\n\n";
      out->flush();
      *out << "\nERROR: Lifted module is broken, this should not happen\n";
      exit(-1);
    }
  }

  return make_pair(srcFn, liftedFn);
}

void mc2llvm::avoidArgMD(CallInst *ci, const string &str) {
  auto &Ctx = ci->getContext();
  auto val = MetadataAsValue::get(Ctx, MDString::get(Ctx, str));
  for (auto &arg : ci->args()) {
    if (arg.get() == val) {
      *out << "\nERROR: " << val->getNameOrAsOperand() << " not supported\n\n";
      exit(-1);
    }
  }
}

void mc2llvm::checkInstSupport(Instruction &i, const DataLayout &DL,
                               set<Type *> &typeSet) {
  typeSet.insert(i.getType());
  for (auto &op : i.operands()) {
    auto *ty = op.get()->getType();
    typeSet.insert(ty);
    if (auto *vty = dyn_cast<VectorType>(ty)) {
      typeSet.insert(vty->getElementType());
    }
    if (auto *pty = dyn_cast<PointerType>(ty)) {
      if (pty->getAddressSpace() != 0) {
        *out << "\nERROR: address spaces other than 0 are unsupported\n\n";
        exit(-1);
      }
    }
  }
  if (i.isVolatile()) {
    *out << "\nERROR: volatiles not supported\n\n";
    exit(-1);
  }
  if (i.isAtomic()) {
    *out << "\nERROR: atomics not supported yet\n\n";
    exit(-1);
  }
  if (isa<VAArgInst>(&i)) {
    *out << "\nERROR: va_arg instructions not supported\n\n";
    exit(-1);
  }
  if (isa<InvokeInst>(&i)) {
    *out << "\nERROR: invoke instructions not supported\n\n";
    exit(-1);
  }
  if (auto *ai = dyn_cast<AllocaInst>(&i)) {
    if (!ai->isStaticAlloca()) {
      *out << "\nERROR: only static allocas supported for now\n\n";
      exit(-1);
    }
    auto allocSize = ai->getAllocationSize(DL);
    if (allocSize)
      totalAllocas += allocSize->getFixedValue();
  }
  if (auto *cb = dyn_cast<CallBase>(&i)) {
    if (isa<InlineAsm>(cb->getCalledOperand())) {
      *out << "\nERROR: inline assembly not supported\n\n";
      exit(-1);
    }
  }
  if (auto *ci = dyn_cast<CallInst>(&i)) {

    // non-null as a callsite attribute is problematic for us and we
    // don't think it happens much, just get rid of it
    ci->removeRetAttr(Attribute::NonNull);
    ci->removeRetAttr(Attribute::NoAlias);

    if (auto callee = ci->getCalledFunction()) {
      checkCallingConv(callee);

      if (callee->isIntrinsic()) {
        const auto &name = callee->getName();
        if (callee->isConstrainedFPIntrinsic() ||
            name.contains("llvm.fptrunc.round")) {
          // FIXME we should be able to support these, except round.dynamic
          avoidArgMD(ci, "round.dynamic");
          avoidArgMD(ci, "round.downward");
          avoidArgMD(ci, "round.upward");
          avoidArgMD(ci, "round.towardzero");
          avoidArgMD(ci, "round.tonearestaway");
        }
      } else {
        for (auto arg = callee->arg_begin(); arg != callee->arg_end(); ++arg) {

          if (auto *vTy = dyn_cast<VectorType>(arg->getType()))
            checkVectorTy(vTy);

          if (arg->hasByValAttr()) {
            *out << "\nERROR: we don't support the byval parameter attribute "
                    "yet\n\n";
            exit(-1);
          }
        }
      }

      auto name = (string)callee->getName();

      if (callee->isVarArg()) {
        // Store the CallInst with the most arguments.
        // This works for calls where all variadic params are type-compatible
        // (e.g., all integers). For mixed types (int + double), the max-arg
        // heuristic may pick a CallInst with wider types than the actual
        // assembly instruction, but the readFromRegTyped truncation fix in
        // arm2llvm.cpp prevents that from crashing — only marsed values may
        // differ.
        auto it = variadicCallSites.find(name);
        if (it == variadicCallSites.end() || ci->arg_size() > it->second->arg_size())
          variadicCallSites[name] = ci;
      }

      if (name.find("llvm.memcpy.element.unordered.atomic") != string::npos) {
        *out << "\nERROR: atomic instrinsics not supported\n\n";
        exit(-1);
      }

      if (name.find("llvm.objc") != string::npos) {
        *out << "\nERROR: llvm.objc instrinsics not supported\n\n";
        exit(-1);
      }

      if (name.find("llvm.thread") != string::npos) {
        *out << "\nERROR: llvm.thread instrinsics not supported\n\n";
        exit(-1);
      }

      if ((name.find("llvm.experimental.gc") != string::npos) ||
          (name.find("llvm.experimental.stackmap") != string::npos)) {
        *out << "\nERROR: llvm GC instrinsics not supported\n\n";
        exit(-1);
      }
    } else {
      // indirect call or signature mismatch
      auto co = ci->getCalledOperand();
      assert(co);
      // variadic calls handled by marshallArgs via source-side CallInst
    }
  }
}

void mc2llvm::checkVectorTy(VectorType *Ty) {
  auto *EltTy = Ty->getElementType();
  int Width = -1;
  if (auto *IntTy = dyn_cast<IntegerType>(EltTy)) {
    Width = IntTy->getBitWidth();
    if (Width != 8 && Width != 16 && Width != 32 && Width != 64)
      goto vec_error;
  } else if (EltTy->isFloatTy()) {
    Width = 32;
  } else if (EltTy->isDoubleTy()) {
    Width = 64;
  } else {
    *out << "\nERROR: Only vectors of integer, f32, f64, supported for now\n\n";
    exit(-1);
  }
  {
    auto Count = Ty->getElementCount().getFixedValue();
    auto VecSize = (Count * Width) / 8;
    if (VecSize != 8 && VecSize != 16)
      goto vec_error;
    return;
  }
vec_error:
  *out << "\nERROR: Only short vectors 8 and 16 bytes long are supported, "
          "in parameters and return values; please see Section 5.4 of "
          "AAPCS64 for more details\n\n";
  exit(-1);
}

// Helper to check if an aggregate argument type is supported.
// Supports arrays and structs whose elements/fields are integers, pointers,
// or recursively other supported aggregates.
static bool isSupportedAggregateType(Type *ty) {
  if (ty->isIntegerTy() || ty->isPointerTy())
    return true;
  if (auto *arrTy = dyn_cast<ArrayType>(ty))
    return isSupportedAggregateType(arrTy->getElementType());
  if (auto *structTy = dyn_cast<StructType>(ty)) {
    for (unsigned i = 0; i < structTy->getNumElements(); ++i)
      if (!isSupportedAggregateType(structTy->getElementType(i)))
        return false;
    return true;
  }
  return false;
}

void mc2llvm::checkSupport(Function *srcFn) {
  checkCallingConv(srcFn);
  if (srcFn->getLinkage() ==
      GlobalValue::LinkageTypes::AvailableExternallyLinkage) {
    *out << "\nERROR: function has externally_available linkage type and won't "
            "be codegenned\n\n";
    exit(-1);
  }

  if (srcFn->hasPersonalityFn()) {
    *out << "\nERROR: personality functions not supported\n\n";
    exit(-1);
  }

  for (auto &arg : srcFn->args()) {
    // backend-specific checks go here
    checkArgSupport(arg);

    // rest of this code is checks that apply to all supported backends
    if (arg.hasByValAttr()) {
      *out << "\nERROR: we don't support the byval parameter attribute yet\n\n";
      exit(-1);
    }
    auto *ty = arg.getType();
    if (ty->isStructTy() || ty->isArrayTy()) {
      if (!isSupportedAggregateType(ty)) {
        *out << "\nERROR: unsupported aggregate argument type\n\n";
        exit(-1);
      }
      continue;  // skip scalar width check for aggregates
    }
    auto &DL = srcFn->getParent()->getDataLayout();
    auto orig_width = DL.getTypeSizeInBits(ty);
    if (auto vTy = dyn_cast<VectorType>(ty)) {
      checkVectorTy(vTy);
      if (orig_width > 128) {
        *out << "\nERROR: Vector arguments >128 bits not supported\n\n";
        exit(-1);
      }
    } else {
      if (orig_width > 64) {
        *out << "\nERROR: Unsupported function argument: Only integer / "
                "pointer parameters 64 bits or smaller supported for now\n\n";
        exit(-1);
      }
    }
  }

  if (auto RT = dyn_cast<VectorType>(srcFn->getReturnType()))
    checkVectorTy(RT);

  if (srcFn->getReturnType()->isStructTy()) {
    *out << "\nERROR: we don't support structures in return values yet\n\n";
    exit(-1);
  }
  if (srcFn->getReturnType()->isArrayTy()) {
    *out << "\nERROR: we don't support arrays in return values yet\n\n";
    exit(-1);
  }
  set<Type *> typeSet;
  auto &DL = srcFn->getParent()->getDataLayout();
  unsigned llvmInstCount = 0;

  totalAllocas = 0;
  for (auto &bb : *srcFn) {
    for (auto &i : bb) {
      checkInstSupport(i, DL, typeSet);
      ++llvmInstCount;
    }
  }
  if (totalAllocas >= stackBytes) {
    *out << "ERROR: Stack frame too large, consider increasing "
            "stackBytes\n\n";
    exit(-1);
  }

  for (auto ty : typeSet)
    checkTypeSupport(ty);

  *out << llvmInstCount << " LLVM instructions in source function\n";
}

void mc2llvm::nameGlobals(Module *M) {
  int num = 0;
  for (auto G = M->global_begin(); G != M->global_end(); ++G) {
    if (G->hasName())
      continue;
    G->setName("g" + std::to_string(num++));
  }
  num = 0;
  for (auto &F : *M) {
    if (F.hasName())
      continue;
    F.setName("f" + std::to_string(num++));
  }
}

/*
 * a function that have the sext or zext attribute on its return value
 * is awkward: this obligates the function to sign- or zero-extend the
 * return value. we want to check this, which requires making the
 * function return a wider type, which requires cloning the function.
 *
 * cloning a function is an awkward case and we'd like to thoroughly
 * test that sort of code, so we just unconditionally do that even
 * when we don't actually need to.
 */
Function *mc2llvm::adjustSrc(Function *srcFn) {
  auto *origRetTy = srcFn->getReturnType();

  if (!(origRetTy->isIntegerTy() || origRetTy->isVectorTy() ||
        origRetTy->isPointerTy() || origRetTy->isVoidTy() ||
        origRetTy->isFloatingPointTy())) {
    *out << "\nERROR: Unsupported Function Return Type: Only int, ptr, vec, "
            "float, and void supported for now\n\n";
    exit(-1);
  }

  auto &DL = srcFn->getParent()->getDataLayout();
  origRetWidth = origRetTy->isVoidTy() ? 0 : DL.getTypeSizeInBits(origRetTy);

  if (origRetTy->isVectorTy()) {
    if (origRetWidth > 128) {
      *out << "\nERROR: Unsupported Function Return: vector > 128 bits\n\n";
      exit(-1);
    }
  } else {
    if (origRetWidth > 64) {
      *out << "\nERROR: Scalar return values larger than 64 bits are not "
              "supported\n\n";
      exit(-1);
    }
  }

  // FIXME -- some of this is ARM-specific, we need a hook into
  // target-specific code here

  has_ret_attr = srcFn->hasRetAttribute(Attribute::SExt) ||
                 srcFn->hasRetAttribute(Attribute::ZExt);

  Type *actualRetTy = nullptr;
  if (has_ret_attr && origRetWidth != 32 && origRetWidth != 64) {
    auto *i32 = Type::getIntNTy(srcFn->getContext(), 32);
    auto *i64 = Type::getIntNTy(srcFn->getContext(), 64);

    // build this first to avoid iterator invalidation
    vector<ReturnInst *> RIs;
    for (auto &BB : *srcFn)
      for (auto &I : BB)
        if (auto *RI = dyn_cast<ReturnInst>(&I))
          RIs.push_back(RI);

    for (auto RI : RIs) {
      BasicBlock::iterator it{RI};
      auto retVal = RI->getReturnValue();
      auto Name = retVal->getName();
      if (origRetTy->isVectorTy()) {
        retVal = new BitCastInst(
            retVal, Type::getIntNTy(srcFn->getContext(), origRetWidth),
            Name + "_bitcast", it);
      }
      if (srcFn->hasRetAttribute(Attribute::ZExt)) {
        retVal = new ZExtInst(retVal, i64, Name + "_zext", it);
      } else {
        if (origRetWidth < 32) {
          auto sext = new SExtInst(retVal, i32, Name + "_sext", it);
          retVal = new ZExtInst(sext, i64, Name + "_zext", it);
        } else {
          retVal = new SExtInst(retVal, i64, Name + "_sext", it);
        }
      }
      ReturnInst::Create(srcFn->getContext(), retVal, it);
      RI->eraseFromParent();
    }
    actualRetTy = i64;
  } else {
    actualRetTy = origRetTy;
  }

  FunctionType *NFTy =
      FunctionType::get(actualRetTy, srcFn->getFunctionType()->params(), false);
  Function *NF =
      Function::Create(NFTy, srcFn->getLinkage(), srcFn->getAddressSpace(),
                       srcFn->getName(), srcFn->getParent());

  // TODO -- it's probably not ok to copy all flags this way, we need
  // to be picky and think hard about this
  NF->copyAttributesFrom(srcFn);

  NF->splice(NF->begin(), srcFn);
  NF->takeName(srcFn);
  srcFn->replaceAllUsesWith(NF);

  for (Function::arg_iterator I = srcFn->arg_begin(), E = srcFn->arg_end(),
                              I2 = NF->arg_begin();
       I != E; ++I, ++I2) {
    I->replaceAllUsesWith(&*I2);
  }

  srcFn->eraseFromParent();
  return NF;
}

void mc2llvm::fixupOptimizedTgt(Function *tgt) {
  /*
   * these attributes can be soundly removed, and a good thing too
   * since they cause spurious TV failures in ASM memory mode
   */
  for (auto arg = tgt->arg_begin(); arg != tgt->arg_end(); ++arg) {
    arg->removeAttr(llvm::Attribute::Captures);
    arg->removeAttr(llvm::Attribute::ReadNone);
    arg->removeAttr(llvm::Attribute::ReadOnly);
    arg->removeAttr(llvm::Attribute::WriteOnly);
    // As of July 2025, Alive2 is giving false alarms relating to initializes
    arg->removeAttr(llvm::Attribute::Initializes);
  }

  /*
   * when we originally generated the target function, we allocated
   * its stack memory using a custom allocation function; this is to
   * keep LLVM from making unwarranted assumptions about that memory
   * and optimizing it in undesirable ways. however, Alive doesn't
   * want to see the custom allocator. so, here, before passing target
   * to Alive, we replace it with a regular old alloc
   */
  if (!myAllocVH) {
    return; // myalloc was optimized out
  }

  myAlloc = dyn_cast<Function>(myAllocVH);
  Instruction *myAllocCall{nullptr};
  for (auto &bb : *tgt) {
    for (auto &i : bb) {
      if (auto *ci = dyn_cast<CallInst>(&i)) {
        if (auto callee = ci->getCalledFunction()) {
          if (callee == myAlloc) {
            IRBuilder<> B(&i);
            auto *i8Ty = Type::getInt8Ty(ci->getContext());
            auto *alloca = B.CreateAlloca(i8Ty, 0, stackSize, "stack");
            alloca->setAlignment(Align(16));
            i.replaceAllUsesWith(alloca);
            assert(myAllocCall == nullptr);
            myAllocCall = &i;
          }
        }
      }
    }
  }
  if (myAllocCall)
    myAllocCall->eraseFromParent();
}
