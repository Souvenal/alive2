#pragma once

// Obj2Asm: Convert ELF object files (.o) to re-assemblable GAS assembly (.s).
//
// This class reconstructs the information lost during compilation — symbol
// names, relocation specifiers, section attributes — from the ELF binary
// and emits assembly that MCAsmParser can parse back into the lifting
// pipeline.
//
// Uses MCAsmStreamer API instead of manual string concatenation for type
// safety and correct GAS syntax formatting.

#include "lifter_util/binary_reader.h"

#include "llvm/Support/MemoryBuffer.h"

#include <map>
#include <memory>
#include <string>
#include <utility>

namespace llvm {
class Target;
} // namespace llvm

namespace obj2asm {

/// Relocation info extracted from .rela.text
struct RelocInfo {
  uint32_t type;          ///< ELF relocation type
  std::string symbolName; ///< Target symbol name
  int64_t addend;         ///< Addend from .rela entry
};

/// Map: (symbol/section name, addend offset) → synthetic label name
/// Used to resolve symbol+addend and section+addend relocations.
/// The lifter's mapExprVar() can only handle simple symbol references,
/// not MCBinaryExpr like "maze+5", so we create synthetic labels (e.g. __sec_0)
/// and define them at the appropriate offsets in the data sections.
using SectionOffsetLabels = std::map<std::pair<std::string, uint64_t>, std::string>;

/// Build a relocation map from .rela.text: instruction offset → RelocInfo
std::map<uint64_t, RelocInfo>
buildTextRelocMap(llvm::object::SectionRef &textSec,
                  llvm::object::ObjectFile &obj);

/// Scan a relocation map and create synthetic labels for section+addend
/// references. When a relocation references a section symbol (name starts
/// with '.') with a non-zero addend, we create a synthetic label like
/// __sec_N so the lifter can find it as a regular global symbol.
SectionOffsetLabels
buildSectionOffsetLabels(const std::map<uint64_t, RelocInfo> &relocMap);

/// Obj2Asm: Convert ELF .o files to re-assemblable assembly text.
///
/// Uses MCAsmStreamer to emit properly formatted GAS assembly, matching
/// the output format of LLVM's addPassesToEmitFile() (CodeGenFileType::AssemblyFile).
class Obj2Asm {
public:
  /// Construct an Obj2Asm instance for the given target.
  /// @param Targ  Resolved LLVM target (e.g. TheAArch64Target)
  /// @param out   Diagnostic output stream (for errors)
  Obj2Asm(const llvm::Target *Targ, std::ostream *out);

  /// Destructor — defined in .cpp where complete types are available.
  ~Obj2Asm();

  // Non-copyable, movable
  Obj2Asm(const Obj2Asm &) = delete;
  Obj2Asm &operator=(const Obj2Asm &) = delete;
  Obj2Asm(Obj2Asm &&) = default;
  Obj2Asm &operator=(Obj2Asm &&) = default;

  /// Convert the full ELF object file to re-assemblable assembly.
  /// This is the top-level entry point, replacing generateFullAsm().
  /// @param obj       Open ELF ObjectFile
  /// @param symMap    Pre-built symbol map from binary_reader
  /// @param fnFilter  If non-empty, only emit this function in .text
  /// @return          MemoryBuffer containing the assembly text
  std::unique_ptr<llvm::MemoryBuffer>
  convertFullAsm(llvm::object::ObjectFile &obj,
                 const std::map<uint64_t, lifter::SymbolInfo> &symMap,
                 const std::string &fnFilter = "");

private:
  /// Emit the .text section as re-assemblable assembly.
  void convertTextSection(llvm::object::SectionRef &sec,
                           llvm::object::ObjectFile &obj,
                           const std::map<uint64_t, lifter::SymbolInfo> &symMap,
                           const std::map<uint64_t, RelocInfo> &textRelocMap,
                           const SectionOffsetLabels &sectionOffsetLabels,
                           const std::string &fnFilter);

  /// Emit data sections (.rodata, .data, etc.) as assembly.
  void convertDataSections(llvm::object::ObjectFile &obj,
                            const SectionOffsetLabels &sectionOffsetLabels);

  /// Emit BSS declarations (.zero) and __sec_N labels for section relocations.
  void convertBSSDeclarations(
      llvm::object::ObjectFile &obj,
      const SectionOffsetLabels &sectionOffsetLabels);

  // MC infrastructure — unique_ptrs to incomplete types, destructor in .cpp
  struct Impl;
  std::unique_ptr<Impl> P;
};

} // namespace obj2asm
