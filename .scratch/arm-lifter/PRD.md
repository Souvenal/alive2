# arm-lifter: AArch64 ELF to LLVM IR Lifter

Status: `ready-for-agent`

## Problem Statement

Cross-architecture binary translation is needed when software must run on a different CPU ISA than it was compiled for. The canonical example: a project built for ARM64 Linux needs to be ported to x86_64 Linux. Full source-level recompilation is often impractical — the original build system, toolchain version, or source code may be unavailable. Existing binary translation tools (QEMU user-mode, Box64, etc.) add runtime overhead and may miss edge cases.

arm-lifter solves this by lifting the compiled ARM64 binary back to LLVM IR, then letting LLVM's existing backend recompile it to x86_64. The result is a **static binary translation**: the lifted binary runs natively on x86_64 with no emulation layer, at full native speed.

This is a fork (branch `object-lifter`) of the Alive2 project, repurposing its ARM→LLVM IR lifting backend (`backend_tv/`) from translation-validation to cross-architecture binary translation.

## Solution

arm-lifter takes two inputs produced from the same C source:
- An **AArch64 ELF `.o` file** (the compiled binary)
- A **`.bc` bitcode file** (the LLVM IR from the same compilation, with `-emit-llvm`)

The output is a single `.ll` file containing all functions from the `.o` lifted into LLVM IR, along with all global-variable definitions from the `.bc`. This `.ll` can then be re-compiled by any LLVM backend (e.g. x86_64) to produce a native binary.

The pipeline:
1. Read the ELF `.o`, extract symbol table and section data
2. Disassemble `.text` using LLVM's MCDisassembler, emitting re-assemblable GAS assembly
3. For each function, parse the assembly through LLVM's MCAsmParser and lift each instruction to LLVM IR via backend_tv's ARM instruction translators
4. All functions land in a single shared `llvm::Module` — cross-function calls resolve directly
5. Global variables are imported from the `.bc` with full initializers
6. Apply safe cleanup passes (mem2reg, dce, simplifycfg, globaldce) — no inlining or IPO
7. Output a single `.ll` file

The lifter's direct contract is to produce IR that (a) passes `opt -verify` and (b) re-compiles without codegen errors. End-to-end behavioral equivalence is verified by an external validation pipeline that runs both the original ARM64 binary and the recompiled x86_64 binary in QEMU and compares their observable behavior.

## User Stories

1. As a developer with an ARM64 codebase, I want to lift a single `.o` file to LLVM IR, so that I can inspect the lifted IR for correctness.
2. As a developer, I want the lifted IR to be valid LLVM IR that passes `opt -verify`, so that it can be processed by downstream LLVM tools.
3. As a developer, I want the lifted IR to re-compile to x86_64 without codegen errors, so that I can produce a working binary for my target platform.
4. As a developer, I want the lifted IR to preserve the observable behavior of the original ARM64 binary (stdout, exit code, memory writes via known calls), so that the recompiled binary works correctly.
5. As a developer testing the lifter, I want a pytest-based end-to-end test runner, so that I can run the full test corpus with a single command.
6. As a developer, I want the test runner to classify tests as must-pass or known-broken (xfail), so that regressions are caught immediately without being blocked by open issues.
7. As a developer, I want the test runner to drive both the original ARM64 binary (reference) and the recompiled x86_64 binary (subject) via a Lima VM, so that I can compare their behavior without manual QEMU invocation.
8. As a developer working on a whole-project migration, I want to batch-lift all `.o` files from a `compile_commands.json`, so that I can reassemble a full project from lifted code.
9. As a developer, I want the lifter to handle structs, arrays, loops, conditionals, and integer arithmetic correctly, so that the majority of C code compiles without issues.
10. As a developer, I want the lifter to handle floating-point instructions correctly, so that floating-point-heavy code can be lifted.
11. As a developer writing C code that uses standard compiler builtins (division, stack protection), I want the lifter to produce correct type signatures for codegen-injected symbols like `__udivdi3` and `__stack_chk_fail`, so that the recompiled binary links and runs correctly.
12. As a developer, I want the lifter to handle indirect calls via function pointers (`blr xN`) safely, so that callback-driven code (e.g. Coremark) can be lifted.
13. As a developer, I want the lifter to handle variadic functions, so that `printf`-family and other variadic code works.
14. As a developer, I want the lifter to handle all executable sections (not just `.text`), so that `.init`/`.fini` and other section types are covered.
15. As a developer debugging the lifter, I want the CLI to support `--show-asm` to print the intermediate assembly, so that I can diagnose failures.
16. As a developer building the lifter, I want minimal build dependencies (no Z3, no Alive2 IR, no SMT solver), so that the build is fast and the binary is small.
17. As a developer maintaining the lifter, I want all modifications to `backend_tv/` recorded in `docs/changelog/`, so that the divergence from upstream is auditable.
18. As a developer, I want dead code (RISC-V, ASLP, Mach-O parser, DWARF signature extractor) removed from the tree, so that the codebase is easier to navigate.
19. As a developer, I want the data sections in the lifted IR to be optimized (`.long`/`.quad` instead of individual `.byte`), so that the output is more readable and compiles faster.
20. As a developer, I want the lifter to mark any lifted functions that cannot be handled with a clear error and `exit(-1)`, so that partially lifted modules are never produced.

