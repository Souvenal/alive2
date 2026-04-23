// Copyright (c) 2026-present Souvenal
// Distributed under the MIT license that can be found in the LICENSE file.
//
// arm-lifter: Lift ARM64 ELF object files to LLVM IR.
// Directly reads ELF .o files, disassembles via MCDisassembler API,
// generates assembly MemoryBuffer (matching generateAsm() format),
// then calls liftFunc().
//
// Usage: arm-lifter <input.o> --src-bc=input.bc [--fn=main] [--output-dir=dir] [options]
//
// Output: if --output-dir/-o is given, writes to <dir>/<input_basename>_<fn>.lifted.ll
//         otherwise prints to stdout.
// If --fn is omitted, all functions in the object file are lifted.

#include "backend_tv/binary_reader.h"
#include "backend_tv/lifter.h"
#include "llvm_util/utils.h"
#include "util/version.h"

#include "llvm/BinaryFormat/ELF.h"
#include "llvm/Bitcode/BitcodeReader.h"
#include "llvm/IR/DataLayout.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/GlobalValue.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/InstIterator.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/MC/MCAsmInfo.h"
#include "llvm/MC/MCContext.h"
#include "llvm/MC/MCDisassembler/MCDisassembler.h"
#include "llvm/MC/MCDisassembler/MCRelocationInfo.h"
#include "llvm/MC/MCDisassembler/MCSymbolizer.h"
#include "llvm/MC/MCExpr.h"
#include "llvm/MC/MCInst.h"
#include "llvm/MC/MCInstPrinter.h"
#include "llvm/MC/MCInstrAnalysis.h"
#include "llvm/MC/MCInstrInfo.h"
#include "llvm/MC/MCObjectFileInfo.h"
#include "llvm/MC/MCRegisterInfo.h"
#include "llvm/MC/MCSubtargetInfo.h"
#include "llvm/MC/MCSymbol.h"
#include "llvm/MC/MCTargetOptions.h"
#include "llvm/MC/TargetRegistry.h"
#include "llvm/Object/ELFObjectFile.h"
#include "llvm/Object/ObjectFile.h"
#include "Target/AArch64/MCTargetDesc/AArch64MCAsmInfo.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/InitLLVM.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Signals.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/TargetParser/Triple.h"
#include "llvm/Transforms/Utils/Cloning.h"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
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

llvm::cl::opt<string>
    opt_fn("fn",
           llvm::cl::desc("Function to lift (omit to lift all functions)"),
           llvm::cl::cat(lifter_cmdargs),
           llvm::cl::Optional);

llvm::cl::opt<string>
    opt_optimize_tgt("optimize-tgt",
                     llvm::cl::desc("Optimize lifted code (default=O3)"),
                     llvm::cl::cat(lifter_cmdargs), llvm::cl::init("O3"));

llvm::cl::opt<string> opt_output_dir(
    "output-dir",
    llvm::cl::desc("Output directory for lifted LLVM IR (default=stdout). "
                   "File is named <input_basename>_<function>.lifted.ll"),
    llvm::cl::cat(lifter_cmdargs));

