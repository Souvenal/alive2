# 2026-07-16 - instruction-map sidecar

## Why

Instruction correlation previously recovered ARM assembly by parsing
`arm-lifter` diagnostic logs. Logs are not a stable machine-readable contract,
so correlation data needed a structured source of truth.

## Changes

- Added `lifter_util/instruction_map.*`, a versioned JSON writer using LLVM's
  `llvm::json` support.
- `mc2llvm::run()` now captures one record for every lifted `MCInst`, including
  parser-inserted sentinel NOPs. Records contain the function-local ARM
  instruction ID, synthetic DWARF line, MC basic-block name, opcode name and
  numeric ID, and assembly emitted by the same `MCInstPrinter` path used for
  diagnostics.
- Extended `liftFuncToModule()` with an optional instruction-map output
  parameter and threaded it through `arm2llvm`. `tools/arm-lifter.cpp` owns
  sidecar creation through explicit `--asm-map=<path>` output.
- Preserved the existing DWARF carrier contract:
  `dwarf_line = arm_inst_id + 1`, with debug file `arm_asm.s`.

## Scope

`arm_inst_id` is function-local because each `mc2llvm::run()` invocation
starts at zero and `arm-lifter` invokes it once per lifted function. No object
offsets, relocations, or inferred LLVM-value fields are emitted.
