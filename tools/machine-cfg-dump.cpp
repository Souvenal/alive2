#include "lifter_util/machine_cfg.h"

#include "llvm/MC/TargetRegistry.h"
#include "llvm/Object/ELFObjectFile.h"
#include "llvm/Object/ObjectFile.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/Error.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/InitLLVM.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/raw_ostream.h"

#include <memory>
#include <string>
#include <system_error>

using namespace llvm;
using namespace llvm::object;

namespace {

cl::OptionCategory MachineCfgOptions("machine CFG options");

cl::opt<std::string> Input(cl::Positional, cl::desc("<input ELF object>"),
                           cl::Required, cl::cat(MachineCfgOptions));

cl::opt<std::string> Function("function", cl::desc("Only emit this function"),
                              cl::cat(MachineCfgOptions));

cl::opt<std::string> Cpu("mcpu",
                         cl::desc("CPU used for MC decoding (default=generic)"),
                         cl::init("generic"), cl::cat(MachineCfgOptions));

cl::opt<std::string> Output("output-file",
                            cl::desc("Output JSON path (default=stdout)"),
                            cl::cat(MachineCfgOptions));

cl::alias OutputAlias("o", cl::desc("Alias for --output-file"),
                      cl::aliasopt(Output), cl::cat(MachineCfgOptions));

} // namespace

int main(int argc, char **argv) {
  InitLLVM init(argc, argv);
  cl::HideUnrelatedOptions(MachineCfgOptions);
  cl::ParseCommandLineOptions(argc, argv);

  InitializeAllTargetInfos();
  InitializeAllTargetMCs();
  InitializeAllDisassemblers();

  ErrorOr<std::unique_ptr<MemoryBuffer>> buffer = MemoryBuffer::getFile(Input);
  if (!buffer) {
    errs() << "machine-cfg-dump: cannot open '" << Input
           << "': " << buffer.getError().message() << '\n';
    return 1;
  }

  Expected<std::unique_ptr<ObjectFile>> objectOrError =
      ObjectFile::createObjectFile(buffer.get()->getMemBufferRef());
  if (!objectOrError) {
    logAllUnhandledErrors(objectOrError.takeError(), errs(),
                          "machine-cfg-dump: ");
    return 1;
  }
  ObjectFile &object = **objectOrError;
  if (!isa<ELFObjectFileBase>(object)) {
    errs() << "machine-cfg-dump: input must be an ELF object\n";
    return 1;
  }

  Triple triple = object.makeTriple();
  std::string targetError;
  const Target *target = TargetRegistry::lookupTarget(triple, targetError);
  if (!target) {
    errs() << "machine-cfg-dump: " << targetError << '\n';
    return 1;
  }

  Expected<lifter::MachineCfgBundle> cfg =
      lifter::buildMachineCfg(object, *target, Cpu, Function);
  if (!cfg) {
    logAllUnhandledErrors(cfg.takeError(), errs(), "machine-cfg-dump: ");
    return 1;
  }

  if (Output.empty()) {
    lifter::writeMachineCfg(outs(), *cfg);
    return 0;
  }

  std::error_code error;
  raw_fd_ostream out(Output, error, sys::fs::OF_Text);
  if (error) {
    errs() << "machine-cfg-dump: cannot open output '" << Output
           << "': " << error.message() << '\n';
    return 1;
  }
  lifter::writeMachineCfg(out, *cfg);
  return out.has_error() ? 1 : 0;
}
