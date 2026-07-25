#pragma once

#include "llvm/Support/Error.h"

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace llvm {
class StringRef;
class Target;
class raw_ostream;

namespace object {
class ObjectFile;
}
} // namespace llvm

namespace lifter {

inline constexpr unsigned MachineCfgVersion = 1;

struct MachineInstruction {
  std::string id;
  uint64_t address;
  uint64_t offset;
  uint64_t size;
  uint64_t opcodeId;
  std::string bytes;
  std::string opcode;
  std::string assembly;
  std::string mnemonic;
  std::string operands;
  std::string flowKind;
  bool isTerminator;
  bool isCall;
  std::optional<uint64_t> directTarget;
  std::optional<std::string> relocationSymbol;
};

struct MachineBlock {
  std::string id;
  std::string key;
  uint64_t startAddress;
  uint64_t startOffset;
  uint64_t endOffset;
  std::vector<unsigned> instructionIndices;
  std::optional<unsigned> terminatorIndex;
  bool isEntry;
  bool isReachable;
  bool containsCall;
  bool hasUnresolvedExit;
};

struct MachineEdge {
  std::string sourceBlock;
  std::string sourceBlockKey;
  std::optional<std::string> targetBlock;
  std::optional<std::string> targetBlockKey;
  std::string kind;
  std::string resolution;
  std::optional<uint64_t> targetAddress;
  std::optional<std::string> targetSymbol;
};

struct MachineFunctionCfg {
  std::string name;
  std::string section;
  uint64_t sectionIndex;
  uint64_t address;
  uint64_t size;
  std::vector<MachineInstruction> instructions;
  std::vector<MachineBlock> blocks;
  std::vector<MachineEdge> edges;
  std::vector<std::string> errors;
};

struct MachineCfgBundle {
  std::string objectFormat;
  std::string triple;
  std::string architecture;
  std::string cpu;
  std::vector<MachineFunctionCfg> functions;
};

llvm::Expected<MachineCfgBundle>
buildMachineCfg(llvm::object::ObjectFile &object, const llvm::Target &target,
                llvm::StringRef cpu, llvm::StringRef functionFilter);

void writeMachineCfg(llvm::raw_ostream &out, const MachineCfgBundle &bundle);

} // namespace lifter
