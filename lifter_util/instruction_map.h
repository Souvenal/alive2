#pragma once

// Copyright (c) 2026-present Souvenal
// Distributed under the MIT license that can be found in the LICENSE file.

#include <cstdint>
#include <string>
#include <system_error>
#include <vector>

namespace llvm {
class StringRef;
class raw_ostream;
} // namespace llvm

namespace lifter {

inline constexpr unsigned InstructionMapVersion = 1;
inline constexpr char InstructionMapDebugFile[] = "arm_asm.s";

struct InstructionMapInstruction {
  uint64_t armInstId;
  uint64_t dwarfLine;
  std::string mcBlock;
  std::string opcode;
  uint64_t opcodeId;
  std::string asmText;
};

struct InstructionMapFunction {
  std::string name;
  std::vector<InstructionMapInstruction> instructions;
};

void writeInstructionMap(llvm::raw_ostream &out,
                         const std::vector<InstructionMapFunction> &functions);

std::error_code
writeInstructionMapFile(llvm::StringRef path,
                        const std::vector<InstructionMapFunction> &functions);

} // namespace lifter