## Implementation Decisions

### Architecture

The lifter follows a 4-stage pipeline:

```
.o ─→ Obj2Asm ──(GAS .s)──→ arm2llvm ──(MCInst)──→ mc2llvm ──(IR)──→ Shared Module
```

- **Obj2Asm** (`lifter_util/obj2asm.h/cpp`): Reads ELF sections directly, disassembles via `MCDisassembler`, emits GAS-format assembly text via `MCAsmStreamer`. Handles relocation resolution (synthetic labels for section+addend references). Future work: emit `.long`/`.quad` for bulk data instead of per-byte.
- **arm2llvm** (`backend_tv/arm2llvm.h` + `arm2llvm_*.cpp`): LLVM MCInst → LLVM IR instruction translators. One file per instruction category (arith, branch, load, store, float, vector, misc). The `arm2llvm` class wraps `mc2llvm` and adds ARM-specific register mapping, ABI handling, and calling convention logic.
- **mc2llvm** (`backend_tv/mc2llvm.h/cpp`): The MC→IR engine core. Maintains register state, branches, basic blocks, and call resolution. Has two execution paths: refinement path (upstream, `adjustSrc`) and shared-module path (this branch, via `ObjCtx` parameter).
- **ObjectLiftContext** (`lifter_util/object_lift_context.h/cpp`): Owns the single shared `llvm::Module`. Imports globals from src-bc upfront via `llvm::Linker::linkModules()` (function bodies stripped). Provides `lookupGlobal()` and `getOrCreateGlobalDecl()` for the lifter's symbol resolution.

### Module ownership (ADR-0001)

`backend_tv/` is "owned but pruned". Modification tiers:
- **Removable as dead code:** `aslp/`, `riscv2llvm*`, SMT/Alive2 refinement path (`adjustSrc`, `fixupOptimizedTgt`), `#ifdef BUILD_ASLP` blocks
- **Stable — change with care:** `arm2llvm*.cpp`, `mc2llvm.cpp`, `lifter.cpp` public API
- **Freely changeable:** new entry points in `lifter.h`, wrapper types, CMake

### Shared Module architecture (ADR-0002)

All functions in a `.o` lift into one `llvm::Module`. Cross-function direct calls resolve via `Module::getFunction()`. Globals are imported once from src-bc. First failure triggers `exit(-1)` — partial output is not meaningful.

### CLI interface

- Positional: `<input.o>`
- Required: `--src-bc=<file.bc>`
- Optional: `-o/--output-file=`, `--show-asm`, `--run-replace-ptrtoint` (default on)
- Uses LLVM's `cl::ParseCommandLineOptions` — standard LLVM CLI conventions

