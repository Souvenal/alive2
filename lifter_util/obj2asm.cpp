// Obj2Asm: Convert ELF object files (.o) to re-assemblable GAS assembly (.s).
//
// Reconstructs the information lost during compilation — symbol names,
// relocation specifiers, section attributes — from the ELF binary and
// emits assembly that MCAsmParser can parse back into the lifting pipeline.

#include "lifter_util/obj2asm.h"

#include "llvm/BinaryFormat/ELF.h"
#include "llvm/MC/MCAsmBackend.h"
#include "llvm/MC/MCCodeEmitter.h"
#include "llvm/MC/MCContext.h"
#include "llvm/MC/MCInstPrinter.h"
#include "llvm/MC/MCStreamer.h"
#include "llvm/MC/MCDisassembler/MCDisassembler.h"
#include "llvm/MC/MCDisassembler/MCRelocationInfo.h"
#include "llvm/MC/MCDisassembler/MCSymbolizer.h"
#include "llvm/MC/MCExpr.h"
#include "llvm/MC/MCInst.h"
#include "llvm/MC/MCInstPrinter.h"
#include "llvm/MC/MCInstrInfo.h"
#include "llvm/MC/MCObjectFileInfo.h"
#include "llvm/MC/MCRegisterInfo.h"
#include "llvm/MC/MCSectionELF.h"
#include "llvm/MC/MCSubtargetInfo.h"
#include "llvm/MC/MCSymbol.h"
#include "llvm/MC/MCTargetOptions.h"
#include "llvm/MC/TargetRegistry.h"
#include "llvm/Object/ELFObjectFile.h"
#include "llvm/Object/ObjectFile.h"
#include "llvm/Support/Endian.h"
#include "llvm/Support/MathExtras.h"
#include "llvm/Support/MemoryBuffer.h"
#include "Target/AArch64/MCTargetDesc/AArch64MCAsmInfo.h"

#include <algorithm>
#include <cstdlib>
#include <set>
#include <sstream>

using namespace std;
using namespace llvm;
using namespace llvm::object;

namespace obj2asm {

static const char *const DefaultCPU = "generic";
static const char *const DefaultFeatures = "";

// ============================================================================
// Pimpl structure — holds all MC infrastructure with complete types
// ============================================================================

struct Obj2Asm::Impl {
  const llvm::Target *Targ;
  std::ostream *Out;

