#include "lifter_util/machine_cfg.h"

#include "llvm/ADT/ArrayRef.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/MC/MCAsmInfo.h"
#include "llvm/MC/MCContext.h"
#include "llvm/MC/MCDisassembler/MCDisassembler.h"
#include "llvm/MC/MCInst.h"
#include "llvm/MC/MCInstPrinter.h"
#include "llvm/MC/MCInstrAnalysis.h"
#include "llvm/MC/MCInstrInfo.h"
#include "llvm/MC/MCObjectFileInfo.h"
#include "llvm/MC/MCRegisterInfo.h"
#include "llvm/MC/MCSubtargetInfo.h"
#include "llvm/MC/MCTargetOptions.h"
#include "llvm/MC/TargetRegistry.h"
#include "llvm/Object/ELFObjectFile.h"
#include "llvm/Object/ObjectFile.h"
#include "llvm/Support/Error.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/TargetParser/Triple.h"

#include <algorithm>
#include <map>
#include <queue>
#include <set>
#include <system_error>
#include <utility>

using namespace llvm;
using namespace llvm::object;

namespace lifter {
namespace {

struct FunctionSymbol {
  std::string name;
  SectionRef section;
  uint64_t address;
  uint64_t size;
};

struct RelocationInfo {
  uint64_t offset;
  std::string symbol;
};

struct McEnvironment {
  Triple triple;
  std::unique_ptr<MCRegisterInfo> registerInfo;
  std::unique_ptr<MCAsmInfo> asmInfo;
  std::unique_ptr<MCSubtargetInfo> subtargetInfo;
  std::unique_ptr<MCInstrInfo> instrInfo;
  std::unique_ptr<MCContext> context;
  std::unique_ptr<MCObjectFileInfo> objectFileInfo;
  std::unique_ptr<MCDisassembler> disassembler;
  std::unique_ptr<MCInstrAnalysis> instrAnalysis;
  std::unique_ptr<MCInstPrinter> instPrinter;
};

std::string hexBytes(ArrayRef<uint8_t> bytes) {
  static constexpr char Digits[] = "0123456789abcdef";
  std::string result;
  result.reserve(bytes.size() * 2);
  for (uint8_t byte : bytes) {
    result.push_back(Digits[byte >> 4]);
    result.push_back(Digits[byte & 0xf]);
  }
  return result;
}

std::pair<std::string, std::string> splitAssembly(StringRef assembly) {
  assembly = assembly.trim();
  size_t split = assembly.find_first_of(" \t");
  if (split == StringRef::npos)
    return {assembly.str(), ""};
  return {assembly.take_front(split).str(),
          assembly.drop_front(split).trim().str()};
}

Expected<McEnvironment>
createMcEnvironment(ObjectFile &object, const Target &target, StringRef cpu) {
  McEnvironment env;
  env.triple = object.makeTriple();
  MCTargetOptions options;

  env.registerInfo.reset(target.createMCRegInfo(env.triple));
  if (!env.registerInfo)
    return createStringError(std::errc::not_supported,
                             "target has no MC register info");

  env.asmInfo.reset(
      target.createMCAsmInfo(*env.registerInfo, env.triple, options));
  if (!env.asmInfo)
    return createStringError(std::errc::not_supported,
                             "target has no MC assembly info");

  env.subtargetInfo.reset(target.createMCSubtargetInfo(env.triple, cpu, ""));
  if (!env.subtargetInfo)
    return createStringError(std::errc::not_supported,
                             "target has no MC subtarget info");

  env.instrInfo.reset(target.createMCInstrInfo());
  if (!env.instrInfo)
    return createStringError(std::errc::not_supported,
                             "target has no MC instruction info");

  env.context = std::make_unique<MCContext>(env.triple, env.asmInfo.get(),
                                            env.registerInfo.get(),
                                            env.subtargetInfo.get());
  env.objectFileInfo.reset(target.createMCObjectFileInfo(*env.context, false));
  env.context->setObjectFileInfo(env.objectFileInfo.get());

  env.disassembler.reset(
      target.createMCDisassembler(*env.subtargetInfo, *env.context));
  if (!env.disassembler)
    return createStringError(std::errc::not_supported,
                             "target has no MC disassembler");

  if (auto *elfObject = dyn_cast<ELFObjectFileBase>(&object))
    env.disassembler->setABIVersion(elfObject->getEIdentABIVersion());

  env.instrAnalysis.reset(target.createMCInstrAnalysis(env.instrInfo.get()));
  if (!env.instrAnalysis)
    return createStringError(std::errc::not_supported,
                             "target has no MC instruction analysis");

  unsigned dialect = env.asmInfo->getAssemblerDialect();
  env.instPrinter.reset(target.createMCInstPrinter(
      env.triple, dialect, *env.asmInfo, *env.instrInfo, *env.registerInfo));
  if (!env.instPrinter)
    return createStringError(std::errc::not_supported,
                             "target has no MC instruction printer");
  env.instPrinter->setPrintImmHex(true);
  env.instPrinter->setPrintBranchImmAsAddress(true);
  env.instPrinter->setMCInstrAnalysis(env.instrAnalysis.get());
  return env;
}

Expected<std::vector<FunctionSymbol>>
collectFunctionSymbols(ObjectFile &object, StringRef functionFilter) {
  std::vector<FunctionSymbol> functions;
  for (const SymbolRef &symbol : object.symbols()) {
    Expected<SymbolRef::Type> type = symbol.getType();
    if (!type) {
      consumeError(type.takeError());
      continue;
    }
    if (*type != SymbolRef::ST_Function)
      continue;

    Expected<StringRef> name = symbol.getName();
    Expected<uint64_t> address = symbol.getAddress();
    Expected<section_iterator> section = symbol.getSection();
    if (!name || !address || !section) {
      if (!name)
        consumeError(name.takeError());
      if (!address)
        consumeError(address.takeError());
      if (!section)
        consumeError(section.takeError());
      continue;
    }
    if (*section == object.section_end() || !(*section)->isText())
      continue;
    uint64_t size = 0;
    if (isa<ELFObjectFileBase>(object))
      size = ELFSymbolRef(symbol).getSize();
    functions.push_back({name->str(), **section, *address, size});
  }

  std::sort(functions.begin(), functions.end(),
            [](const FunctionSymbol &left, const FunctionSymbol &right) {
              if (left.section.getIndex() != right.section.getIndex())
                return left.section.getIndex() < right.section.getIndex();
              if (left.address != right.address)
                return left.address < right.address;
              return left.name < right.name;
            });

  for (size_t index = 0; index < functions.size(); ++index) {
    FunctionSymbol &function = functions[index];
    if (function.size)
      continue;
    uint64_t sectionEnd =
        function.section.getAddress() + function.section.getSize();
    uint64_t inferredEnd = sectionEnd;
    for (size_t next = index + 1; next < functions.size(); ++next) {
      if (functions[next].section.getIndex() != function.section.getIndex())
        break;
      if (functions[next].address > function.address) {
        inferredEnd = functions[next].address;
        break;
      }
    }
    if (inferredEnd > function.address)
      function.size = inferredEnd - function.address;
  }

  if (!functionFilter.empty()) {
    std::erase_if(functions, [&](const FunctionSymbol &function) {
      return function.name != functionFilter;
    });
    if (functions.empty())
      return createStringError(std::errc::invalid_argument,
                               "function '%s' not found",
                               functionFilter.str().c_str());
  }
  return functions;
}

std::map<uint64_t, std::vector<RelocationInfo>>
collectRelocations(ObjectFile &object) {
  std::map<uint64_t, std::vector<RelocationInfo>> result;
  for (SectionRef relocationSection : object.sections()) {
    Expected<section_iterator> relocated =
        relocationSection.getRelocatedSection();
    if (!relocated || *relocated == object.section_end()) {
      if (!relocated)
        consumeError(relocated.takeError());
      continue;
    }

    uint64_t sectionIndex = (**relocated).getIndex();
    for (const RelocationRef &relocation : relocationSection.relocations()) {
      std::string symbolName;
      symbol_iterator symbol = relocation.getSymbol();
      if (symbol != object.symbol_end()) {
        Expected<StringRef> name = symbol->getName();
        if (name)
          symbolName = name->str();
        else
          consumeError(name.takeError());
      }
      result[sectionIndex].push_back(
          {relocation.getOffset(), std::move(symbolName)});
    }
  }
  for (auto &[_, relocations] : result) {
    std::sort(relocations.begin(), relocations.end(),
              [](const RelocationInfo &left, const RelocationInfo &right) {
                return left.offset < right.offset;
              });
  }
  return result;
}

std::optional<std::string>
findRelocationSymbol(ArrayRef<RelocationInfo> relocations,
                     uint64_t instructionOffset, uint64_t size) {
  for (const RelocationInfo &relocation : relocations) {
    if (relocation.offset < instructionOffset)
      continue;
    if (relocation.offset >= instructionOffset + size)
      break;
    return relocation.symbol;
  }
  return std::nullopt;
}

std::string classifyFlow(const MCInst &instruction,
                         const MCInstrAnalysis &analysis,
                         const MCRegisterInfo &registerInfo) {
  if (analysis.isReturn(instruction))
    return "return";
  if (analysis.isCall(instruction))
    return "call";
  if (analysis.isIndirectBranch(instruction))
    return "indirect_branch";
  if (analysis.isConditionalBranch(instruction))
    return "conditional_branch";
  if (analysis.isUnconditionalBranch(instruction))
    return "unconditional_branch";
  if (analysis.mayAffectControlFlow(instruction, registerInfo))
    return "unknown_control_flow";
  return "none";
}

bool endsBlock(const MachineInstruction &instruction) {
  if (instruction.isCall)
    return false;
  return instruction.isTerminator ||
         instruction.flowKind == "conditional_branch" ||
         instruction.flowKind == "unconditional_branch" ||
         instruction.flowKind == "indirect_branch" ||
         instruction.flowKind == "return" ||
         instruction.flowKind == "unknown_control_flow";
}

Expected<MachineFunctionCfg>
decodeFunction(const FunctionSymbol &symbol, McEnvironment &env,
               ArrayRef<RelocationInfo> relocations) {
  MachineFunctionCfg function;
  function.name = symbol.name;
  Expected<StringRef> sectionName = symbol.section.getName();
  if (!sectionName)
    return sectionName.takeError();
  function.section = sectionName->str();
  function.sectionIndex = symbol.section.getIndex();
  function.address = symbol.address;
  function.size = symbol.size;

  Expected<StringRef> contents = symbol.section.getContents();
  if (!contents)
    return contents.takeError();
  uint64_t sectionAddress = symbol.section.getAddress();
  if (symbol.address < sectionAddress ||
      symbol.address + symbol.size > sectionAddress + contents->size())
    return createStringError(std::errc::invalid_argument,
                             "function '%s' lies outside section '%s'",
                             symbol.name.c_str(), function.section.c_str());

  ArrayRef<uint8_t> sectionBytes(
      reinterpret_cast<const uint8_t *>(contents->data()), contents->size());
  uint64_t address = symbol.address;
  uint64_t endAddress = symbol.address + symbol.size;
  env.instrAnalysis->resetState();

  while (address < endAddress) {
    uint64_t sectionOffset = address - sectionAddress;
    ArrayRef<uint8_t> remaining = sectionBytes.slice(sectionOffset);
    MCInst instruction;
    uint64_t size = 0;
    MCDisassembler::DecodeStatus status = env.disassembler->getInstruction(
        instruction, size, remaining, address, nulls());
    if (status == MCDisassembler::Fail || size == 0) {
      return createStringError(std::errc::illegal_byte_sequence,
                               "cannot decode instruction in '%s' at 0x%llx",
                               symbol.name.c_str(),
                               static_cast<unsigned long long>(address));
    }
    if (address + size > endAddress)
      return createStringError(
          std::errc::illegal_byte_sequence,
          "instruction in '%s' crosses function boundary at 0x%llx",
          symbol.name.c_str(), static_cast<unsigned long long>(address));

    std::string assembly;
    raw_string_ostream assemblyStream(assembly);
    env.instPrinter->printInst(&instruction, address, "", *env.subtargetInfo,
                               assemblyStream);
    assemblyStream.flush();
    auto [mnemonic, operands] = splitAssembly(assembly);

    MachineInstruction decoded{
        .id = symbol.name + "+0x" + utohexstr(address - symbol.address, true),
        .address = address,
        .offset = address - symbol.address,
        .size = size,
        .opcodeId = instruction.getOpcode(),
        .bytes = hexBytes(remaining.take_front(size)),
        .opcode = env.instrInfo->getName(instruction.getOpcode()).str(),
        .assembly = StringRef(assembly).trim().str(),
        .mnemonic = std::move(mnemonic),
        .operands = std::move(operands),
        .flowKind =
            classifyFlow(instruction, *env.instrAnalysis, *env.registerInfo),
        .isTerminator = env.instrAnalysis->isTerminator(instruction),
        .isCall = env.instrAnalysis->isCall(instruction),
        .directTarget = std::nullopt,
        .relocationSymbol =
            findRelocationSymbol(relocations, sectionOffset, size),
    };

    uint64_t target = 0;
    if ((env.instrAnalysis->isBranch(instruction) ||
         env.instrAnalysis->isCall(instruction)) &&
        env.instrAnalysis->evaluateBranch(instruction, address, size, target))
      decoded.directTarget = target;

    function.instructions.push_back(std::move(decoded));
    env.instrAnalysis->updateState(instruction, address);
    address += size;
  }

  if (function.instructions.empty())
    return function;

  std::map<uint64_t, unsigned> instructionAt;
  for (unsigned index = 0; index < function.instructions.size(); ++index)
    instructionAt[function.instructions[index].address] = index;

  std::set<uint64_t> blockStarts{function.instructions.front().address};
  for (unsigned index = 0; index < function.instructions.size(); ++index) {
    const MachineInstruction &instruction = function.instructions[index];
    if (instruction.directTarget && !instruction.relocationSymbol &&
        *instruction.directTarget >= function.address &&
        *instruction.directTarget < function.address + function.size)
      blockStarts.insert(*instruction.directTarget);
    if (endsBlock(instruction) && index + 1 < function.instructions.size())
      blockStarts.insert(function.instructions[index + 1].address);
  }

  std::map<uint64_t, unsigned> blockAt;
  for (unsigned instructionIndex = 0;
       instructionIndex < function.instructions.size();) {
    uint64_t startAddress = function.instructions[instructionIndex].address;
    MachineBlock block{
        .id = "L" + utohexstr(startAddress, true),
        .key = symbol.name + "+0x" +
               utohexstr(startAddress - function.address, true),
        .startAddress = startAddress,
        .startOffset = startAddress - function.address,
        .endOffset = 0,
        .instructionIndices = {},
        .terminatorIndex = std::nullopt,
        .isEntry = function.blocks.empty(),
        .isReachable = false,
        .containsCall = false,
        .hasUnresolvedExit = false,
    };
    blockAt[startAddress] = function.blocks.size();

    while (instructionIndex < function.instructions.size()) {
      if (!block.instructionIndices.empty() &&
          blockStarts.contains(function.instructions[instructionIndex].address))
        break;
      const MachineInstruction &instruction =
          function.instructions[instructionIndex];
      block.instructionIndices.push_back(instructionIndex);
      block.containsCall |= instruction.isCall;
      if (endsBlock(instruction)) {
        block.terminatorIndex = instructionIndex;
        ++instructionIndex;
        break;
      }
      ++instructionIndex;
    }
    const MachineInstruction &last =
        function.instructions[block.instructionIndices.back()];
    block.endOffset = last.offset + last.size;
    function.blocks.push_back(std::move(block));
  }

  auto addEdge = [&](unsigned sourceIndex, std::optional<unsigned> targetIndex,
                     StringRef kind, StringRef resolution,
                     std::optional<uint64_t> targetAddress,
                     std::optional<std::string> targetSymbol) {
    function.edges.push_back({
        .sourceBlock = function.blocks[sourceIndex].id,
        .sourceBlockKey = function.blocks[sourceIndex].key,
        .targetBlock = targetIndex ? std::optional<std::string>(
                                         function.blocks[*targetIndex].id)
                                   : std::nullopt,
        .targetBlockKey = targetIndex ? std::optional<std::string>(
                                            function.blocks[*targetIndex].key)
                                      : std::nullopt,
        .kind = kind.str(),
        .resolution = resolution.str(),
        .targetAddress = targetAddress,
        .targetSymbol = std::move(targetSymbol),
    });
  };

  auto resolveTarget = [&](unsigned sourceIndex,
                           const MachineInstruction &instruction,
                           StringRef edgeKind) {
    if (instruction.relocationSymbol) {
      addEdge(sourceIndex, std::nullopt, edgeKind, "external",
              instruction.directTarget, instruction.relocationSymbol);
      return;
    }
    if (!instruction.directTarget) {
      function.blocks[sourceIndex].hasUnresolvedExit = true;
      addEdge(sourceIndex, std::nullopt, edgeKind, "unresolved", std::nullopt,
              std::nullopt);
      return;
    }
    if (*instruction.directTarget < function.address ||
        *instruction.directTarget >= function.address + function.size) {
      addEdge(sourceIndex, std::nullopt, edgeKind, "external",
              instruction.directTarget, std::nullopt);
      return;
    }
    auto instructionTarget = instructionAt.find(*instruction.directTarget);
    auto blockTarget = blockAt.find(*instruction.directTarget);
    if (instructionTarget == instructionAt.end() ||
        blockTarget == blockAt.end()) {
      function.blocks[sourceIndex].hasUnresolvedExit = true;
      addEdge(sourceIndex, std::nullopt, edgeKind, "misaligned",
              instruction.directTarget, std::nullopt);
      return;
    }
    addEdge(sourceIndex, blockTarget->second, edgeKind, "local",
            instruction.directTarget, std::nullopt);
  };

  for (unsigned blockIndex = 0; blockIndex < function.blocks.size();
       ++blockIndex) {
    MachineBlock &block = function.blocks[blockIndex];
    const MachineInstruction &last =
        function.instructions[block.instructionIndices.back()];
    std::optional<unsigned> nextBlock =
        blockIndex + 1 < function.blocks.size()
            ? std::optional<unsigned>(blockIndex + 1)
            : std::nullopt;

    if (last.flowKind == "conditional_branch") {
      resolveTarget(blockIndex, last, "branch_taken");
      if (nextBlock)
        addEdge(blockIndex, nextBlock, "branch_not_taken", "local",
                function.blocks[*nextBlock].startAddress, std::nullopt);
    } else if (last.flowKind == "unconditional_branch") {
      resolveTarget(blockIndex, last, "jump");
    } else if (last.flowKind == "indirect_branch") {
      block.hasUnresolvedExit = true;
      addEdge(blockIndex, std::nullopt, "indirect", "unresolved", std::nullopt,
              std::nullopt);
    } else if (last.flowKind == "return") {
      addEdge(blockIndex, std::nullopt, "return", "terminal", std::nullopt,
              std::nullopt);
    } else if (last.flowKind == "unknown_control_flow" || last.isTerminator) {
      block.hasUnresolvedExit = true;
      addEdge(blockIndex, std::nullopt, "unknown", "unresolved", std::nullopt,
              std::nullopt);
    } else if (nextBlock) {
      addEdge(blockIndex, nextBlock, "fallthrough", "local",
              function.blocks[*nextBlock].startAddress, std::nullopt);
    }
  }

  std::queue<unsigned> pending;
  function.blocks.front().isReachable = true;
  pending.push(0);
  while (!pending.empty()) {
    unsigned source = pending.front();
    pending.pop();
    for (const MachineEdge &edge : function.edges) {
      if (edge.sourceBlock != function.blocks[source].id || !edge.targetBlock)
        continue;
      auto target = std::find_if(function.blocks.begin(), function.blocks.end(),
                                 [&](const MachineBlock &block) {
                                   return block.id == *edge.targetBlock;
                                 });
      if (target == function.blocks.end() || target->isReachable)
        continue;
      target->isReachable = true;
      pending.push(target - function.blocks.begin());
    }
  }

  return function;
}

void writeInstruction(json::OStream &json,
                      const MachineInstruction &instruction) {
  json.objectBegin();
  json.attribute("id", instruction.id);
  json.attribute("address", instruction.address);
  json.attribute("offset", instruction.offset);
  json.attribute("size", instruction.size);
  json.attribute("bytes", instruction.bytes);
  json.attribute("opcode", instruction.opcode);
  json.attribute("opcode_id", instruction.opcodeId);
  json.attribute("assembly", instruction.assembly);
  json.attribute("mnemonic", instruction.mnemonic);
  json.attribute("operands", instruction.operands);
  json.attribute("flow_kind", instruction.flowKind);
  json.attribute("is_terminator", instruction.isTerminator);
  json.attribute("is_call", instruction.isCall);
  if (instruction.directTarget)
    json.attribute("direct_target", *instruction.directTarget);
  else
    json.attribute("direct_target", nullptr);
  if (instruction.relocationSymbol)
    json.attribute("relocation_symbol", *instruction.relocationSymbol);
  else
    json.attribute("relocation_symbol", nullptr);
  json.objectEnd();
}

void writeBlock(json::OStream &json, const MachineBlock &block) {
  json.objectBegin();
  json.attribute("id", block.id);
  json.attribute("key", block.key);
  json.attribute("start_address", block.startAddress);
  json.attribute("start_offset", block.startOffset);
  json.attribute("end_offset", block.endOffset);
  json.attributeArray("instruction_indices", [&] {
    for (unsigned index : block.instructionIndices)
      json.value(index);
  });
  if (block.terminatorIndex)
    json.attribute("terminator_index", *block.terminatorIndex);
  else
    json.attribute("terminator_index", nullptr);
  json.attribute("is_entry", block.isEntry);
  json.attribute("is_reachable", block.isReachable);
  json.attribute("contains_call", block.containsCall);
  json.attribute("has_unresolved_exit", block.hasUnresolvedExit);
  json.objectEnd();
}

void writeEdge(json::OStream &json, const MachineEdge &edge) {
  json.objectBegin();
  json.attribute("source", edge.sourceBlock);
  json.attribute("source_key", edge.sourceBlockKey);
  if (edge.targetBlock)
    json.attribute("target", *edge.targetBlock);
  else
    json.attribute("target", nullptr);
  if (edge.targetBlockKey)
    json.attribute("target_key", *edge.targetBlockKey);
  else
    json.attribute("target_key", nullptr);
  json.attribute("kind", edge.kind);
  json.attribute("resolution", edge.resolution);
  if (edge.targetAddress)
    json.attribute("target_address", *edge.targetAddress);
  else
    json.attribute("target_address", nullptr);
  if (edge.targetSymbol)
    json.attribute("target_symbol", *edge.targetSymbol);
  else
    json.attribute("target_symbol", nullptr);
  json.objectEnd();
}

} // namespace

Expected<MachineCfgBundle> buildMachineCfg(ObjectFile &object,
                                           const Target &target, StringRef cpu,
                                           StringRef functionFilter) {
  Expected<McEnvironment> environment =
      createMcEnvironment(object, target, cpu);
  if (!environment)
    return environment.takeError();

  Expected<std::vector<FunctionSymbol>> functionSymbols =
      collectFunctionSymbols(object, functionFilter);
  if (!functionSymbols)
    return functionSymbols.takeError();

  auto relocations = collectRelocations(object);
  MachineCfgBundle bundle{
      .objectFormat = object.getFileFormatName().str(),
      .triple = object.makeTriple().getTriple(),
      .architecture = object.makeTriple().getArchName().str(),
      .cpu = cpu.str(),
      .functions = {},
  };

  for (const FunctionSymbol &symbol : *functionSymbols) {
    ArrayRef<RelocationInfo> functionRelocations;
    auto relocationIt = relocations.find(symbol.section.getIndex());
    if (relocationIt != relocations.end())
      functionRelocations = relocationIt->second;
    Expected<MachineFunctionCfg> function =
        decodeFunction(symbol, *environment, functionRelocations);
    if (!function)
      return function.takeError();
    bundle.functions.push_back(std::move(*function));
  }
  return bundle;
}

void writeMachineCfg(raw_ostream &out, const MachineCfgBundle &bundle) {
  json::OStream json(out, 2);
  json.objectBegin();
  json.attribute("version", MachineCfgVersion);
  json.attributeObject("binary", [&] {
    json.attribute("object_format", bundle.objectFormat);
    json.attribute("triple", bundle.triple);
    json.attribute("architecture", bundle.architecture);
    json.attribute("cpu", bundle.cpu);
  });
  json.attributeArray("functions", [&] {
    for (const MachineFunctionCfg &function : bundle.functions) {
      json.objectBegin();
      json.attribute("name", function.name);
      json.attribute("section", function.section);
      json.attribute("section_index", function.sectionIndex);
      json.attribute("address", function.address);
      json.attribute("size", function.size);
      json.attributeArray("instructions", [&] {
        for (const MachineInstruction &instruction : function.instructions)
          writeInstruction(json, instruction);
      });
      json.attributeArray("blocks", [&] {
        for (const MachineBlock &block : function.blocks)
          writeBlock(json, block);
      });
      json.attributeArray("edges", [&] {
        for (const MachineEdge &edge : function.edges)
          writeEdge(json, edge);
      });
      json.attributeArray("errors", [&] {
        for (const std::string &error : function.errors)
          json.value(error);
      });
      json.objectEnd();
    }
  });
  json.objectEnd();
  out << '\n';
}

} // namespace lifter
