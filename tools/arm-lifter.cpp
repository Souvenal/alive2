// Copyright (c) 2026-present Souvenal
// Distributed under the MIT license that can be found in the LICENSE file.
//
// arm-lifter: Lift ARM64 ELF object files to LLVM IR.
// Directly reads ELF .o files, disassembles via MCDisassembler API,
// generates assembly MemoryBuffer (matching generateAsm() format),
// then calls liftFunc().
//
// Usage: arm-lifter <input.o> --src-bc=input.bc [-o output_file] [options]
//
// Output: if -o/--output-file is given, writes to that file path.
//         Otherwise, writes to <input_basename>.lifted.ll in the current
//         directory.
// All functions are lifted into a single shared Module, so cross-function
// calls become direct calls and global variables use complete src-bc definitions.

#include "lifter_util/binary_reader.h"
#include "lifter_util/lifter_cleanup.h"
#include "lifter_util/obj2asm.h"
#include "lifter_util/object_lift_context.h"
#include "backend_tv/lifter.h"
#include "llvm_util/utils.h"
#include "util/version.h"

#include "llvm/Bitcode/BitcodeReader.h"
#include "llvm/IR/DataLayout.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/GlobalValue.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/InstIterator.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/MC/TargetRegistry.h"
#include "llvm/Object/ELFObjectFile.h"
#include "llvm/Object/ObjectFile.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/InitLLVM.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Signals.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/TargetParser/Triple.h"
#include "llvm/Transforms/Utils/Cloning.h"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <utility>

using namespace std;
using namespace llvm;
using namespace llvm::object;