  // Per-conversion state (created in convertFullAsm)
  std::string ResultStr;
  std::unique_ptr<llvm::raw_string_ostream> OS;
  std::unique_ptr<llvm::MCContext> Ctx;
  std::unique_ptr<llvm::MCRegisterInfo> MRI;
  std::unique_ptr<llvm::MCAsmInfo> MAI;
  std::unique_ptr<llvm::MCSubtargetInfo> STI;
  std::unique_ptr<llvm::MCInstrInfo> MCII;
  std::unique_ptr<llvm::MCObjectFileInfo> MCOFI;
  std::unique_ptr<llvm::MCStreamer> Streamer;
};

// ============================================================================
// Constructor / Destructor
// ============================================================================

Obj2Asm::Obj2Asm(const llvm::Target *Targ, std::ostream *out)
    : P(std::make_unique<Impl>()) {
  P->Targ = Targ;
  P->Out = out;
}

Obj2Asm::~Obj2Asm() = default;

// ============================================================================
// ELF parsing helpers
// ============================================================================

map<uint64_t, RelocInfo>
buildTextRelocMap(SectionRef &textSec, ObjectFile &obj) {
  map<uint64_t, RelocInfo> relocMap;
  for (SectionRef sec : obj.sections()) {
    auto relocatedSecOrErr = sec.getRelocatedSection();
    if (!relocatedSecOrErr)
      continue;
    if (*relocatedSecOrErr == textSec) {
      for (const RelocationRef &reloc : sec.relocations()) {
        uint64_t offset = reloc.getOffset();
        uint32_t type = reloc.getType();
        auto symIt = reloc.getSymbol();
        string symName;
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
      break;
    }
  }
  return relocMap;
}

SectionOffsetLabels
buildSectionOffsetLabels(const map<uint64_t, RelocInfo> &relocMap) {
  SectionOffsetLabels labels;
  unsigned idx = 0;
  for (auto &[offset, reloc] : relocMap) {
    if (reloc.symbolName.empty())
      continue;
    // Create synthetic labels for section symbols (".rodata"+0x9e) that
    // have no corresponding label in the assembly output. Regular symbols
    // with non-zero addends ("maze"+5) are instead handled via MCBinaryExpr
    // in tryAddingSymbolicOperand(), which mc2llvm resolves via GEP.
    if (reloc.symbolName.starts_with(".")) {
      auto key = make_pair(reloc.symbolName, (uint64_t)reloc.addend);
      if (labels.find(key) == labels.end()) {
        labels[key] = "__sec_" + to_string(idx++);
      }
    }
  }
  return labels;
}

// ============================================================================
// Custom MCSymbolizer for ELF relocation resolution
// ============================================================================

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
    if (IsBranch) {
      uint64_t targetAddr = (uint64_t)((int64_t)Value + (int64_t)Address);

      auto relocIt = relocMap.find(Address);
      if (relocIt != relocMap.end() &&
          !relocIt->second.symbolName.empty()) {
        MCSymbol *Sym = Ctx.getOrCreateSymbol(relocIt->second.symbolName);
        const MCExpr *Expr = MCSymbolRefExpr::create(Sym, Ctx);
        Inst.addOperand(MCOperand::createExpr(Expr));
        return true;
      }

      auto symIt = symMap.find(targetAddr);
      string labelName;
      if (symIt != symMap.end()) {
        labelName = symIt->second.name;
      } else {
        stringstream ss;
        ss << ".L_" << hex << targetAddr;
        labelName = ss.str();
      }
      MCSymbol *Sym = Ctx.getOrCreateSymbol(labelName);
      const MCExpr *Expr = MCSymbolRefExpr::create(Sym, Ctx);
      Inst.addOperand(MCOperand::createExpr(Expr));
      return true;
    }

    auto it = relocMap.find(Address);
    if (it == relocMap.end())
      return false;

    const auto &reloc = it->second;
    if (reloc.symbolName.empty())
      return false;

    MCSymbol *Sym = Ctx.getOrCreateSymbol(reloc.symbolName);
    const MCExpr *BaseExpr = MCSymbolRefExpr::create(Sym, Ctx);

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

    // For section symbols: use base synth label (addend=0) as the
    // symbol ref, then wrap in MCBinaryExpr if addend != 0. This keeps
    // text references pointing to the base MCGlobal (contiguous data),
    // and mc2llvm resolves the addend via GEP.
    // For regular symbols with addend: use MCBinaryExpr directly.
    if (reloc.symbolName.starts_with(".")) {
      auto baseKey = make_pair(reloc.symbolName, (uint64_t)0);
      auto baseLabelIt = sectionOffsetLabels.find(baseKey);
      if (baseLabelIt != sectionOffsetLabels.end()) {
        MCSymbol *BaseSym = Ctx.getOrCreateSymbol(baseLabelIt->second);
        BaseExpr = MCSymbolRefExpr::create(BaseSym, Ctx);
      }
      if (reloc.addend != 0) {
        auto *AddendExpr = MCConstantExpr::create(reloc.addend, Ctx);
        BaseExpr = MCBinaryExpr::createAdd(BaseExpr, AddendExpr, Ctx);
      }
    } else if (reloc.addend != 0) {
      // Regular symbol + addend: MCBinaryExpr → mc2llvm handles via GEP
      auto *AddendExpr = MCConstantExpr::create(reloc.addend, Ctx);
      BaseExpr = MCBinaryExpr::createAdd(BaseExpr, AddendExpr, Ctx);
    }

    const MCExpr *Expr = MCSpecifierExpr::create(BaseExpr, spec, Ctx);
    Inst.addOperand(MCOperand::createExpr(Expr));
    return true;
  }

  void tryAddingPcLoadReferenceComment(raw_ostream &CStream,
                                        int64_t Value,
                                        uint64_t Address) override {}
};

// ============================================================================
// AArch64 branch target pre-scan
// ============================================================================