llvm::cl::alias opt_output_alias("o",
                                 llvm::cl::desc("Alias for --output-dir"),
                                 llvm::cl::aliasopt(opt_output_dir),
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
// ELF -> Assembly generation
// ============================================================================

/// Relocation info extracted from .rela.text
struct RelocInfo {
    uint32_t type;           // ELF relocation type (e.g. R_AARCH64_ADR_PREL_PG_HI21)
    std::string symbolName;  // Target symbol name
    int64_t addend;          // Addend from .rela entry
};

/// Map: (section name, addend offset) → synthetic label name
/// Used to resolve section+addend relocations (e.g. .rodata.str1.1+0x9e → __sec_0)
using SectionOffsetLabels = map<pair<string, uint64_t>, string>;

/// Scan a relocation map and create synthetic labels for section+addend references.
/// When a relocation references a section symbol (name starts with '.') with a
/// non-zero addend, we create a synthetic label like __sec_N so the lifter can
/// find it as a regular global symbol, instead of generating MCBinaryExpr.
/// We also create labels for section+0 references since section names themselves
/// (e.g. .rodata.str1.1) are not findable by mc2llvm::lazyAddGlobal().
static SectionOffsetLabels
buildSectionOffsetLabels(const map<uint64_t, RelocInfo> &relocMap) {
    SectionOffsetLabels labels;
    unsigned idx = 0;
    for (auto &[offset, reloc] : relocMap) {
        if (!reloc.symbolName.empty() && reloc.symbolName.starts_with(".")) {
            auto key = make_pair(reloc.symbolName, (uint64_t)reloc.addend);
            if (labels.find(key) == labels.end()) {
                labels[key] = "__sec_" + to_string(idx++);
            }
        }
    }
    return labels;
}

// Custom MCSymbolizer that resolves addresses to symbol names from our symbol map
// and resolves data relocations (ADRP, LDR, ADD, etc.) using MCSpecifierExpr.
class ELFSymbolizer : public MCSymbolizer {
protected:
    const map<uint64_t, lifter::SymbolInfo> &symMap;
    const map<uint64_t, RelocInfo> &relocMap;
    const SectionOffsetLabels &sectionOffsetLabels;

public:
    ELFSymbolizer(MCContext &Ctx, std::unique_ptr<MCRelocationInfo> RelInfo,
                  const map<uint64_t, lifter::SymbolInfo> &symMap,
                  const map<uint64_t, RelocInfo> &relocMap,
                  const SectionOffsetLabels &sectionOffsetLabels)
        : MCSymbolizer(Ctx, std::move(RelInfo)), symMap(symMap),
          relocMap(relocMap), sectionOffsetLabels(sectionOffsetLabels) {}

    bool tryAddingSymbolicOperand(MCInst &Inst, raw_ostream &CStream,
                                  int64_t Value, uint64_t Address,
                                  bool IsBranch, uint64_t Offset,
                                  uint64_t OpSize, uint64_t InstSize) override {
        // Branch instructions: AArch64 disassembler passes Value as
        // PC-relative offset (not target address), so we must add
        // Address to get the absolute target.
        if (IsBranch) {
            uint64_t targetAddr = (uint64_t)((int64_t)Value + (int64_t)Address);

            // Priority 1: Check relocation map (handles BL to external
            // functions like __ubsan_handle_add_overflow, and also BL add
            // when a R_AARCH64_CALL26 relocation exists).
            auto relocIt = relocMap.find(Address);
            if (relocIt != relocMap.end() &&
                !relocIt->second.symbolName.empty()) {
                MCSymbol *Sym =
                    Ctx.getOrCreateSymbol(relocIt->second.symbolName);
                const MCExpr *Expr = MCSymbolRefExpr::create(Sym, Ctx);
                Inst.addOperand(MCOperand::createExpr(Expr));
                return true;
            }

            // Priority 2: Check symbol map (function entry points)
            auto symIt = symMap.find(targetAddr);
            std::string labelName;
            if (symIt != symMap.end()) {
                labelName = symIt->second.name;
            } else {
                // Priority 3: Local branch within a function — use the
                // .L_<hex_addr> label format that disassembleTextSection
                // emits.
                stringstream ss;
                ss << ".L_" << hex << targetAddr;
                labelName = ss.str();
            }
            MCSymbol *Sym = Ctx.getOrCreateSymbol(labelName);
            const MCExpr *Expr = MCSymbolRefExpr::create(Sym, Ctx);
            Inst.addOperand(MCOperand::createExpr(Expr));
            return true;
        }

        // 2. Non-branch instructions: resolve via relocation map
        //    AArch64 disassembler passes Offset=0, so we must use Address
        //    to look up relocations (Address == instruction address in .text).
        auto it = relocMap.find(Address);
        if (it == relocMap.end())
            return false;

        const auto &reloc = it->second;
        if (reloc.symbolName.empty())
            return false;

        MCSymbol *Sym = Ctx.getOrCreateSymbol(reloc.symbolName);
        const MCExpr *BaseExpr = MCSymbolRefExpr::create(Sym, Ctx);

        // 3. Map ELF relocation type → AArch64 specifier
        AArch64::Specifier spec = AArch64::S_None;
        switch (reloc.type) {
        case ELF::R_AARCH64_ADR_PREL_PG_HI21:
        case ELF::R_AARCH64_ADR_PREL_PG_HI21_NC:
            spec = AArch64::S_ABS_PAGE;
            break;
        case ELF::R_AARCH64_ADR_GOT_PAGE:
            spec = AArch64::S_GOT_PAGE;
            break;
        case ELF::R_AARCH64_ADD_ABS_LO12_NC:
        case ELF::R_AARCH64_LDST8_ABS_LO12_NC:
        case ELF::R_AARCH64_LDST16_ABS_LO12_NC:
        case ELF::R_AARCH64_LDST32_ABS_LO12_NC:
        case ELF::R_AARCH64_LDST64_ABS_LO12_NC:
        case ELF::R_AARCH64_LDST128_ABS_LO12_NC:
            spec = AArch64::S_LO12;
            break;
        case ELF::R_AARCH64_LD64_GOT_LO12_NC:
            spec = AArch64::S_GOT_LO12;
            break;
        case ELF::R_AARCH64_CALL26:
            spec = AArch64::S_CALL;
            break;
        default:
            return false;
        }

        // 4. Handle addend: resolve section+addend as synthetic label
        //    instead of generating MCBinaryExpr. This eliminates the
        //    section+offset expression that mc2llvm cannot handle.
        //    Also resolve section+0 since section symbols are not
        //    findable by lazyAddGlobal().
        if (!reloc.symbolName.empty() && reloc.symbolName.starts_with(".")) {
            // Section symbol → use synthetic label
            auto key = make_pair(reloc.symbolName, (uint64_t)reloc.addend);
            auto labelIt = sectionOffsetLabels.find(key);
            if (labelIt != sectionOffsetLabels.end()) {
                MCSymbol *SynSym = Ctx.getOrCreateSymbol(labelIt->second);
                BaseExpr = MCSymbolRefExpr::create(SynSym, Ctx);
                // No addend needed — the label is at the exact offset
            }
        } else if (reloc.addend != 0) {
            // Non-section symbols with addend: fallback to MCBinaryExpr
            const MCExpr *SpecExpr =
                MCSpecifierExpr::create(BaseExpr, spec, Ctx);
            SpecExpr = MCBinaryExpr::createAdd(
                SpecExpr, MCConstantExpr::create(reloc.addend, Ctx), Ctx);
            Inst.addOperand(MCOperand::createExpr(SpecExpr));
            return true;
        }

        const MCExpr *Expr = MCSpecifierExpr::create(BaseExpr, spec, Ctx);

        Inst.addOperand(MCOperand::createExpr(Expr));
        return true;
    }

    void tryAddingPcLoadReferenceComment(raw_ostream &CStream,
                                          int64_t Value,
                                          uint64_t Address) override {}
};

// Build a relocation map from .rela.text: instruction offset → RelocInfo
static map<uint64_t, RelocInfo>
buildTextRelocMap(SectionRef &textSec, ObjectFile &obj) {
    map<uint64_t, RelocInfo> relocMap;
    // SectionRef::relocations() on .text returns empty for ELF because
    // relocations live in a separate .rela.text section. We need to find
    // the .rela.text section and iterate its relocations instead.
    for (SectionRef sec : obj.sections()) {
        // Check if this section is a relocation section targeting .text
        auto relocatedSecOrErr = sec.getRelocatedSection();
        if (!relocatedSecOrErr)
            continue;
        if (*relocatedSecOrErr == textSec) {
            // This is the .rela.text section — iterate its relocations
            for (const RelocationRef &reloc : sec.relocations()) {
                uint64_t offset = reloc.getOffset();
                uint32_t type = reloc.getType();
                auto symIt = reloc.getSymbol();
                std::string symName;
                if (symIt != obj.symbol_end()) {
                    Expected<StringRef> nameOrErr = symIt->getName();
                    if (nameOrErr)
                        symName = nameOrErr->str();
                }
                int64_t addend = 0;
                auto *ELFObj = dyn_cast<ELFObjectFileBase>(&obj);
                if (ELFObj) {
                    if (auto addendOrErr = ELFRelocationRef(reloc).getAddend())
                        addend = *addendOrErr;
                }
                relocMap[offset] = {type, symName, addend};
            }
            break; // Found the relocation section, no need to continue
        }
    }
    return relocMap;
}

// Disassemble .text section using MCDisassembler API.
// Generates assembly matching generateAsm() output format.
// If fnFilter is non-empty, only the named function is emitted.
std::string disassembleTextSection(SectionRef &sec, ObjectFile &obj,
                                   const llvm::Target *Targ,
                                   const map<uint64_t, lifter::SymbolInfo> &symMap,
                                   const map<uint64_t, RelocInfo> &textRelocMap,
                                   const SectionOffsetLabels &sectionOffsetLabels,
                                   ostream *out,
                                   const string &fnFilter = "") {
    auto secNameOrErr = sec.getName();
    string secName = secNameOrErr ? secNameOrErr->str() : "";
    if (!sec.isText() || secName != ".text")
        return "";

    // Set up MC infrastructure for disassembly
    Triple TheTriple = obj.makeTriple();
    string SubStr;
    auto MRI = unique_ptr<MCRegisterInfo>(Targ->createMCRegInfo(TheTriple));
    if (!MRI) {
        *out << "ERROR: Failed to create MCRegisterInfo\n";
        exit(-1);
    }

    MCTargetOptions MCOptions;
    auto MAI = unique_ptr<MCAsmInfo>(
        Targ->createMCAsmInfo(*MRI, TheTriple, MCOptions));
    if (!MAI) {
        *out << "ERROR: Failed to create MCAsmInfo\n";
        exit(-1);
    }

    auto STI = unique_ptr<MCSubtargetInfo>(
        Targ->createMCSubtargetInfo(TheTriple, DefaultCPU, DefaultFeatures));
    if (!STI) {
        *out << "ERROR: Failed to create MCSubtargetInfo\n";
        exit(-1);
    }

    auto MCII = unique_ptr<MCInstrInfo>(Targ->createMCInstrInfo());
    if (!MCII) {
        *out << "ERROR: Failed to create MCInstrInfo\n";
        exit(-1);
    }

    MCContext Ctx(TheTriple, MAI.get(), MRI.get(), STI.get());
    auto *MCOFI = Targ->createMCObjectFileInfo(Ctx, false);
    Ctx.setObjectFileInfo(MCOFI);

    auto DisAsm = unique_ptr<MCDisassembler>(
        Targ->createMCDisassembler(*STI, Ctx));
    if (!DisAsm) {
        *out << "ERROR: Failed to create MCDisassembler\n";
        exit(-1);
    }

    // Build relocation map for symbolizing ADRP/LDR/ADD data references
    // (now passed in from generateFullAsm to avoid building twice)

    // Set up symbolizer for branch target resolution + data relocation symbolization
    auto RelInfo = Targ->createMCRelocationInfo(TheTriple, Ctx);
    auto Symbolizer = make_unique<ELFSymbolizer>(
        Ctx, std::unique_ptr<MCRelocationInfo>(RelInfo), symMap,
        textRelocMap, sectionOffsetLabels);
    DisAsm->setSymbolizer(std::move(Symbolizer));

    auto IP = unique_ptr<MCInstPrinter>(
        Targ->createMCInstPrinter(TheTriple, 0, *MAI, *MCII, *MRI));
    if (!IP) {
        *out << "ERROR: Failed to create MCInstPrinter\n";
        exit(-1);
    }
    IP->setPrintImmHex(true);

    // Get section contents
    Expected<StringRef> contentsOrErr = sec.getContents();
    if (!contentsOrErr) {
        *out << "ERROR: Failed to get section contents\n";
        exit(-1);
    }
    StringRef bytes = *contentsOrErr;
    uint64_t sectionAddr = sec.getAddress();
    uint64_t sectionSize = sec.getSize();

    // Build a sorted list of function symbols in this section
    struct FuncInfo {
        uint64_t addr;
        string name;
        uint64_t size;
    };
    vector<FuncInfo> funcSymbols;
    for (auto &[addr, info] : symMap) {
        if (info.isFunction && addr >= sectionAddr &&
            addr < sectionAddr + sectionSize) {
            funcSymbols.push_back({addr, info.name, info.size});
        }
    }
    std::sort(funcSymbols.begin(), funcSymbols.end(),
         [](const FuncInfo &a, const FuncInfo &b) { return a.addr < b.addr; });

    // Build a set of function start addresses for quick lookup
    map<uint64_t, int> funcStartMap; // addr -> index in funcSymbols
    // Find the target function index if filtering
    int targetFuncIdx = -1;
    for (int i = 0; i < (int)funcSymbols.size(); ++i) {
        funcStartMap[funcSymbols[i].addr] = i;
        if (!fnFilter.empty() && funcSymbols[i].name == fnFilter)
            targetFuncIdx = i;
    }

    // If filtering and function not found, return empty
    if (!fnFilter.empty() && targetFuncIdx < 0)
        return "";

    // Compute the address range to disassemble
    uint64_t startAddr = sectionAddr;
    uint64_t endAddr = sectionAddr + sectionSize;
    if (!fnFilter.empty()) {
        const auto &fn = funcSymbols[targetFuncIdx];
        startAddr = fn.addr;
        // Use explicit size if available, else use next function start or section end
        if (fn.size > 0)
            endAddr = fn.addr + fn.size;
        else if (targetFuncIdx + 1 < (int)funcSymbols.size())
            endAddr = funcSymbols[targetFuncIdx + 1].addr;
    }

    // Build a local symbol map for the text section: offset -> name
    // (for branch targets, data references, etc.)
    map<uint64_t, string> localSymMap;
    for (auto &[addr, info] : symMap) {
        if (addr >= sectionAddr && addr < sectionAddr + sectionSize) {
            localSymMap[addr] = info.name;
        }
    }

    // Disassemble instruction by instruction
    string result;
    result += "\t.file\t1 \".\" \"input.o\"\n";
    result += "\t.text\n";

    uint64_t addr = startAddr;
    uint64_t instCount = 0;
    int curFuncIdx = -1;
    bool inFunction = false;

    // If filtering, emit the target function header now
    if (!fnFilter.empty()) {
        curFuncIdx = targetFuncIdx;
        inFunction = true;
        const auto &fn = funcSymbols[targetFuncIdx];
        result += "\t.globl\t" + fn.name + "\n";
        result += "\t.p2align\t2\n";
        result += "\t.type\t" + fn.name + ",@function\n";
        result += fn.name + ":\n";
        result += "\t.cfi_startproc\n";
    }

    while (addr < endAddr) {
        // Check if we're at a function boundary (only when not filtering)
        if (fnFilter.empty()) {
            auto funcIt = funcStartMap.find(addr);
            if (funcIt != funcStartMap.end()) {
                // Close previous function if any
                if (inFunction) {
                    result += "\t.cfi_endproc\n";
                    result += "\t.size\t" + funcSymbols[curFuncIdx].name +
                              ", .-" + funcSymbols[curFuncIdx].name + "\n\n";
                }

                curFuncIdx = funcIt->second;
                const auto &fn = funcSymbols[curFuncIdx];
                inFunction = true;

                result += "\t.globl\t" + fn.name + "\n";
                result += "\t.p2align\t2\n";
                result += "\t.type\t" + fn.name + ",@function\n";
                result += fn.name + ":\n";
                result += "\t.cfi_startproc\n";
            }
        }

        // Disassemble one instruction
        MCInst Inst;
        uint64_t Size = 0;
        ArrayRef<uint8_t> Bytes(
            reinterpret_cast<const uint8_t *>(bytes.data() + (addr - sectionAddr)),
            sectionAddr + sectionSize - addr);

        bool DecodeOK = DisAsm->getInstruction(Inst, Size, Bytes, addr, nulls());
        if (!DecodeOK || Size == 0) {
            // Failed to decode — skip 4 bytes (ARM64 instruction size)
            Size = 4;
            addr += Size;
            continue;
        }

        // Add .loc directive (line number for lineMap)
        result += "\t.loc\t1 " + to_string(instCount) + " 0\n";

        // Add address label
        {
            stringstream labelSS;
            labelSS << ".L_" << hex << addr;
            result += labelSS.str() + ":\n";
        }

        // Print the instruction
        string instStr;
        raw_string_ostream instOS(instStr);
        IP->printInst(&Inst, addr, "", *STI, instOS);
        instOS.flush();

        // Trim leading and trailing whitespace
        while (!instStr.empty() && (instStr.front() == ' ' || instStr.front() == '\t'))
            instStr.erase(instStr.begin());
        while (!instStr.empty() && (instStr.back() == ' ' || instStr.back() == '\t' ||
                                    instStr.back() == '\n' || instStr.back() == '\r'))
            instStr.pop_back();
        if (!instStr.empty())
            result += "\t" + instStr + "\n";

        addr += Size;
        ++instCount;
    }

    // Close last function
    if (inFunction) {
        result += "\t.cfi_endproc\n";
        result += "\t.size\t" + funcSymbols[curFuncIdx].name +
                  ", .-" + funcSymbols[curFuncIdx].name + "\n\n";
    }

    return result;
}

// Generate data sections assembly (.rodata, .data, etc.)
// sectionOffsetLabels: maps (section_name, addend) → synthetic label name
//   for offsets referenced via section+addend relocations.
std::string generateDataSections(ObjectFile &obj,
                                 const SectionOffsetLabels &sectionOffsetLabels,
                                 ostream *out) {
    string result;

    for (SectionRef sec : obj.sections()) {
        auto nameOrErr = sec.getName();
        string secName = nameOrErr ? nameOrErr->str() : "";
        if (secName.empty() || secName == ".text" || sec.isBSS())
            continue;

        // Skip section names that are metadata (e.g. .symtab, .strtab, etc.)
        if (secName.starts_with(".") &&
            (secName.find("symtab") != string::npos ||
             secName.find("strtab") != string::npos ||
             secName.find("shstrtab") != string::npos ||
             secName.find("rela") != string::npos ||
             secName.find("note") != string::npos ||
             secName.find("debug") != string::npos ||
             secName.find("comment") != string::npos ||
             secName.find("eh_frame") != string::npos))
            continue;

        // Only process progbits sections
        // SectionRef doesn't have getType(); use ELF section type via ELFSectionRef
        ELFSectionRef elfSec(sec);
        uint64_t type = elfSec.getType();
        if (type != ELF::SHT_PROGBITS)
            continue;

        Expected<StringRef> contentsOrErr = sec.getContents();
        if (!contentsOrErr)
            continue;
        StringRef data = *contentsOrErr;
        if (data.empty())
            continue;

        // Build relocation map for this section
        // NOTE: For ELF, SectionRef::relocations() returns empty because
        // relocations live in separate .rela.* sections. We need to find
        // the corresponding .rela section and iterate it manually,
        // same as buildTextRelocMap() does for .text.
        map<uint64_t, string> relocMap;
        for (SectionRef relaSec : obj.sections()) {
            auto relaSecNameOrErr = relaSec.getName();
            string relaSecName = relaSecNameOrErr ? relaSecNameOrErr->str() : "";
            if (!relaSecName.starts_with(".rela")) continue;
            auto relocatedSecOrErr = relaSec.getRelocatedSection();
            if (!relocatedSecOrErr || *relocatedSecOrErr != sec) continue;
            // Found the .rela section for this data section
            for (const RelocationRef &reloc : relaSec.relocations()) {
                auto kind = lifter::classifyRelocation(reloc);
                if (kind == lifter::RelocKind::Other)
                    continue;
                auto symIt = reloc.getSymbol();
                Expected<StringRef> nameOrErr3 = symIt->getName();
                if (!nameOrErr3) continue;
                relocMap[reloc.getOffset()] = nameOrErr3->str();
            }
            break;
        }

        // Build symbol map for this section
        map<uint64_t, string> secSymMap;
        for (SymbolRef sym : obj.symbols()) {
            auto symSecOrErr = sym.getSection();
            if (!symSecOrErr || *symSecOrErr != sec)
                continue;
            Expected<StringRef> nameOrErr2 = sym.getName();
            Expected<uint64_t> addrOrErr = sym.getAddress();
            if (!nameOrErr2 || !addrOrErr)
                continue;
            uint64_t symOff = *addrOrErr - sec.getAddress();
            secSymMap[symOff] = nameOrErr2->str();
        }

        // Emit section header
        string sectionFlags = "\"a\"";
        if (secName == ".data" || secName.find(".data.") == 0)
            sectionFlags = "\"aw\"";
        if (secName == ".rodata" || secName.find(".rodata.") == 0)
            sectionFlags = "\"a\"";

        result += "\t.section\t" + secName + "," + sectionFlags + ",@progbits\n";

        // Alignment
        Align secAlign = sec.getAlignment();
        if (secAlign > Align(1)) {
            // compute log2 of alignment for .p2align
            unsigned alignVal = secAlign.value();
            unsigned p2align = 0;
            while ((1u << p2align) < alignVal)
                ++p2align;
            result += "\t.p2align\t" + to_string(p2align) + "\n";
        }

        // Build a sorted set of synthetic label offsets for this section
        // so we can break data emission at those positions
        set<uint64_t> synthLabelOffsets;
        for (auto &[key, label] : sectionOffsetLabels) {
            if (key.first == secName)
                synthLabelOffsets.insert(key.second);
        }

        // Emit data with symbols and relocations
        uint64_t offset = 0;
        while (offset < data.size()) {
            // Emit symbol label if one exists here
            auto symIt = secSymMap.find(offset);
            if (symIt != secSymMap.end()) {
                result += symIt->second + ":\n";
            }

            // Emit synthetic label for section+addend relocations
            auto synthIt = sectionOffsetLabels.find(make_pair(secName, offset));
            if (synthIt != sectionOffsetLabels.end()) {
                result += synthIt->second + ":\n";
            }

            // Check if there's a relocation at this offset
            auto relocIt = relocMap.find(offset);
            if (relocIt != relocMap.end()) {
                // Determine relocation size from the relocation type
                // For R_AARCH64_ABS64, it's 8 bytes; for R_AARCH64_ABS32, 4 bytes
                // Find the actual reloc to get the type
                uint64_t relocSize = 8; // default to 64-bit
                // We also need the original relocation to check for
                // section symbols that need synthetic label replacement
                string relocSymName = relocIt->second;
                int64_t relocAddend = 0;
                for (SectionRef relaSec : obj.sections()) {
                    auto relocatedSecOrErr = relaSec.getRelocatedSection();
                    if (!relocatedSecOrErr || *relocatedSecOrErr != sec)
                        continue;
                    for (const RelocationRef &reloc : relaSec.relocations()) {
                        if (reloc.getOffset() != offset)
                            continue;
                        auto kind = lifter::classifyRelocation(reloc);
                        if (kind == lifter::RelocKind::Absolute32)
                            relocSize = 4;
                        // Check for addend
                        auto *ELFObj2 = dyn_cast<ELFObjectFileBase>(&obj);
                        if (ELFObj2) {
                            if (auto addendOrErr2 = ELFRelocationRef(reloc).getAddend())
                                relocAddend = *addendOrErr2;
                        }
                        break;
                    }
                    break;
                }
                // Replace section symbol with synthetic label if available
                if (!relocSymName.empty() && relocSymName.starts_with(".")) {
                    auto synthKey = make_pair(relocSymName, (uint64_t)relocAddend);
                    auto synthIt2 = sectionOffsetLabels.find(synthKey);
                    if (synthIt2 != sectionOffsetLabels.end()) {
                        relocSymName = synthIt2->second;
                    }
                }
                if (relocSize == 8)
                    result += "\t.quad\t" + relocSymName + "\n";
                else
                    result += "\t.long\t" + relocSymName + "\n";
                offset += relocSize;
                continue;
            }

            // Find the next "break point" — the nearest offset where we
            // must emit a label (synthetic or symbol) or relocation
            uint64_t nextBreak = data.size();
            {
                auto nextSym = secSymMap.upper_bound(offset);
                if (nextSym != secSymMap.end() && nextSym->first < nextBreak)
                    nextBreak = nextSym->first;
                auto nextSynth = synthLabelOffsets.upper_bound(offset);
                if (nextSynth != synthLabelOffsets.end() && *nextSynth < nextBreak)
                    nextBreak = *nextSynth;
                auto nextReloc = relocMap.upper_bound(offset);
                if (nextReloc != relocMap.end() && nextReloc->first < nextBreak)
                    nextBreak = nextReloc->first;
            }

            // Try to detect null-terminated string for .asciz
            // Only if: (1) section is rodata, (2) no symbol starts here,
            // (3) no synthetic label at this offset,
            // (4) at least 4 printable chars + null, (5) next symbol
            // doesn't overlap this range, (6) string doesn't cross a
            // break point (label/reloc)
            if (secName.find("rodata") != string::npos &&
                secSymMap.find(offset) == secSymMap.end() &&
                sectionOffsetLabels.find(make_pair(secName, offset)) ==
                    sectionOffsetLabels.end()) {
                // Check if this looks like a C string
                uint64_t strEnd = offset;
                bool isString = false;
                while (strEnd < data.size()) {
                    char c = data[strEnd];
                    if (c == 0) {
                        isString = (strEnd > offset + 2); // at least 3 chars + null
                        break;
                    }
                    if (c < 32 || c > 126) {
                        isString = false;
                        break;
                    }
                    ++strEnd;
                }
                // Check that no symbol overlaps this string
                if (isString) {
                    auto nextSym = secSymMap.upper_bound(offset);
                    if (nextSym != secSymMap.end() && nextSym->first <= strEnd)
                        isString = false;
                }
                // Check that no synthetic label overlaps this string
                if (isString) {
                    auto nextSynth = synthLabelOffsets.upper_bound(offset);
                    if (nextSynth != synthLabelOffsets.end() &&
                        *nextSynth <= strEnd)
                        isString = false;
                }
                // Check that no relocation overlaps this string
                if (isString) {
                    auto nextReloc = relocMap.upper_bound(offset);
                    if (nextReloc != relocMap.end() && nextReloc->first <= strEnd)
                        isString = false;
                }
                if (isString) {
                    string str(data.data() + offset, strEnd - offset);
                    result += "\t.asciz\t\"" + str + "\"\n";
                    offset = strEnd + 1; // skip past null terminator
                    continue;
                }
            }

            // Emit raw bytes — but limit to nextBreak so we don't skip
            // over any labels or relocations
            result += "\t.byte\t";
            unsigned count = 0;
            uint64_t limit = min(nextBreak, (uint64_t)data.size());
            for (uint64_t i = 0; offset + i < limit && count < 8; ++i, ++count) {
                if (i > 0) result += ", ";
                unsigned val = (unsigned)(unsigned char)data[offset + i];
                result += std::to_string(val);
            }
            result += "\n";
            offset += count;
        }

        // Emit .size directives for symbols in this section
        for (SymbolRef sym : obj.symbols()) {
            auto symSecOrErr = sym.getSection();
            if (!symSecOrErr || *symSecOrErr != sec)
                continue;
            Expected<StringRef> nameOrErr2 = sym.getName();
            uint64_t symSize = ELFSymbolRef(sym).getSize();
            if (!nameOrErr2)
                continue;
            if (symSize > 0)
                result += "\t.size\t" + nameOrErr2->str() + ", " +
                          to_string(symSize) + "\n";
        }

        result += "\n";
    }

    return result;
}

// Generate BSS declarations (.comm)
std::string generateBSSDeclarations(ObjectFile &obj) {
    string result;

    for (SectionRef sec : obj.sections()) {
        if (!sec.isBSS())
            continue;
        auto nameOrErr = sec.getName();
        string secName = nameOrErr ? nameOrErr->str() : "";
        if (secName.empty())
            continue;

        // Find symbols in BSS sections
        for (SymbolRef sym : obj.symbols()) {
            Expected<SymbolRef::Type> typeOrErr = sym.getType();
            if (!typeOrErr)
                continue;

            auto symSecOrErr = sym.getSection();
            if (!symSecOrErr || *symSecOrErr != sec)
                continue;

            Expected<StringRef> nameOrErr2 = sym.getName();
            uint64_t symSize = ELFSymbolRef(sym).getSize();
            if (!nameOrErr2)
                continue;

            string symName = nameOrErr2->str();
            if (symSize == 0)
                continue;

            // For BSS, use .comm with alignment
            unsigned align = 3; // default 8-byte alignment (2^3)
            result += "\t.comm\t" + symName + ", " + to_string(symSize) +
                      ", " + to_string(align) + "\n";
        }
    }

    return result;
}

// Generate the full assembly MemoryBuffer from an ELF object file.
// Output format matches generateAsm() from backend-tv.
// If fnFilter is non-empty, only the named function is emitted in .text.
unique_ptr<llvm::MemoryBuffer>
generateFullAsm(ObjectFile &obj, const llvm::Target *Targ,
                const map<uint64_t, lifter::SymbolInfo> &symMap,
                ostream *out, const string &fnFilter = "") {

    string fullAsm;

    // 0. Build relocation map and synthetic labels for section+addend references.
    //    We need the relocMap before disassembly so we can build
    //    sectionOffsetLabels and share it with both disassembly and data
    //    section generation.
    map<uint64_t, RelocInfo> textRelocMap;
    for (SectionRef sec : obj.sections()) {
        auto nameOrErr = sec.getName();
        string secName = nameOrErr ? nameOrErr->str() : "";
        if (sec.isText() && secName == ".text") {
            textRelocMap = buildTextRelocMap(sec, obj);
            break;
        }
    }
    auto sectionOffsetLabels = buildSectionOffsetLabels(textRelocMap);

    // Also build synthetic labels for data section relocations.
    // For example, .data contains: greeting: .quad .rodata.str1.1
    // The ".rodata.str1.1" is a section symbol that the lifter cannot
    // find via lazyAddGlobal(). We replace it with a synthetic label
    // like __sec_N, and emit that label at the start of .rodata.str1.1.
    {
        unsigned nextIdx = sectionOffsetLabels.size();
        for (SectionRef sec : obj.sections()) {
            auto secNameOrErr = sec.getName();
            string secName = secNameOrErr ? secNameOrErr->str() : "";
            if (secName.empty() || secName == ".text" || sec.isBSS())
                continue;
            // Skip metadata sections
            if (secName.starts_with(".") &&
                (secName.find("rela") != string::npos ||
                 secName.find("debug") != string::npos ||
                 secName.find("symtab") != string::npos ||
                 secName.find("strtab") != string::npos ||
                 secName.find("note") != string::npos ||
                 secName.find("comment") != string::npos ||
                 secName.find("eh_frame") != string::npos))
                continue;

            // Find the .rela section for this data section
            for (SectionRef relaSec : obj.sections()) {
                auto relocatedSecOrErr = relaSec.getRelocatedSection();
                if (!relocatedSecOrErr || *relocatedSecOrErr != sec)
                    continue;
                for (const RelocationRef &reloc : relaSec.relocations()) {
                    auto symIt = reloc.getSymbol();
                    if (symIt == obj.symbol_end())
                        continue;
                    Expected<StringRef> nameOrErr = symIt->getName();
                    if (!nameOrErr || nameOrErr->empty())
                        continue;
                    string symName = nameOrErr->str();
                    if (!symName.starts_with("."))
                        continue;
                    auto addendOrErr = ELFRelocationRef(reloc).getAddend();
                    uint64_t addend = addendOrErr ? *addendOrErr : 0;
                    auto key = make_pair(symName, addend);
                    if (sectionOffsetLabels.find(key) == sectionOffsetLabels.end()) {
                        sectionOffsetLabels[key] = "__sec_" + to_string(nextIdx++);
                    }
                }
                break;
            }
        }
    }

    // 1. Disassemble .text section
    for (SectionRef sec : obj.sections()) {
        auto nameOrErr = sec.getName();
        string secName = nameOrErr ? nameOrErr->str() : "";
        if (sec.isText() && secName == ".text") {
            fullAsm += disassembleTextSection(sec, obj, Targ, symMap,
                                              textRelocMap, sectionOffsetLabels,
                                              out, fnFilter);
        }
    }

    // 2. Generate data sections
    fullAsm += generateDataSections(obj, sectionOffsetLabels, out);

    // 3. Generate BSS declarations
    fullAsm += generateBSSDeclarations(obj);

    return llvm::MemoryBuffer::getMemBufferCopy(fullAsm);
}

// ============================================================================
// Main lifting pipeline
// ============================================================================

/// Lift a single function from the object file.
/// @param obj       Open ELF ObjectFile
/// @param Targ      Resolved target
/// @param symMap    Pre-built symbol map
/// @param srcModule src-bc module with complete function signatures and
///                  extern declarations
/// @param fnName    Name of the function to lift
/// @param Context   Shared LLVMContext (for Type* compatibility)
/// @param out       Diagnostic output stream
/// @return true on success, false on error
bool liftOneFunction(ObjectFile &obj, const llvm::Target *Targ,
                     const map<uint64_t, lifter::SymbolInfo> &symMap,
                     Module *srcModule, const string &fnName,
                     LLVMContext &Context, ostream *out) {

    // Generate complete assembly MemoryBuffer (filtered to this function)
    auto AsmBuffer = generateFullAsm(obj, Targ, symMap, out, fnName);

    if (opt_show_asm) {
        *out << "\n\n------------ Assembly for " << fnName
             << ": ------------\n\n";
        *out << string(AsmBuffer->getBuffer());
        *out << "-------------" << std::endl;
    }

    // Create synthetic source function and call liftFunc.
    // We build a synthetic srcModule (separate from the src-bc module)
    // because liftFunc's adjustSrc() mutates srcFn (splice, return type
    // change), and we don't want to modify the shared src-bc module.
    auto syntheticModule = make_unique<Module>("synthetic", Context);
    syntheticModule->setTargetTriple(DefaultTT);
    syntheticModule->setDataLayout(DefaultDL);

    // Copy function declarations from src-bc module to synthetic module.
    // This provides the lifter with complete signatures for:
    //   - Other defined functions (for bl/call instructions)
    //   - External function declarations (declare @foo(...))
    for (auto &F : *srcModule) {
        if (F.getName() == fnName)
            continue; // current function handled separately below
        Function::Create(F.getFunctionType(), GlobalValue::ExternalLinkage,
                         F.getName(), syntheticModule.get());
    }

    // Copy extern global variable declarations from src-bc module.
    // This replaces the ELF UND heuristic (which used i8 for all globals)
    // with precise types from the bitcode (e.g., @bar = external global i32).
    for (auto &G : srcModule->globals()) {
        if (G.isDeclaration()) {
            new GlobalVariable(*syntheticModule, G.getValueType(),
                               false, GlobalValue::ExternalLinkage,
                               nullptr, G.getName());
        }
    }

    // Get the target function from src-bc module and create srcFn
    Function *srcFnDecl = srcModule->getFunction(fnName);
    if (!srcFnDecl) {
        *out << "ERROR: Function '" << fnName
             << "' not found in src-bc module\n";
        return false;
    }
    FunctionType *fty = srcFnDecl->getFunctionType();
    *out << "src-bc: signature for " << fnName << ": ";
    if (fty->getReturnType() == Type::getVoidTy(Context))
        *out << "void";
    else {
        std::string typeStr;
        llvm::raw_string_ostream rso(typeStr);
        fty->getReturnType()->print(rso);
        *out << rso.str();
    }
    *out << "(" << fty->getNumParams() << " args)\n";
    Function *srcFn = Function::Create(fty, GlobalValue::ExternalLinkage, fnName,
                                       syntheticModule.get());
    BasicBlock *entry = BasicBlock::Create(Context, "entry", srcFn);
    ReturnInst::Create(Context, entry);

    // Call liftFunc (same as backend-tv)
    unordered_map<unsigned, Instruction *> lineMap; // empty
    auto [F1, F2] = lifter::liftFunc(srcFn, std::move(AsmBuffer), lineMap,
                                      opt_optimize_tgt, out, Targ, DefaultTT,
                                      DefaultCPU, DefaultFeatures);

    if (run_replace_ptrtoint) {
        tryReplacePtrtoInt(F2);
    }

    // Set all global variables to weak linkage to avoid duplicate symbol
    // errors when linking multiple .ll files that reference the same global.
    for (auto &GV : F2->getParent()->globals()) {
        if (GV.hasInitializer() && !GV.isDeclaration()) {
            GV.setLinkage(GlobalValue::WeakAnyLinkage);
        }
    }

    // Remove NoCreateUndefOrPoison attribute from all functions in the module.
    // This is a LLVM 20+ attribute that older LLVM versions (e.g. zig cc) don't
    // recognize, causing "unterminated attribute group" parse errors when
    // recompiling the lifted .ll files.
    for (auto &F : *F2->getParent()) {
        F.removeFnAttr(Attribute::NoCreateUndefOrPoison);
    }

    // Output
    auto lifted = lifter::moduleToString(F2->getParent());
    if (opt_output_dir != "") {
        namespace fs = std::filesystem;
        fs::path inputPath = fs::path(std::string(opt_input));
        std::string inputBasename = inputPath.stem().string();
        std::string outputFilename =
            inputBasename + "_" + fnName + ".lifted.ll";
        fs::path outputDir = fs::path(std::string(opt_output_dir));
        fs::create_directories(outputDir);
        fs::path outputPath = outputDir / outputFilename;

        ofstream of(outputPath.string());
        of << lifted;
        of.close();
        *out << "Lifted IR saved to " << outputPath.string() << "\n";
    } else {
        *out << "\n\n------------ Lifted IR for " << fnName
             << ": ------------\n\n";
        *out << lifted;
        *out << "\n-------------\n";
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
    //    and extern declarations, replacing DWARF-based signature extraction)
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

    // 5. Determine which functions to lift based on --fn
    //    Functions with definitions (non-declarations) in srcModule are candidates.
    vector<string> funcsToLift;

    if (opt_fn != "") {
        // Single function mode — validate existence in srcModule
        Function *fn = srcModule->getFunction(std::string(opt_fn));
        if (!fn) {
            *out << "ERROR: Function '" << opt_fn
                 << "' not found in src-bc module\n";
            exit(-1);
        }
        if (fn->isDeclaration()) {
            *out << "ERROR: Function '" << opt_fn
                 << "' is only declared (not defined) in src-bc module — "
                 << "cannot lift an external declaration\n";
            exit(-1);
        }
        funcsToLift.push_back(std::string(opt_fn));
    } else {
        // All-functions mode — lift all defined functions in srcModule
        for (auto &F : *srcModule) {
            if (!F.isDeclaration())
                funcsToLift.push_back(string(F.getName()));
        }
        if (funcsToLift.empty()) {
            *out << "ERROR: No defined functions found in src-bc module\n";
            exit(-1);
        }
        *out << "Lifting " << funcsToLift.size() << " function(s) from "
             << opt_input << "\n";
    }

    // 6. Lift each function
    for (const auto &fnName : funcsToLift) {
        *out << "\n========== Lifting function: " << fnName
             << " ==========\n";
        if (!liftOneFunction(obj, Targ, symMap, srcModule.get(), fnName,
                             sharedCtx, out)) {
            *out << "ERROR: Failed to lift function '" << fnName << "'\n";
            exit(-1);
        }
    }

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
