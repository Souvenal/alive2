#pragma once

// ObjectLiftContext: Manages shared LLVM Module state for whole-program lifting.
//
// In the per-function lifting model, each liftFunc() call creates an independent
// LLVMContext/Module, causing global variables to be truncated (only the bytes
// referenced by that function's assembly are emitted) and cross-function calls
// to appear as external declarations.
//
// ObjectLiftContext solves this by providing a shared Module that all functions
// lift into. Global variable definitions are imported from src-bc (complete with
// initializers and correct types) before lifting begins, so the lifter finds
// them via Module::getGlobalVariable() instead of creating truncated versions
// from MCglobals.

#include <string>

#include "llvm/ADT/StringRef.h"
#include "llvm/Object/ObjectFile.h"
#include <map>

namespace llvm {
class Module;
class GlobalVariable;
class Constant;
class Type;
class StringRef;
} // namespace llvm

class ObjectLiftContext {
  llvm::Module &SharedModule;

  // "__sec_5" → ".rodata.str1.1" (populated lazily by registerSectionLabel)
  std::map<std::string, std::string> sectionLabelMap;

  // (".rodata.str1.1", 0x2b) → @.str.4  (precomputed by buildGlobalOffsetMap)
  std::map<std::pair<std::string, uint64_t>, llvm::GlobalVariable*>
      offsetGlobalMap;

public:
  /// Construct an ObjectLiftContext that manages the given shared Module.
  /// The Module must outlive this ObjectLiftContext.
  explicit ObjectLiftContext(llvm::Module &SharedModule);

  /// Import global variable definitions from src-bc module into SharedModule.
  ///
  /// Uses llvm::Linker::linkModules() to import all globals (with initializers)
  /// and function declarations from the src-bc module. Function bodies are
  /// stripped (deleteBody) since we only need signatures for cross-function
  /// call resolution — the actual function bodies come from lifting.
  ///
  /// This should be called once before any liftFuncToModule() calls.
  void importSrcBcGlobals(llvm::Module &SrcModule);

  /// Look up a global variable by name in the shared Module.
  /// Returns nullptr if not found (caller should fall back to MCglobals or
  /// create a declaration).
  ///
  /// This is a thin wrapper around Module::getGlobalVariable(), which uses
  /// the Module's internal ValueSymbolTable for O(1) lookup.
  llvm::GlobalVariable *lookupGlobal(const std::string &Name);

  /// Get or create a global variable declaration in the shared Module.
  ///
  /// Wraps Module::getOrInsertGlobal(): if a global with the given name
  /// already exists, returns it (possibly as a bitcast ConstantExpr if
  /// types don't match). Otherwise creates an external declaration.
  llvm::Constant *getOrCreateGlobalDecl(const llvm::StringRef &Name,
                                         llvm::Type *Ty);

  /// Precompute (section, offset) → GlobalVariable* mapping from ELF section
  /// bytes and source BC global initializers. Called once after
  /// importSrcBcGlobals() and before any liftFuncToModule() calls.
  void buildGlobalOffsetMap(llvm::object::ObjectFile &ELF,
                            llvm::Module &SrcModule);

  /// Register a mapping from a synthetic global label (e.g. "__sec_5")
  /// to the ELF section it was created from (e.g. ".rodata.str1.1").
  void registerSectionLabel(const std::string &label,
                            const std::string &section);

  /// Look up a source BC GlobalVariable* that matches the given
  /// (section, offset) pair. Returns nullptr if no match found.
  llvm::GlobalVariable *
  lookupGlobalAtOffset(const std::string &section, uint64_t offset) const;

  /// Get the ELF section name for a given label. Returns empty string if
  /// the label was not registered.
  std::string getSectionLabel(const std::string &label) const;

  /// Get the shared Module reference.
  llvm::Module &getModule() { return SharedModule; }
};