/// Scan AArch64 instruction bytes to find all addresses that are targets of
/// branch instructions (B, B.cond, CBZ, CBNZ, TBZ, TBNZ — NOT BL, which is a
/// call). These addresses need a .L_<addr> label in the assembly so the
/// MCAsmParser creates a basic block at each branch target.
///
/// Without this pre-scan, every instruction gets a .L_<addr> label, creating
/// one basic block per instruction — inflating the lifted IR and making
/// non-optimized output unreadable.
static std::set<uint64_t>
scanBranchTargetsAArch64(llvm::StringRef bytes, uint64_t sectionAddr,
                          uint64_t startAddr, uint64_t endAddr) {
  std::set<uint64_t> targets;
  for (uint64_t addr = startAddr; addr + 4 <= endAddr; addr += 4) {
    uint32_t inst = llvm::support::endian::read32le(
        bytes.bytes_begin() + (addr - sectionAddr));

    // B (unconditional, excluding BL): bits[31:26] = 000101
    // BL is 100101 → bit[31] distinguishes them under mask 0xFC000000.
    if ((inst & 0xFC000000) == 0x14000000) {
      int64_t offset = llvm::SignExtend64((inst & 0x03FFFFFF) << 2, 28);
      targets.insert(addr + offset);
    }
    // B.cond: bits[31:25] = 0101010, bit[4] = 0
    else if ((inst & 0xFF000010) == 0x54000000) {
      int64_t offset =
          llvm::SignExtend64(((inst >> 5) & 0x7FFFF) << 2, 21);
      targets.insert(addr + offset);
    }
    // CBZ/CBNZ (32- and 64-bit): bits[31:25] = 011010x
    else if ((inst & 0x7E000000) == 0x34000000) {
      int64_t offset =
          llvm::SignExtend64(((inst >> 5) & 0x7FFFF) << 2, 21);
      targets.insert(addr + offset);
    }
    // TBZ/TBNZ (32- and 64-bit): bits[31:25] = 011011x
    else if ((inst & 0x7E000000) == 0x36000000) {
      int64_t offset =
          llvm::SignExtend64(((inst >> 5) & 0x3FFF) << 2, 16);
      targets.insert(addr + offset);
    }
  }
  return targets;
}

// ============================================================================
// Obj2Asm implementation
// ============================================================================


