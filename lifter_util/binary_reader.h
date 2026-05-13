#pragma once

#include <map>
#include <string>

#include "llvm/Object/ObjectFile.h"

namespace lifter {

// Relocation classification for ELF object files
enum class RelocKind { Absolute64, Absolute32, Other };
RelocKind classifyRelocation(const llvm::object::RelocationRef &reloc);

// Build a map: address -> (name, isFunction) for all symbols in an ObjectFile
struct SymbolInfo {
  std::string name;
  bool isFunction;
  uint64_t size;
};
std::map<uint64_t, SymbolInfo>
buildSymbolMap(llvm::object::ObjectFile &obj);

} // namespace lifter