Default output: `<inputbasename>.lifted.ll` in current directory.

### Codegen-injected runtime symbols (issue 11)

A new deep module: `lifter_util/codegen_runtime_abi.{h,cpp}`. Single function:

```
FunctionType *lookupRuntimeFunctionType(StringRef Name, LLVMContext &Ctx)
```

Returns canonical LLVM `FunctionType` for known compiler-rt / runtime symbols (`__udivdi3`, `__divti3`, `__stack_chk_fail`, etc.), or `nullptr` for unknown names. Consulted by `ObjectLiftContext` before the fallback `getOrCreateGlobalDecl`. Table populated from compiler-rt ABI documentation; grows opportunistically as the validation pipeline surfaces missing entries.

### Lifter cleanup passes

Safe function-local cleanup pipeline in `lifter_util/lifter_cleanup.cpp`. Runs only: mem2reg, dce, simplifycfg, globaldce. Does NOT run: instcombine (deletes defined function calls), function-attrs (infers noreturn incorrectly), or any inlining/IPO passes.

### Error handling

`exit(-1)` on first unrecoverable error. Rationale: shared module model makes partial output meaningless; continuing past a failed lift wastes compile time for no usable output.

## Testing Decisions

### Test philosophy

Test external behavior (binary behaves like the original), not implementation details (which registers are used, order of basic blocks). Good tests catch regressions in compiled output, not in the lifting pipeline internals.

### End-to-end testing (primary)

Driven by **pytest** (issue 12). Each `.c` file under `tests/lift/cases/` is a test case. For each case:
1. Compile reference ARM64 binary natively in Lima VM
2. Cross-compile `.o` + `.bc`, run arm-lifter, recompile to x86_64
3. Run both binaries in Lima (reference natively, x86_64 via `qemu-x86_64`)
4. Assert: stdout match + exit code match

### Test discipline

- **must-pass**: binary behavior matches. Regressions break CI.
- **xfail** (strict=True): known broken, linked to open issue. Unexpected pass also fails — forces issue closure.
- **no checked-in golden output**: all expected values computed at test time from the reference binary (avoids drift).

### Lima integration

One ARM64 Ubuntu Lima VM (`lima-default`). ARM64 binaries run natively (`lima -- /path/to/binary`). x86_64 binaries run via `qemu-x86_64` user-mode (`lima -- qemu-x86_64 /path/to/binary`). No manual VM interaction required — pytest drives everything via `subprocess.run(["lima", "--", ...])`.

### Unit testing (secondary)

- **`codegen_runtime_abi`**: unit-testable in isolation (no Lima, no ELF file, no arm-lifter). Test: for each known symbol name, assert `lookupRuntimeFunctionType` returns the correct `FunctionType`.
- **`obj2asm`**: potentially unit-testable by feeding a known ELF `.o` and comparing emitted assembly against expected text. Lower priority than end-to-end tests.

### Prior art

`scripts/lift_compile_commands.py` demonstrates the per-command compilation pattern. `tests/lift/Makefile` demonstrates the single-case dev loop. The pytest runner generalizes these into automated batch testing.

## Out of Scope

- **Mach-O format** (issue 01). ELF only.
- **C++ exception handling and RTTI**. Focus is C; C++ is deferred to later investigation.
- **Thread-local storage** with complex models. Simple TLS (initial exec) may work; general model is deferred.
- **Alive2 translation validation / SMT refinement**. arm-lifter does not check that the lifted IR refines the source; behavioral equivalence is verified externally.
- **Dynamic linking / shared libraries** .o-to-.o lifting only; shared library support is future work.
- **Running the lifter on a full project in CI**. The `lift_compile_commands.py` workflow is for ad-hoc validation; project-scale CI is separate.
- **Debug info preservation** in lifted IR. Source locations from `.bc` are not propagated.
- **Performance optimization of lifted IR** beyond the safe cleanup pass set. Profile-guided optimization of lifted code is future work.
- **Simultaneous multi-architecture support**. arm-lifter is AArch64-only.