void Obj2Asm::convertTextSection(
    SectionRef &sec, ObjectFile &obj,
    const map<uint64_t, lifter::SymbolInfo> &symMap,
    const map<uint64_t, RelocInfo> &textRelocMap,
    const SectionOffsetLabels &sectionOffsetLabels,
    const string &fnFilter) {
  auto secNameOrErr = sec.getName();
  string secName = secNameOrErr ? secNameOrErr->str() : "";
  if (!sec.isText() || secName != ".text")
    return;

  Triple TheTriple = obj.makeTriple();

  MCTargetOptions MCOptions;
  auto SymMRI = unique_ptr<MCRegisterInfo>(P->Targ->createMCRegInfo(TheTriple));
  if (!SymMRI) {
    *P->Out << "ERROR: Failed to create MCRegisterInfo\n";
    exit(-1);
  }

  auto SymMAI = unique_ptr<MCAsmInfo>(
      P->Targ->createMCAsmInfo(*SymMRI, TheTriple, MCOptions));
  if (!SymMAI) {
    *P->Out << "ERROR: Failed to create MCAsmInfo\n";
    exit(-1);
  }

  auto SymSTI = unique_ptr<MCSubtargetInfo>(
      P->Targ->createMCSubtargetInfo(TheTriple, DefaultCPU, DefaultFeatures));
  if (!SymSTI) {
    *P->Out << "ERROR: Failed to create MCSubtargetInfo\n";
    exit(-1);
  }

  auto SymMCII = unique_ptr<MCInstrInfo>(P->Targ->createMCInstrInfo());
  if (!SymMCII) {
    *P->Out << "ERROR: Failed to create MCInstrInfo\n";
    exit(-1);
  }

  auto SymCtx =
      make_unique<MCContext>(TheTriple, SymMAI.get(), SymMRI.get(), SymSTI.get());
  auto *SymMCOFI = P->Targ->createMCObjectFileInfo(*SymCtx, false);
  SymCtx->setObjectFileInfo(SymMCOFI);

  auto SymDisAsm = unique_ptr<MCDisassembler>(
      P->Targ->createMCDisassembler(*SymSTI, *SymCtx));
  if (!SymDisAsm) {
    *P->Out << "ERROR: Failed to create MCDisassembler\n";
    exit(-1);
  }

  auto RelInfo = P->Targ->createMCRelocationInfo(TheTriple, *SymCtx);
  auto Symbolizer = make_unique<ELFSymbolizer>(
      *SymCtx, unique_ptr<MCRelocationInfo>(RelInfo), symMap,
      textRelocMap, sectionOffsetLabels);
  SymDisAsm->setSymbolizer(std::move(Symbolizer));

  auto SymIP = unique_ptr<MCInstPrinter>(
      P->Targ->createMCInstPrinter(TheTriple, 0, *SymMAI, *SymMCII, *SymMRI));
  if (!SymIP) {
    *P->Out << "ERROR: Failed to create MCInstPrinter\n";
    exit(-1);
  }
  SymIP->setPrintImmHex(true);

  // Get section contents
  Expected<StringRef> contentsOrErr = sec.getContents();
  if (!contentsOrErr) {
    *P->Out << "ERROR: Failed to get section contents\n";
    exit(-1);
  }
  StringRef bytes = *contentsOrErr;
  uint64_t sectionAddr = sec.getAddress();
  uint64_t sectionSize = sec.getSize();

  // Build sorted list of function symbols in this section
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

  map<uint64_t, int> funcStartMap;
  int targetFuncIdx = -1;
  for (int i = 0; i < (int)funcSymbols.size(); ++i) {
    funcStartMap[funcSymbols[i].addr] = i;
    if (!fnFilter.empty() && funcSymbols[i].name == fnFilter)
      targetFuncIdx = i;
  }

  if (!fnFilter.empty() && targetFuncIdx < 0)
    return;

  uint64_t startAddr = sectionAddr;
  uint64_t endAddr = sectionAddr + sectionSize;
  if (!fnFilter.empty()) {
    const auto &fn = funcSymbols[targetFuncIdx];
    startAddr = fn.addr;
    if (fn.size > 0)
      endAddr = fn.addr + fn.size;
    else if (targetFuncIdx + 1 < (int)funcSymbols.size())
      endAddr = funcSymbols[targetFuncIdx + 1].addr;
  }

  // Pre-scan: identify addresses that need .L_<addr> labels.
  // Only branch targets (B/B.cond/CBZ/CBNZ/TBZ/TBNZ) need labels.
  // Function entry addresses already have their own label emitted below,
  // so skip them even if something branches to the function entry.
  std::set<uint64_t> branchTargets;
  if (bytes.size() >= 4) {
    branchTargets = scanBranchTargetsAArch64(bytes, sectionAddr,
                                             startAddr, endAddr);
    // Remove function entry addresses — they already have function labels
    // which the parser will turn into basic blocks.
    for (auto &[fnAddr, _] : funcStartMap)
      branchTargets.erase(fnAddr);
  }

  // Emit .file directive for DWARF line info
  // MCAsmStreamer's emitFileDirective emits ".file \"dir\" \"filename\"" form,
  // but we need the file-number form ".file 1 \".\" \"input.o\"" for .loc directives.
  *P->OS << "\t.file\t1 \".\" \"input.o\"\n";
  P->Streamer->switchSection(P->Ctx->getObjectFileInfo()->getTextSection());

  uint64_t addr = startAddr;
  uint64_t instCount = 0;
  int curFuncIdx = -1;
  bool inFunction = false;

  // If filtering, emit the target function header now
  if (!fnFilter.empty()) {
    curFuncIdx = targetFuncIdx;
    inFunction = true;
    const auto &fn = funcSymbols[targetFuncIdx];
    MCSymbol *FnSym = P->Ctx->getOrCreateSymbol(fn.name);
    P->Streamer->emitSymbolAttribute(FnSym, MCSA_Global);
    P->Streamer->emitValueToAlignment(Align(4));
    P->Streamer->emitSymbolAttribute(FnSym, MCSA_ELF_TypeFunction);
    P->Streamer->emitLabel(FnSym);
    P->Streamer->emitCFIStartProc(true);
  }

  while (addr < endAddr) {
    // Check function boundary (only when not filtering)
    if (fnFilter.empty()) {
      auto funcIt = funcStartMap.find(addr);
      if (funcIt != funcStartMap.end()) {
        // Close previous function
        if (inFunction) {
          P->Streamer->emitCFIEndProc();
          MCSymbol *CurFnSym =
              P->Ctx->getOrCreateSymbol(funcSymbols[curFuncIdx].name);
          MCSymbol *EndSym = P->Ctx->createTempSymbol();
          P->Streamer->emitLabel(EndSym);
          const MCExpr *SizeExpr = MCBinaryExpr::createSub(
              MCSymbolRefExpr::create(EndSym, *P->Ctx),
              MCSymbolRefExpr::create(CurFnSym, *P->Ctx), *P->Ctx);
          P->Streamer->emitELFSize(CurFnSym, SizeExpr);
          *P->OS << "\n"; // blank line between functions
        }

        curFuncIdx = funcIt->second;
        const auto &fn = funcSymbols[curFuncIdx];
        inFunction = true;

        MCSymbol *FnSym = P->Ctx->getOrCreateSymbol(fn.name);
        P->Streamer->emitSymbolAttribute(FnSym, MCSA_Global);
        P->Streamer->emitValueToAlignment(Align(4));
        P->Streamer->emitSymbolAttribute(FnSym, MCSA_ELF_TypeFunction);
        P->Streamer->emitLabel(FnSym);
        P->Streamer->emitCFIStartProc(true);
      }
    }

    // Disassemble one instruction
    MCInst Inst;
    uint64_t Size = 0;
    ArrayRef<uint8_t> Bytes(
        reinterpret_cast<const uint8_t *>(
            bytes.data() + (addr - sectionAddr)),
        sectionAddr + sectionSize - addr);

    bool DecodeOK =
        SymDisAsm->getInstruction(Inst, Size, Bytes, addr, nulls());
    if (!DecodeOK || Size == 0) {
      Size = 4;
      addr += Size;
      continue;
    }

    // Emit .loc directive (line number for lineMap)
    P->Streamer->emitDwarfLocDirective(1, instCount, 0, 0, 0, 0, StringRef(),
                                     StringRef());

    // Emit address label — only at actual branch targets.
    // Every instruction used to get .L_<addr>, which created one basic block
    // per instruction during parsing. The pre-scan above (branchTargets)
    // limits labels to addresses that are actual branch targets.
    if (branchTargets.count(addr)) {
      stringstream labelSS;
      labelSS << ".L_" << hex << addr;
      MCSymbol *AddrSym = P->Ctx->getOrCreateSymbol(labelSS.str());
      P->Streamer->emitLabel(AddrSym);
    }

    // Emit the instruction via MCInstPrinter + raw output
    // We can't use P->Streamer->emitInstruction() here because our Streamer
    // is an MCAsmStreamer that will format the instruction, but it needs
    // the instruction in a way that the assembler can parse it back.
    // The MCInstPrinter + IP->printInst() already handles MCSpecifierExpr
    // output correctly (e.g. :abs_g0:symbol), so we use it directly.
    string instStr;
    raw_string_ostream instOS(instStr);
    SymIP->printInst(&Inst, addr, "", *SymSTI, instOS);
    instOS.flush();

    // Trim leading/trailing whitespace
    while (!instStr.empty() &&
           (instStr.front() == ' ' || instStr.front() == '\t'))
      instStr.erase(instStr.begin());
    while (!instStr.empty() && (instStr.back() == ' ' || instStr.back() == '\t' ||
                                instStr.back() == '\n' || instStr.back() == '\r'))
      instStr.pop_back();
    if (!instStr.empty())
      *P->OS << "\t" << instStr << "\n";

    addr += Size;
    ++instCount;
  }

  // Close last function
  if (inFunction) {
    P->Streamer->emitCFIEndProc();
    MCSymbol *CurFnSym =
        P->Ctx->getOrCreateSymbol(funcSymbols[curFuncIdx].name);
    MCSymbol *EndSym = P->Ctx->createTempSymbol();
    P->Streamer->emitLabel(EndSym);
    const MCExpr *SizeExpr = MCBinaryExpr::createSub(
        MCSymbolRefExpr::create(EndSym, *P->Ctx),
        MCSymbolRefExpr::create(CurFnSym, *P->Ctx), *P->Ctx);
    P->Streamer->emitELFSize(CurFnSym, SizeExpr);
    *P->OS << "\n";
  }
}

