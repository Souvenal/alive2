#include "lifter_util/binary_reader.h"

#include "llvm/BinaryFormat/ELF.h"
#include "llvm/Object/ELFObjectFile.h"
#include "llvm/Object/ObjectFile.h"

using namespace llvm;
using namespace llvm::object;

namespace lifter {

RelocKind classifyRelocation(const RelocationRef &reloc) {
  uint64_t type = reloc.getType();
  // AArch64 ELF relocation types
  if (type == ELF::R_AARCH64_ABS64)
    return RelocKind::Absolute64;
  if (type == ELF::R_AARCH64_ABS32)
    return RelocKind::Absolute32;
  return RelocKind::Other;
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

} // namespace lifter
