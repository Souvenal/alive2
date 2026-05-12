#include "lifter_util/binary_reader.h"

#include "llvm/BinaryFormat/Dwarf.h"
#include "llvm/BinaryFormat/ELF.h"
#include "llvm/DebugInfo/DWARF/DWARFContext.h"
#include "llvm/DebugInfo/DWARF/DWARFDie.h"
#include "llvm/IR/DerivedTypes.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Type.h"
#include "llvm/Object/ELFObjectFile.h"
#include "llvm/Object/MachO.h"
#include "llvm/Object/ObjectFile.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/raw_ostream.h"

#include <cstdlib>
#include <iostream>
#include <map>
#include <sstream>

using namespace llvm;
using namespace llvm::object;

namespace lifter {

static bool isArm64MachO(const ObjectFile &Obj) {
  if (isa<MachOObjectFile>(&Obj))
    return Obj.getArch() == Triple::aarch64;
  return false;
}

static std::string getArchName(const ObjectFile &Obj) {
  if (isArm64MachO(Obj))
    return "aarch64";
  if (Obj.getArch() == Triple::aarch64)
    return "aarch64";
  if (Obj.getArch() == Triple::riscv64)
    return "riscv64";
  return "";
}

std::map<uint64_t, std::string>
parseStubSymbols(const std::string &binaryPath) {
  std::map<uint64_t, std::string> stubMap;

  auto BufferOrErr = MemoryBuffer::getFile(binaryPath);
  if (!BufferOrErr)
    return stubMap;

  auto ObjOrErr = ObjectFile::createObjectFile(*BufferOrErr->get());
  if (!ObjOrErr)
    return stubMap;

  ObjectFile &Obj = **ObjOrErr;
  auto *MachO = dyn_cast<MachOObjectFile>(&Obj);
  if (!MachO)
    return stubMap;

  std::vector<std::string> symbols;
  for (const SymbolRef &Sym : Obj.symbols()) {
    Expected<StringRef> NameOrErr = Sym.getName();
    if (NameOrErr) {
      std::string Name = NameOrErr->str();
      if (!Name.empty() && Name[0] == '_')
        Name = Name.substr(1);
      symbols.push_back(Name);
    } else {
      symbols.push_back("");
    }
  }

  std::stringstream ss;
  ss << "otool -I " << binaryPath << " 2>/dev/null";
  FILE *fp = popen(ss.str().c_str(), "r");
  if (!fp)
    return stubMap;

  char buffer[512];
  bool inStubsOrGot = false;

  while (fgets(buffer, sizeof(buffer), fp) != nullptr) {
    std::string line(buffer);

    if (line.find("__stubs") != std::string::npos ||
        line.find("__got") != std::string::npos) {
      inStubsOrGot = true;
      continue;
    }

    if (inStubsOrGot && line.find("__") != std::string::npos &&
        line.find("__stubs") == std::string::npos &&
        line.find("__got") == std::string::npos) {
      inStubsOrGot = false;
      continue;
    }

    if (!inStubsOrGot)
      continue;

    unsigned long addr;
    int index;
    if (sscanf(line.c_str(), "0x%lx %d", &addr, &index) == 2 ||
        sscanf(line.c_str(), "%lx %d", &addr, &index) == 2) {
      if (index >= 0 && static_cast<size_t>(index) < symbols.size()) {
        stubMap[addr] = symbols[index];
      }
    }
  }
  pclose(fp);

  return stubMap;
}

std::map<uint64_t, std::string>
parseFunctionSymbols(const std::string &binaryPath) {
  std::map<uint64_t, std::string> funcMap;

  auto BufferOrErr = MemoryBuffer::getFile(binaryPath);
  if (!BufferOrErr)
    return funcMap;

  auto ObjOrErr = ObjectFile::createObjectFile(*BufferOrErr->get());
  if (!ObjOrErr)
    return funcMap;

  ObjectFile &Obj = **ObjOrErr;

  for (const SymbolRef &Sym : Obj.symbols()) {
    Expected<SymbolRef::Type> TypeOrErr = Sym.getType();
    if (!TypeOrErr || *TypeOrErr != SymbolRef::ST_Function)
      continue;

    Expected<StringRef> NameOrErr = Sym.getName();
    Expected<uint64_t> AddrOrErr = Sym.getAddress();
    if (!NameOrErr || !AddrOrErr)
      continue;

    uint64_t Addr = *AddrOrErr;
    if (Addr == 0)
      continue;

    std::string Name = NameOrErr->str();
    if (!Name.empty() && Name[0] == '_')
      Name = Name.substr(1);

    funcMap[Addr] = Name;
  }

  return funcMap;
}

std::string
replaceAddressWithSymbol(const std::string &asmPart,
                         const std::map<uint64_t, std::string> &stubMap,
                         const std::map<uint64_t, std::string> &funcMap) {
  size_t mnemonicEnd = asmPart.find('\t');
  if (mnemonicEnd == std::string::npos)
    return asmPart;

  std::string mnemonic = asmPart.substr(0, mnemonicEnd);
  std::string rest = asmPart.substr(mnemonicEnd + 1);

  if (mnemonic == "bl" || mnemonic == "b" || mnemonic == "b.eq" ||
      mnemonic == "b.ne" || mnemonic == "b.lt" || mnemonic == "b.gt" ||
      mnemonic == "b.le" || mnemonic == "b.ge" || mnemonic == "b.hi" ||
      mnemonic == "b.lo" || mnemonic == "cbz" || mnemonic == "cbnz" ||
      mnemonic == "tbz" || mnemonic == "tbnz") {

    std::string firstOp, secondOp;
    size_t commaPos = rest.find(',');

    if (mnemonic == "cbz" || mnemonic == "cbnz" || mnemonic == "tbz" ||
        mnemonic == "tbnz") {
      if (commaPos == std::string::npos)
        return asmPart;

      firstOp = rest.substr(0, commaPos);
      std::string afterComma = rest.substr(commaPos + 1);
      size_t start = afterComma.find_first_not_of(" \t");
      if (start != std::string::npos) {
        afterComma = afterComma.substr(start);
      }
      size_t spacePos = afterComma.find(' ');
      secondOp = (spacePos != std::string::npos)
                     ? afterComma.substr(0, spacePos)
                     : afterComma;
      if (!secondOp.empty() && secondOp.back() == '\n')
        secondOp.pop_back();
    } else {
      size_t spacePos = rest.find(' ');
      secondOp =
          (spacePos != std::string::npos) ? rest.substr(0, spacePos) : rest;
      if (!secondOp.empty() && secondOp.back() == '\n')
        secondOp.pop_back();
    }

    uint64_t addr = 0;
    if (secondOp.substr(0, 2) == "0x" || secondOp.substr(0, 2) == "0X") {
      addr = std::stoull(secondOp, nullptr, 16);
    }

    auto it = stubMap.find(addr);
    if (it != stubMap.end() && !it->second.empty()) {
      if (!firstOp.empty()) {
        return mnemonic + "\t" + firstOp + ", _" + it->second;
      }
      return mnemonic + "\t_" + it->second;
    }

    auto funcIt = funcMap.find(addr);
    if (funcIt != funcMap.end() && !funcIt->second.empty()) {
      if (!firstOp.empty()) {
        return mnemonic + "\t" + firstOp + ", _" + funcIt->second;
      }
      return mnemonic + "\t_" + funcIt->second;
    }

    if (addr != 0) {
      std::stringstream labelSS;
      labelSS << ".L_" << std::hex << addr;
      if (!firstOp.empty()) {
        return mnemonic + "\t" + firstOp + ", " + labelSS.str();
      }
      return mnemonic + "\t" + labelSS.str();
    }
  }

  if (mnemonic == "adrp") {
    size_t commaPos = rest.find(',');
    if (commaPos != std::string::npos) {
      std::string regPart = rest.substr(0, commaPos);
      std::string afterComma = rest.substr(commaPos + 1);
      size_t start = afterComma.find_first_not_of(" \t");
      if (start != std::string::npos) {
        afterComma = afterComma.substr(start);
      }
      size_t spacePos = afterComma.find(' ');
      std::string addrStr = (spacePos != std::string::npos)
                                ? afterComma.substr(0, spacePos)
                                : afterComma;
      if (!addrStr.empty() && addrStr.back() == '\n')
        addrStr.pop_back();

      uint64_t addr = 0;
      if (addrStr.substr(0, 2) == "0x" || addrStr.substr(0, 2) == "0X") {
        addr = std::stoull(addrStr, nullptr, 16);
      }

      if (addr != 0) {
        auto it = stubMap.find(addr);
        if (it != stubMap.end() && !it->second.empty()) {
          return mnemonic + "\t" + regPart + ", _" + it->second + "@GOTPAGE";
        }
        auto funcIt = funcMap.find(addr);
        if (funcIt != funcMap.end() && !funcIt->second.empty()) {
          return mnemonic + "\t" + regPart + ", _" + funcIt->second + "@PAGE";
        }
        std::stringstream labelSS;
        labelSS << ".L_" << std::hex << addr;
        return mnemonic + "\t" + regPart + ", " + labelSS.str();
      }
    }
  }

  return asmPart;
}

std::vector<ExtractedFunction>
extractFunctionsFromBinary(const std::string &binaryPath) {
  std::vector<ExtractedFunction> functions;

  auto BufferOrErr = MemoryBuffer::getFile(binaryPath);
  if (!BufferOrErr) {
    errs() << "Error: Cannot open binary: " << binaryPath << "\n";
    return functions;
  }

  auto ObjOrErr = ObjectFile::createObjectFile(*BufferOrErr->get());
  if (!ObjOrErr) {
    errs() << "Error: Cannot create ObjectFile: " << binaryPath << "\n";
    return functions;
  }

  ObjectFile &Obj = **ObjOrErr;
  std::string ArchName = getArchName(Obj);
  if (ArchName.empty()) {
    errs() << "Error: Unsupported binary architecture\n";
    return functions;
  }

  for (const SymbolRef &Sym : Obj.symbols()) {
    Expected<SymbolRef::Type> TypeOrErr = Sym.getType();
    if (!TypeOrErr)
      continue;

    if (*TypeOrErr != SymbolRef::ST_Function)
      continue;

    Expected<StringRef> NameOrErr = Sym.getName();
    if (!NameOrErr)
      continue;

    Expected<uint64_t> AddrOrErr = Sym.getAddress();
    if (!AddrOrErr)
      continue;

    uint64_t Addr = *AddrOrErr;
    if (Addr == 0)
      continue;

    StringRef Name = *NameOrErr;
    if (Name.starts_with("_"))
      Name = Name.drop_front(1);

    ExtractedFunction EF;
    EF.name = Name.str();
    EF.address = Addr;
    EF.size = 0;
    functions.push_back(EF);
  }

  std::sort(functions.begin(), functions.end(),
            [](const ExtractedFunction &a, const ExtractedFunction &b) {
              return a.address < b.address;
            });

  for (size_t i = 0; i < functions.size(); ++i) {
    if (i + 1 < functions.size()) {
      functions[i].size = functions[i + 1].address - functions[i].address;
    } else {
      functions[i].size = 4096;
    }
  }

  return functions;
}

std::string disassembleFunction(const std::string &binaryPath, uint64_t address,
                                uint64_t size, const std::string &fnName,
                                const std::string &objdumpPath) {
  auto stubMap = parseStubSymbols(binaryPath);
  auto funcMap = parseFunctionSymbols(binaryPath);

  std::string result;
  {
    std::stringstream ss;
    ss << objdumpPath << " -d --start-address=0x" << std::hex << address
       << " --stop-address=0x" << (address + size + 1024) << " " << binaryPath;

    std::string cmd = ss.str();
    FILE *fp = popen(cmd.c_str(), "r");
    if (!fp) {
      errs() << "Error: Failed to run objdump: " << objdumpPath << "\n";
      return "";
    }

    char buffer[512];
    bool inFunction = false;
    bool seenRet = false;
    while (fgets(buffer, sizeof(buffer), fp) != nullptr) {
      std::string line(buffer);

      if (line.find("Disassembly of section") != std::string::npos) {
        inFunction = false;
        continue;
      }
      if (line.find("file format") != std::string::npos)
        continue;

      if (line.find("<") != std::string::npos &&
          line.find(">") != std::string::npos) {
        if (line.find(">:") != std::string::npos) {
          if (seenRet && line.find("_main>") == std::string::npos) {
            break;
          }
          inFunction = true;
          continue;
        }
      }

      if (!inFunction)
        continue;

      if (line.find(":") != std::string::npos) {
        size_t colonPos = line.find(':');
        std::string addrPart = line.substr(0, colonPos);

        uint64_t curAddr = 0;
        try {
          if (addrPart.substr(0, 2) == "0x" || addrPart.substr(0, 2) == "0X") {
            curAddr = std::stoull(addrPart, nullptr, 16);
          } else {
            curAddr = std::stoull(addrPart, nullptr, 16);
          }
        } catch (...) {
          curAddr = 0;
        }

        std::string afterColon = line.substr(colonPos + 1);

        size_t tabPos = afterColon.find('\t');
        if (tabPos != std::string::npos) {
          if (curAddr != 0) {
            std::stringstream labelSS;
            labelSS << ".L_" << std::hex << curAddr;
            std::string label = labelSS.str();
            result += label + ":\n";
          }
          std::string asmPart = afterColon.substr(tabPos + 1);

          size_t commentPos = asmPart.find(';');
          if (commentPos != std::string::npos) {
            asmPart = asmPart.substr(0, commentPos);
          }

          asmPart = replaceAddressWithSymbol(asmPart, stubMap, funcMap);

          if (asmPart.find("ret") != std::string::npos) {
            if (!asmPart.empty() && asmPart != "\n" && asmPart != " ") {
              if (asmPart.back() == '\n' || asmPart.back() == '\r')
                result += "\t" + asmPart;
              else
                result += "\t" + asmPart + "\n";
            }
            seenRet = true;
            continue;
          }

          if (!asmPart.empty() && asmPart != "\n" && asmPart != " ") {
            if (asmPart.back() == '\n' || asmPart.back() == '\r')
              result += "\t" + asmPart;
            else
              result += "\t" + asmPart + "\n";
          }
        }
      }
    }
    pclose(fp);
  }

  if (result.empty()) {
    errs() << "Error: Failed to disassemble function\n";
    return "";
  }

  std::string assembly;
  std::string mangledName = "_" + fnName;
  assembly = "\t.text\n";
  assembly += "\t.globl\t" + mangledName + "\n";
  assembly += "\t.p2align\t2\n";
  assembly += "\t.type\t" + mangledName + ",@function\n";
  assembly += mangledName + ":\n";
  assembly += "\t.cfi_startproc\n";
  assembly += result;
  assembly += "\t.cfi_endproc\n";
  assembly += "\t.size\t" + mangledName + ", .-" + mangledName + "\n";

  return assembly;
}

std::string disassembleAllFunctions(const std::string &binaryPath,
                                    const std::string &objdumpPath) {
  auto funcs = extractFunctionsFromBinary(binaryPath);
  if (funcs.empty()) {
    errs() << "Error: No functions found in binary\n";
    return "";
  }

  std::string assembly = "\t.text\n";

  for (const auto &func : funcs) {
    std::string funcAsm =
        disassembleFunction(binaryPath, func.address, func.size, func.name,
                            objdumpPath);
    if (!funcAsm.empty()) {
      assembly += funcAsm;
      assembly += "\n";
    }
  }

  return assembly;
}

RelocKind classifyRelocation(const RelocationRef &reloc) {
  uint64_t type = reloc.getType();
  // AArch64 ELF relocation types
  if (type == ELF::R_AARCH64_ABS64)
    return RelocKind::Absolute64;
  if (type == ELF::R_AARCH64_ABS32)
    return RelocKind::Absolute32;
  return RelocKind::Other;
}

std::map<uint64_t, std::string>
getRelocationMap(const SectionRef &sec) {
  std::map<uint64_t, std::string> relocMap;
  for (const RelocationRef &reloc : sec.relocations()) {
    auto kind = classifyRelocation(reloc);
    if (kind == RelocKind::Other)
      continue;
    auto symIt = reloc.getSymbol();
    Expected<StringRef> nameOrErr = symIt->getName();
    if (!nameOrErr)
      continue;
    relocMap[reloc.getOffset()] = nameOrErr->str();
  }
  return relocMap;
}

std::map<uint64_t, SymbolInfo>
buildSymbolMap(ObjectFile &obj) {
  std::map<uint64_t, SymbolInfo> symMap;
  for (const SymbolRef &sym : obj.symbols()) {
    Expected<SymbolRef::Type> typeOrErr = sym.getType();
    if (!typeOrErr)
      continue;
    Expected<StringRef> nameOrErr = sym.getName();
    if (!nameOrErr)
      continue;
    Expected<uint64_t> addrOrErr = sym.getAddress();
    if (!addrOrErr)
      continue;
    uint64_t addr = *addrOrErr;
    std::string name = nameOrErr->str();
    bool isFunc = (*typeOrErr == SymbolRef::ST_Function);
    bool isCommon = false;
    auto flagsOrErr = sym.getFlags();
    if (flagsOrErr && (*flagsOrErr & SymbolRef::SF_Common))
      isCommon = true;
    // Skip zero-address symbols that aren't functions or common
    if (addr == 0 && !isFunc && !isCommon)
      continue;
    uint64_t size = ELFSymbolRef(sym).getSize();
    symMap[addr] = {name, isFunc, size};
  }
  return symMap;
}

namespace {
// Recursively convert a DWARF type DIE to llvm::Type*.
// Returns nullptr for unsupported types (struct, array, union, etc.).
llvm::Type *resolveDIType(const llvm::DWARFDie &Die, uint64_t PointerSize,
                          llvm::LLVMContext &Ctx) {
  if (!Die.isValid())
    return nullptr;

  switch (Die.getTag()) {

  case llvm::dwarf::DW_TAG_base_type: {
    auto Encoding = Die.find(llvm::dwarf::DW_AT_encoding);
    auto ByteSize = Die.find(llvm::dwarf::DW_AT_byte_size);
    if (!Encoding || !ByteSize)
      return nullptr;

    uint64_t bitSize = *ByteSize->getAsUnsignedConstant() * 8;
    switch (*Encoding->getAsUnsignedConstant()) {
    case llvm::dwarf::DW_ATE_signed:
    case llvm::dwarf::DW_ATE_unsigned:
    case llvm::dwarf::DW_ATE_signed_char:
    case llvm::dwarf::DW_ATE_unsigned_char:
      return llvm::Type::getIntNTy(Ctx, bitSize);
    case llvm::dwarf::DW_ATE_float:
      if (bitSize == 32)
        return llvm::Type::getFloatTy(Ctx);
      if (bitSize == 64)
        return llvm::Type::getDoubleTy(Ctx);
      return nullptr;
    case llvm::dwarf::DW_ATE_address:
      return llvm::PointerType::get(Ctx, 0);
    default:
      return nullptr;
    }
  }

  case llvm::dwarf::DW_TAG_pointer_type:
  case llvm::dwarf::DW_TAG_reference_type:
  case llvm::dwarf::DW_TAG_rvalue_reference_type: {
    // Use opaque pointer (LLVM 15+ style)
    return llvm::PointerType::get(Ctx, 0);
  }

  case llvm::dwarf::DW_TAG_const_type:
  case llvm::dwarf::DW_TAG_volatile_type:
  case llvm::dwarf::DW_TAG_restrict_type:
  case llvm::dwarf::DW_TAG_typedef: {
    // Skip cvr/typedef qualifiers, recurse into inner type
    if (auto Inner =
            Die.getAttributeValueAsReferencedDie(llvm::dwarf::DW_AT_type))
      return resolveDIType(Inner, PointerSize, Ctx);
    return nullptr;
  }

  case llvm::dwarf::DW_TAG_structure_type:
  case llvm::dwarf::DW_TAG_class_type:
  case llvm::dwarf::DW_TAG_union_type:
  case llvm::dwarf::DW_TAG_array_type:
    return nullptr; // lifter doesn't support struct/array params or returns

  default:
    // Try resolving through DW_AT_type reference for unhandled tags
    if (auto Inner =
            Die.getAttributeValueAsReferencedDie(llvm::dwarf::DW_AT_type))
      return resolveDIType(Inner, PointerSize, Ctx);
    return nullptr;
  }
}
} // namespace

std::optional<DWARFFunctionSignature>
extractFunctionSignatureFromDWARF(ObjectFile &obj,
                                          const std::string &fnName,
                                          LLVMContext &Context) {
  auto DICtx = DWARFContext::create(
      obj, DWARFContext::ProcessDebugRelocations::Process);

  for (const auto &CU : DICtx->compile_units()) {
    for (unsigned i = 0, e = CU->getNumDIEs(); i < e; ++i) {
      DWARFDie Die = CU->getDIEAtIndex(i);
      if (Die.getTag() != llvm::dwarf::DW_TAG_subprogram)
        continue;

      auto Name = Die.getName(DINameKind::ShortName);
      if (!Name || Name != fnName)
        continue;

      // Resolve return type
      llvm::Type *retTy = Type::getVoidTy(Context);
      if (auto RetTypeDie =
              Die.getAttributeValueAsReferencedDie(llvm::dwarf::DW_AT_type)) {
        auto *RT =
            resolveDIType(RetTypeDie, CU->getAddressByteSize(), Context);
        if (!RT)
          return std::nullopt; // unsupported return type
        retTy = RT;
      }

      // Resolve parameter types
      std::vector<llvm::Type *> paramTypes;
      for (DWARFDie Child : Die.children()) {
        if (Child.getTag() == llvm::dwarf::DW_TAG_formal_parameter) {
          if (auto ParamTypeDie =
                  Child.getAttributeValueAsReferencedDie(llvm::dwarf::DW_AT_type)) {
            if (auto *PT = resolveDIType(ParamTypeDie,
                                         CU->getAddressByteSize(), Context)) {
              paramTypes.push_back(PT);
            } else {
              return std::nullopt; // unsupported parameter type
            }
          }
        }
      }

      auto *fty = llvm::FunctionType::get(retTy, paramTypes, false);
      return DWARFFunctionSignature{fnName, fty};
    }
  }
  return std::nullopt; // function not found or no debug info
}

} // namespace lifter