void Obj2Asm::convertDataSections(ObjectFile &obj,
                                   const SectionOffsetLabels &sectionOffsetLabels) {
  for (SectionRef sec : obj.sections()) {
    auto nameOrErr = sec.getName();
    string secName = nameOrErr ? nameOrErr->str() : "";
    if (secName.empty() || secName == ".text" || sec.isBSS())
      continue;

    // Skip metadata sections
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
    map<uint64_t, string> relocMap;
    for (SectionRef relaSec : obj.sections()) {
      auto relaSecNameOrErr = relaSec.getName();
      string relaSecName =
          relaSecNameOrErr ? relaSecNameOrErr->str() : "";
      if (!relaSecName.starts_with(".rela"))
        continue;
      auto relocatedSecOrErr = relaSec.getRelocatedSection();
      if (!relocatedSecOrErr || *relocatedSecOrErr != sec)
        continue;
      for (const RelocationRef &reloc : relaSec.relocations()) {
        auto kind = lifter::classifyRelocation(reloc);
        if (kind == lifter::RelocKind::Other)
          continue;
        auto symIt = reloc.getSymbol();
        Expected<StringRef> nameOrErr3 = symIt->getName();
        if (!nameOrErr3)
          continue;
        relocMap[reloc.getOffset()] = nameOrErr3->str();
      }
      break;
    }

    // Build symbol map for this section (skip section symbols — their label
    // is implicitly defined by the .section directive and emitting them again
    // causes "symbol already defined" errors)
    // Also skip ARM mapping symbols ($a, $d, $t, $x) which mark instruction
    // vs data boundaries for the linker but don't represent real symbols.
    map<uint64_t, string> secSymMap;
    for (SymbolRef sym : obj.symbols()) {
      auto symSecOrErr = sym.getSection();
      if (!symSecOrErr || *symSecOrErr != sec)
        continue;
      Expected<StringRef> nameOrErr2 = sym.getName();
      Expected<uint64_t> addrOrErr = sym.getAddress();
      if (!nameOrErr2 || !addrOrErr)
        continue;
      string symName = nameOrErr2->str();
      // Skip ELF section symbols (name matches section name)
      if (symName == secName)
        continue;
      // Skip ARM mapping symbols
      if (symName == "$a" || symName == "$d" || symName == "$t" || symName == "$x")
        continue;
      uint64_t symOff = *addrOrErr - sec.getAddress();
      secSymMap[symOff] = symName;
    }

    // Emit section header using MCAsmStreamer
    // We need to get or create the MCSection for this ELF section
    string sectionFlags = "\"a\"";
    if (secName == ".data" || secName.find(".data.") == 0)
      sectionFlags = "\"aw\"";
    if (secName == ".rodata" || secName.find(".rodata.") == 0)
      sectionFlags = "\"a\"";

    // Switch to the appropriate section using MCContext
    // MCContext::getELFSection creates the section if it doesn't exist
    unsigned secFlags =
        (secName == ".data" || secName.find(".data.") == 0)
            ? (ELF::SHF_WRITE | ELF::SHF_ALLOC)
            : ELF::SHF_ALLOC;
    MCSectionELF *ELFSec = P->Ctx->getELFSection(
        secName, ELF::SHT_PROGBITS, secFlags);
    P->Streamer->switchSection(ELFSec);

    // Alignment
    Align secAlign = sec.getAlignment();
    if (secAlign > Align(1))
      P->Streamer->emitValueToAlignment(secAlign);

    // Build sorted synthetic label offsets for this section.
    // This includes both section+addend labels (key = section name) and
    // regular symbol+addend labels (key = symbol name).
    // Track which labels come from section symbols — these should not
    // split the data (text references use MCBinaryExpr base+addend instead).
    set<uint64_t> synthLabelOffsets;
    map<uint64_t, string> synthLabelMap;
    set<string> sectionLabels; // synth labels from section symbols
    for (auto &[key, label] : sectionOffsetLabels) {
      const string &symOrSecName = key.first;
      uint64_t addend = key.second;
      if (symOrSecName == secName) {
        synthLabelOffsets.insert(addend);
        synthLabelMap[addend] = label;
        sectionLabels.insert(label);
      } else if (!symOrSecName.starts_with(".")) {
        // Regular symbol: find its offset in this section
        for (auto &[off, name] : secSymMap) {
          if (name == symOrSecName) {
            uint64_t synthOff = off + addend;
            synthLabelOffsets.insert(synthOff);
            synthLabelMap[synthOff] = label;
            break;
          }
        }
      }
    }

    // Emit data with symbols and relocations
    uint64_t offset = 0;
    while (offset < data.size()) {
      // Emit symbol label
      auto symIt = secSymMap.find(offset);
      if (symIt != secSymMap.end()) {
        MCSymbol *Sym = P->Ctx->getOrCreateSymbol(symIt->second);
        P->Streamer->emitLabel(Sym);
      }

      // Emit synthetic label for symbol+addend / section+addend relocations.
      // Skip section-symbol labels at non-zero offsets — these split
      // contiguous data and break base+offset address arithmetic in the
      // lifted IR. Text section references use MCBinaryExpr (base+addend)
      // instead, resolved via GEP in mc2llvm::getExprVar.
      auto synthIt = synthLabelMap.find(offset);
      if (synthIt != synthLabelMap.end()) {
        bool isSectionLabel = sectionLabels.count(synthIt->second);
        if (!isSectionLabel || offset == 0) {
          MCSymbol *Sym = P->Ctx->getOrCreateSymbol(synthIt->second);
          P->Streamer->emitLabel(Sym);
        }
      }

      // Check for relocation at this offset
      auto relocIt = relocMap.find(offset);
      if (relocIt != relocMap.end()) {
        uint64_t relocSize = 8;
        string relocSymName = relocIt->second;
        int64_t relocAddend = 0;

        // Find relocation type and addend
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
            auto *ELFObj2 = dyn_cast<ELFObjectFileBase>(&obj);
            if (ELFObj2) {
              if (auto addendOrErr2 = ELFRelocationRef(reloc).getAddend())
                relocAddend = *addendOrErr2;
            }
            break;
          }
          break;
        }

        // Replace symbol with synthetic label if available
        // (covers both section symbols and regular symbols with addends)
        if (!relocSymName.empty()) {
          auto synthKey =
              make_pair(relocSymName, (uint64_t)relocAddend);
          auto synthIt2 = sectionOffsetLabels.find(synthKey);
          if (synthIt2 != sectionOffsetLabels.end()) {
            relocSymName = synthIt2->second;
          }
        }

        // Emit symbol reference using MCStreamer API
        MCSymbol *RelocSym = P->Ctx->getOrCreateSymbol(relocSymName);
        const MCExpr *Expr = MCSymbolRefExpr::create(RelocSym, *P->Ctx);
        P->Streamer->emitValueImpl(Expr, relocSize, SMLoc());
        offset += relocSize;
        continue;
      }

      // Find next break point
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

      // Try to detect null-terminated string for .asciz.
      // Note: we do NOT skip this for offsets with synthetic labels.
      // A synthetic label marks where a relocation points INTO a string,
      // which is fine — the label is already emitted above, and the string
      // data should still be emitted contiguously through the streamer.
      if (secName.find("rodata") != string::npos &&
          secSymMap.find(offset) == secSymMap.end()) {
        uint64_t strEnd = offset;
        bool isString = false;
        while (strEnd < data.size()) {
          char c = data[strEnd];
          if (c == 0) {
            isString = (strEnd > offset + 2);
            break;
          }
          if (c < 32 || c > 126) {
            isString = false;
            break;
          }
          ++strEnd;
        }
        if (isString) {
          auto nextSym = secSymMap.upper_bound(offset);
          if (nextSym != secSymMap.end() && nextSym->first <= strEnd)
            isString = false;
        }
        if (isString) {
          auto nextSynth = synthLabelOffsets.upper_bound(offset);
          if (nextSynth != synthLabelOffsets.end() && *nextSynth <= strEnd)
            isString = false;
        }
        if (isString) {
          auto nextReloc = relocMap.upper_bound(offset);
          if (nextReloc != relocMap.end() && nextReloc->first <= strEnd)
            isString = false;
        }
        if (isString) {
          // Emit through the streamer (not direct *P->OS write) to
          // maintain correct ordering: P->OS is wrapped by a
          // formatted_raw_ostream that may buffer content, and direct
          // writes bypass the buffer, interleaving labels incorrectly.
          P->Streamer->emitBytes(
              StringRef(data.data() + offset, strEnd - offset + 1));
          offset = strEnd + 1;
          continue;
        }
      }

      // Emit raw bytes
      unsigned count = 0;
      uint64_t limit = min(nextBreak, (uint64_t)data.size());
      SmallString<64> byteData;
      for (uint64_t i = 0; offset + i < limit && count < 8; ++i, ++count) {
        byteData.push_back(data[offset + i]);
      }
      // MCStreamer::emitBytes emits .byte directives
      P->Streamer->emitBytes(byteData);
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
      if (symSize > 0) {
        MCSymbol *Sym = P->Ctx->getOrCreateSymbol(nameOrErr2->str());
        const MCExpr *SizeExpr = MCConstantExpr::create(symSize, *P->Ctx);
        P->Streamer->emitELFSize(Sym, SizeExpr);
      }
    }

    *P->OS << "\n";
  }
}

