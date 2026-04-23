#pragma once

#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "llvm/Object/ObjectFile.h"

namespace llvm {
class FunctionType;
class LLVMContext;
class Type;
} // namespace llvm

namespace lifter {

/// Function signature extracted from DWARF .debug_info
struct DWARFFunctionSignature {
  std::string name;                    // Function name
  llvm::FunctionType *funcType = nullptr; // Complete function type
};

/// Extract a function's signature from the ELF .debug_info section.
///
/// @param obj      Open ELF ObjectFile (must contain .debug_info)
/// @param fnName   Target function name (must match DW_AT_name)
/// @param Context  LLVMContext (for creating llvm::Type*)
/// @return Signature on success; nullopt if no debug info or unsupported type
std::optional<DWARFFunctionSignature>
extractFunctionSignatureFromDWARF(llvm::object::ObjectFile &obj,
                                  const std::string &fnName,
                                  llvm::LLVMContext &Context);

struct ExtractedFunction {
  std::string name;
  uint64_t address;
  uint64_t size;
  std::string assembly; // Disassembled text
};

// Relocation classification for ELF object files
enum class RelocKind { Absolute64, Absolute32, Other };
RelocKind classifyRelocation(const llvm::object::RelocationRef &reloc);

// Build a map: section offset -> symbol name for all relocations in a section
std::map<uint64_t, std::string>
getRelocationMap(const llvm::object::SectionRef &sec);

// Build a map: address -> (name, isFunction) for all symbols in an ObjectFile
struct SymbolInfo {
  std::string name;
  bool isFunction;
  uint64_t size;
};
std::map<uint64_t, SymbolInfo>
buildSymbolMap(llvm::object::ObjectFile &obj);

// Extract all ARM64 function symbols from a Mach-O binary
std::vector<ExtractedFunction>
extractFunctionsFromBinary(const std::string &binaryPath);

// Disassemble a specific address range to assembly text.
// objdumpPath: path to llvm-objdump binary (default: "llvm-objdump")
std::string disassembleFunction(const std::string &binaryPath, uint64_t address,
                                uint64_t size, const std::string &fnName,
                                const std::string &objdumpPath = "llvm-objdump");

// Disassemble all functions from a Mach-O binary into a single assembly text.
// Each function is wrapped with its own .globl, .type, .size directives.
// objdumpPath: path to llvm-objdump binary (default: "llvm-objdump")
std::string disassembleAllFunctions(const std::string &binaryPath,
                                    const std::string &objdumpPath = "llvm-objdump");

// Parse stub/GOT symbols from a Mach-O binary (address -> name map)
std::map<uint64_t, std::string>
parseStubSymbols(const std::string &binaryPath);

// Parse function symbols from a Mach-O binary (address -> name map)
std::map<uint64_t, std::string>
parseFunctionSymbols(const std::string &binaryPath);

// Replace raw addresses in assembly text with symbol names
std::string
replaceAddressWithSymbol(const std::string &asmPart,
                         const std::map<uint64_t, std::string> &stubMap,
                         const std::map<uint64_t, std::string> &funcMap);

} // namespace lifter