namespace {

llvm::cl::OptionCategory lifter_cmdargs("ARM lifter options");

llvm::cl::opt<string> opt_input(llvm::cl::Positional,
                                llvm::cl::desc("<input ELF .o file>"),
                                llvm::cl::cat(lifter_cmdargs),
                                llvm::cl::Required);

llvm::cl::opt<string> opt_output_file(
    "output-file",
    llvm::cl::desc("Output file path for lifted LLVM IR "
                   "(default=<input_basename>.lifted.ll in current directory)"),
    llvm::cl::cat(lifter_cmdargs));

llvm::cl::alias opt_output_alias("o",
                                 llvm::cl::desc("Alias for --output-file"),
                                 llvm::cl::aliasopt(opt_output_file),
                                 llvm::cl::cat(lifter_cmdargs));

llvm::cl::opt<bool> run_replace_ptrtoint(
    "run-replace-ptrtoint",
    llvm::cl::desc(
        "Replace ptr-int round trips with single GEP (default=true)"),
    llvm::cl::init(true), llvm::cl::cat(lifter_cmdargs));

llvm::cl::opt<string> opt_src_bc(
    "src-bc",
    llvm::cl::desc("<input .bc file> — LLVM Bitcode with function signatures "
                   "and extern declarations (required)"),
    llvm::cl::cat(lifter_cmdargs), llvm::cl::Required);

llvm::cl::opt<bool> opt_show_asm(
    "show-asm",
    llvm::cl::desc("Print the generated assembly to stdout (default=false)"),
    llvm::cl::init(false), llvm::cl::cat(lifter_cmdargs));

static const llvm::Triple DefaultTT = llvm::Triple("aarch64-unknown-linux-gnu");
static const char *const DefaultDL =
    "e-m:e-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32";
static const char *const DefaultCPU = "generic";
static const char *const DefaultFeatures = "";

// Given an intToPtr instruction, checks for a round trip
// from a ptrToInt instruction, then replaces with a single GEP instruction.
bool tryReplaceRoundTrip(llvm::IntToPtrInst *intToPtr) {
    assert(intToPtr);

    auto *op_inst = dyn_cast<llvm::Instruction>(intToPtr->getOperand(0));
    if (!op_inst || op_inst->getOpcode() != llvm::Instruction::Add ||
        op_inst->getNumUses() > 1)
        return false;

    bool ptrOnLeft = true;
    auto *ptrToInt = dyn_cast<llvm::PtrToIntInst>(op_inst->getOperand(0));

    if (!ptrToInt) {
        ptrOnLeft = false;
        ptrToInt = dyn_cast<llvm::PtrToIntInst>(op_inst->getOperand(1));
    }

    if (!ptrToInt || ptrToInt->getNumUses() > 1)
        return false;

    llvm::IRBuilder<> B(intToPtr);

    llvm::Value *gep =
        B.CreateGEP(B.getInt8Ty(), ptrToInt->getOperand(0),
                    {op_inst->getOperand(ptrOnLeft ? 1 : 0)}, "");

    intToPtr->replaceAllUsesWith(gep);
    intToPtr->eraseFromParent();
    op_inst->eraseFromParent();
    ptrToInt->eraseFromParent();

    return true;
}

// find and collapse sequences of the form ptrToInt, add, intToPtr
// into a single GEP instruction.
void tryReplacePtrtoInt(llvm::Function *fn) {
    for (auto it = instructions(*fn).begin(), end = instructions(*fn).end();
         it != end;) {
        llvm::Instruction &Inst = *it++;
        if (auto *intToPtr = dyn_cast<llvm::IntToPtrInst>(&Inst)) {
            tryReplaceRoundTrip(intToPtr);
        }
    }
}

// ============================================================================
// Main lifting pipeline
// ============================================================================

/// Lift a single function from the object file into the shared Module.
/// @param obj       Open ELF ObjectFile
/// @param Targ      Resolved target
/// @param symMap    Pre-built symbol map
/// @param srcModule src-bc module with complete function signatures and
///                  extern declarations
/// @param fnName    Name of the function to lift
/// @param SharedModule Shared Module to lift into
/// @param ObjCtx    ObjectLiftContext for global variable lookup
/// @param out       Diagnostic output stream
/// @return true on success, false on error
bool liftOneFunction(ObjectFile &obj, const llvm::Target *Targ,
                     const map<uint64_t, lifter::SymbolInfo> &symMap,
                     Module *srcModule, const string &fnName,
                     Module &SharedModule,
                     ObjectLiftContext &ObjCtx, ostream *out) {

    // Generate complete assembly MemoryBuffer (filtered to this function)
    obj2asm::Obj2Asm converter(Targ, out);
    auto AsmBuffer = converter.convertFullAsm(obj, symMap, fnName);

    if (opt_show_asm) {
        *out << "\n\n------------ Assembly for " << fnName
             << ": ------------\n\n";
        *out << string(AsmBuffer->getBuffer());
        *out << "-------------" << std::endl;
    }

    // Get the target function from src-bc module and build LiftMeta
    Function *srcFn = srcModule->getFunction(fnName);
    if (!srcFn) {
        *out << "ERROR: Function '" << fnName
             << "' not found in src-bc module\n";
        return false;
    }
    // Call liftFuncToModule — lifts into SharedModule, adjustSrc is skipped.
    unordered_map<unsigned, Instruction *> lineMap; // empty
    Function *F2 = lifter::liftFuncToModule(
        srcFn, std::move(AsmBuffer), lineMap, out, Targ,
        DefaultTT, DefaultCPU, DefaultFeatures, SharedModule, ObjCtx);

    if (!F2) {
        *out << "ERROR: liftFuncToModule returned null for '" << fnName
             << "'\n";
        return false;
    }

    if (run_replace_ptrtoint) {
        tryReplacePtrtoInt(F2);
    }

    return true;
}

void runLifter(ostream *out) {
    // 1. Open ELF .o
    auto BufferOrErr = MemoryBuffer::getFile(opt_input);
    if (!BufferOrErr) {
        *out << "ERROR: Cannot open input file: " << opt_input << "\n";
        exit(-1);
    }

    auto ObjOrErr = ObjectFile::createObjectFile(*BufferOrErr->get());
    if (!ObjOrErr) {
        *out << "ERROR: Cannot create ObjectFile: " << opt_input << "\n";
        exit(-1);
    }

    ObjectFile &obj = *ObjOrErr->get();

    // Verify it's an ELF AArch64 object
    if (obj.getArch() != Triple::aarch64) {
        *out << "ERROR: Only AArch64 ELF objects are supported\n";
        exit(-1);
    }

    auto *ELFObj = dyn_cast<ELFObjectFileBase>(&obj);
    if (!ELFObj) {
        *out << "ERROR: Input must be an ELF object file\n";
        exit(-1);
    }

    // 2. Initialize target
    Triple TheTriple = obj.makeTriple();
    string Error;
    const auto *Targ = TargetRegistry::lookupTarget(TheTriple, Error);
    if (!Targ) {
        *out << "Can't lookup target: " << Error;
        exit(-1);
    }

    // 3. Build symbol map
    auto symMap = lifter::buildSymbolMap(obj);

    // 4. Load src-bc module (LLVM Bitcode with complete function signatures
    //    and extern declarations)
    LLVMContext sharedCtx;

    auto bcBufferOrErr = MemoryBuffer::getFile(opt_src_bc);
    if (!bcBufferOrErr) {
        *out << "ERROR: Cannot open src-bc file: " << opt_src_bc << "\n";
        exit(-1);
    }

    auto srcModuleOrErr = parseBitcodeFile(
        bcBufferOrErr->get()->getMemBufferRef(), sharedCtx);
    if (!srcModuleOrErr) {
        *out << "ERROR: Failed to parse bitcode file: " << opt_src_bc << "\n";
        exit(-1);
    }
    unique_ptr<Module> srcModule = std::move(*srcModuleOrErr);

    // 5. Collect functions to lift from the object file's symbol table.
    //    The object may be optimized and have fewer functions than src-bc
    //    (inlining, DCE). The object is the ground truth for what exists.
    vector<string> funcsToLift;
    for (auto &[addr, info] : symMap) {
        if (info.isFunction && !info.name.empty())
            funcsToLift.push_back(info.name);
    }
    if (funcsToLift.empty()) {
        *out << "ERROR: No function symbols found in object file\n";
        exit(-1);
    }
    *out << "Lifting " << funcsToLift.size() << " function(s) from "
         << opt_input << "\n";

    // 6. Create shared Module and ObjectLiftContext
    auto SharedModule = make_unique<Module>("lifted", sharedCtx);
    SharedModule->setTargetTriple(DefaultTT);
    SharedModule->setDataLayout(DefaultDL);

    ObjectLiftContext ObjCtx(*SharedModule);
    ObjCtx.importSrcBcGlobals(*srcModule);

    // 7. Lift each function into the shared Module
    for (const auto &fnName : funcsToLift) {
        *out << "\n========== Lifting function: " << fnName
             << " ==========\n";
        if (!liftOneFunction(obj, Targ, symMap, srcModule.get(), fnName,
                             *SharedModule, ObjCtx, out)) {
            *out << "ERROR: Failed to lift function '" << fnName << "'\n";
            exit(-1);
        }
    }

    // Clean up register init boilerplate via safe function-local passes.
    // Does NOT do inlining or IPO — preserves original call structure.
    lifter::cleanup_module(*SharedModule);

    // Set all defined global variables to weak linkage to avoid duplicate
    // symbol errors when linking the lifted .ll with other object files.
    for (auto &GV : SharedModule->globals()) {
        if (GV.hasInitializer() && !GV.isDeclaration()) {
            GV.setLinkage(GlobalValue::WeakAnyLinkage);
        }
    }

    // Also remove NoCreateUndefOrPoison attribute from all functions.
    // This is a LLVM 20+ attribute that older LLVM versions (e.g. zig cc) don't
    // recognize, causing "unterminated attribute group" parse errors.
    for (auto &F : *SharedModule) {
        F.removeFnAttr(Attribute::NoCreateUndefOrPoison);
    }

    // Strip target-specific memory location names (target_mem0, target_mem1)
    // from the memory() attribute on all functions. These are AArch64-backend-
    // specific memory location annotations that the x86 backend cannot parse.
    // We need to promote target_mem locations to the aggregate value ("other")
    // because the .ll printer only omits locations whose ModRef matches Other.
    for (auto &F : *SharedModule) {
        MemoryEffects ME = F.getMemoryEffects();
        ModRefInfo OtherMR = ME.getModRef(IRMemLocation::Other);
        MemoryEffects Cleaned = ME.getWithModRef(IRMemLocation::TargetMem0, OtherMR)
                                  .getWithModRef(IRMemLocation::TargetMem1, OtherMR);
        F.setMemoryEffects(Cleaned);
    }

    // 9. Output the complete shared Module as a single .ll file
    auto lifted = lifter::moduleToString(SharedModule.get());

    namespace fs = std::filesystem;
    fs::path outputPath;
    if (opt_output_file != "") {
        outputPath = fs::path(std::string(opt_output_file));
    } else {
        fs::path inputPath = fs::path(std::string(opt_input));
        outputPath = fs::path(inputPath.stem().string() + ".lifted.ll");
    }

    if (outputPath.has_parent_path() && !outputPath.parent_path().empty()) {
        fs::create_directories(outputPath.parent_path());
    }

    ofstream of(outputPath.string());
    of << lifted;
    of.close();
    *out << "Lifted IR saved to " << outputPath.string() << "\n";

    *out << "\n========== Done: lifted " << funcsToLift.size()
         << " function(s) successfully ==========\n";
}

} // anonymous namespace

int main(int argc, char **argv) {
    llvm::InitLLVM X(argc, argv);
    llvm::EnableDebugBuffering = true;

    std::string Usage =
        R"EOF(ARM64 ELF lifter — lift ELF .o files to LLVM IR:
version )EOF";
    Usage += util::alive_version;

    llvm::cl::HideUnrelatedOptions(lifter_cmdargs);
    llvm::cl::ParseCommandLineOptions(argc, argv, Usage);

    // Initialize AArch64 target backend
    LLVMInitializeAArch64TargetInfo();
    LLVMInitializeAArch64Target();
    LLVMInitializeAArch64TargetMC();
    LLVMInitializeAArch64AsmParser();
    LLVMInitializeAArch64AsmPrinter();
    LLVMInitializeAArch64Disassembler();

    runLifter(&cout);

    return 0;
}