void Obj2Asm::convertBSSDeclarations(ObjectFile &obj) {
  for (SectionRef sec : obj.sections()) {
    if (!sec.isBSS())
      continue;
    auto nameOrErr = sec.getName();
    string secName = nameOrErr ? nameOrErr->str() : "";
    if (secName.empty())
      continue;

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

      unsigned align = 3; // default 8-byte alignment (2^3)
      MCSymbol *Sym = P->Ctx->getOrCreateSymbol(symName);
      P->Streamer->emitCommonSymbol(Sym, symSize, Align(1u << align));
    }
  }
}

unique_ptr<MemoryBuffer>
Obj2Asm::convertFullAsm(ObjectFile &obj,
                         const map<uint64_t, lifter::SymbolInfo> &symMap,
                         const string &fnFilter) {
  // Initialize output and MC infrastructure
  P->ResultStr.clear();
  P->OS = make_unique<raw_string_ostream>(P->ResultStr);

  Triple TheTriple = obj.makeTriple();

  P->MRI.reset(P->Targ->createMCRegInfo(TheTriple));
  if (!P->MRI) {
    *P->Out << "ERROR: Failed to create MCRegisterInfo\n";
    exit(-1);
  }

  MCTargetOptions MCOptions;
  P->MAI.reset(P->Targ->createMCAsmInfo(*P->MRI, TheTriple, MCOptions));
  if (!P->MAI) {
    *P->Out << "ERROR: Failed to create MCAsmInfo\n";
    exit(-1);
  }

  P->STI.reset(P->Targ->createMCSubtargetInfo(TheTriple, DefaultCPU, DefaultFeatures));
  if (!P->STI) {
    *P->Out << "ERROR: Failed to create MCSubtargetInfo\n";
    exit(-1);
  }

  P->MCII.reset(P->Targ->createMCInstrInfo());
  if (!P->MCII) {
    *P->Out << "ERROR: Failed to create MCInstrInfo\n";
    exit(-1);
  }

  P->Ctx = make_unique<MCContext>(TheTriple, P->MAI.get(), P->MRI.get(), P->STI.get());
  P->MCOFI.reset(P->Targ->createMCObjectFileInfo(*P->Ctx, false));
  P->Ctx->setObjectFileInfo(P->MCOFI.get());

  // Create MCInstPrinter — required by MCAsmStreamer (asserts non-null).
  // Used for CFI register name formatting and emitInstruction().
  auto IP = unique_ptr<MCInstPrinter>(
      P->Targ->createMCInstPrinter(TheTriple, 0, *P->MAI, *P->MCII, *P->MRI));
  if (!IP) {
    *P->Out << "ERROR: Failed to create MCInstPrinter\n";
    exit(-1);
  }

  // Create MCAsmStreamer via factory function.
  // MCCodeEmitter and MCAsmBackend are not needed for text-only output.
  std::unique_ptr<MCCodeEmitter> nullCE;
  std::unique_ptr<MCAsmBackend> nullMAB;
  P->Streamer.reset(
      createAsmStreamer(*P->Ctx,
                        make_unique<formatted_raw_ostream>(*P->OS),
                        std::move(IP), std::move(nullCE),
                        std::move(nullMAB)));
  P->Streamer->initSections(true, *P->STI);

  // 0. Build relocation map and synthetic labels
  map<uint64_t, RelocInfo> textRelocMap;
  for (SectionRef sec : obj.sections()) {
    auto nameOrErr = sec.getName();
    string secName = nameOrErr ? nameOrErr->str() : "";
    if (sec.isText() && secName == ".text") {
      textRelocMap = buildTextRelocMap(sec, obj);
      break;
    }
  }

  // Check for executable sections other than .text
  vector<string> skippedExecSections;
  for (SectionRef sec : obj.sections()) {
    auto nameOrErr = sec.getName();
    string secName = nameOrErr ? nameOrErr->str() : "";
    if (sec.isText() && secName != ".text")
      skippedExecSections.push_back(secName);
  }
  if (!skippedExecSections.empty()) {
    *P->Out << "ERROR: Executable sections other than .text are not supported: ";
    for (unsigned i = 0; i < skippedExecSections.size(); ++i) {
      if (i > 0) *P->Out << ", ";
      *P->Out << skippedExecSections[i];
    }
    *P->Out << "\nThese sections contain code that will be missing from the "
         << "lifted IR.\nSee AGENTS.md Known Limitation #9 for details.\n";
    exit(-1);
  }

  auto sectionOffsetLabels = buildSectionOffsetLabels(textRelocMap);

  // Also build synthetic labels for data section relocations.
  // This covers both section symbols (e.g. .rodata+0x9e) and regular
  // symbols with non-zero addends (e.g. maze+5) that appear in data
  // section relocations.
  {
    unsigned nextIdx = sectionOffsetLabels.size();
    for (SectionRef sec : obj.sections()) {
      auto secNameOrErr = sec.getName();
      string secName = secNameOrErr ? secNameOrErr->str() : "";
      if (secName.empty() || secName == ".text" || sec.isBSS())
        continue;
      if (secName.starts_with(".") &&
          (secName.find("rela") != string::npos ||
           secName.find("debug") != string::npos ||
           secName.find("symtab") != string::npos ||
           secName.find("strtab") != string::npos ||
           secName.find("note") != string::npos ||
           secName.find("comment") != string::npos ||
           secName.find("eh_frame") != string::npos))
        continue;

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
          auto addendOrErr = ELFRelocationRef(reloc).getAddend();
          uint64_t addend = addendOrErr ? *addendOrErr : 0;
          // Create synthetic labels for section symbols and for
          // regular symbols with non-zero addends
          if (symName.starts_with(".") || addend != 0) {
            auto key = make_pair(symName, addend);
            if (sectionOffsetLabels.find(key) == sectionOffsetLabels.end()) {
              sectionOffsetLabels[key] = "__sec_" + to_string(nextIdx++);
            }
          }
        }
        break;
      }
    }
  }

  // 1. Convert .text section
  for (SectionRef sec : obj.sections()) {
    auto nameOrErr = sec.getName();
    string secName = nameOrErr ? nameOrErr->str() : "";
    if (sec.isText() && secName == ".text") {
      convertTextSection(sec, obj, symMap, textRelocMap,
                         sectionOffsetLabels, fnFilter);
    }
  }

  // 2. Convert data sections
  convertDataSections(obj, sectionOffsetLabels);

  // 3. Convert BSS declarations
  convertBSSDeclarations(obj);

  // Flush and return
  P->OS->flush();
  return MemoryBuffer::getMemBufferCopy(P->ResultStr);
}

} // namespace obj2asm
