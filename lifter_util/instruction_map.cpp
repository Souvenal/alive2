// Copyright (c) 2026-present Souvenal
// Distributed under the MIT license that can be found in the LICENSE file.

#include "lifter_util/instruction_map.h"

#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;

namespace lifter {

void writeInstructionMap(raw_ostream &out,
                         const std::vector<InstructionMapFunction> &functions) {
  json::OStream json(out, 2);
  json.objectBegin();
  json.attribute("version", InstructionMapVersion);
  json.attribute("debug_file", InstructionMapDebugFile);
  json.attributeArray("functions", [&] {
    for (const auto &function : functions) {
      json.objectBegin();
      json.attribute("name", function.name);
      json.attributeArray("instructions", [&] {
        for (const auto &instruction : function.instructions) {
          json.objectBegin();
          json.attribute("arm_inst_id", instruction.armInstId);
          json.attribute("dwarf_line", instruction.dwarfLine);
          json.attribute("mc_block", instruction.mcBlock);
          json.attribute("opcode", instruction.opcode);
          json.attribute("opcode_id", instruction.opcodeId);
          json.attribute("asm", instruction.asmText);
          json.objectEnd();
        }
      });
      json.objectEnd();
    }
  });
  json.objectEnd();
  out << '\n';
}

std::error_code
writeInstructionMapFile(StringRef path,
                        const std::vector<InstructionMapFunction> &functions) {
  std::error_code error;
  raw_fd_ostream out(path, error, sys::fs::OF_Text);
  if (error)
    return error;

  writeInstructionMap(out, functions);
  out.flush();
  return out.error();
}

} // namespace lifter
