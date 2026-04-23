Alive2 — arm-lifter fork
=======================

This is a fork of the [arm-tv branch](https://github.com/regehr/alive2/tree/arm-tv) of Alive2, extended with **arm-lifter** — a standalone tool that lifts ARM64 (AArch64) machine code from ELF object files into LLVM IR, by reusing `backend_tv`'s existing lifter infrastructure.

### What arm-lifter does

```
C source ──→ ELF .o ──→ LLVM IR (.ll)
```

arm-lifter reads an AArch64 ELF `.o` file, disassembles its text section, symbolizes data/BSS sections, and calls into `backend_tv`'s `liftFunc()` to produce semantically equivalent LLVM IR. This enables downstream analysis, decompilation, or formal verification of compiled ARM binaries.

### Quick Start

#### Step 0: Build local LLVM

arm-lifter requires a local LLVM build with RTTI enabled. Tested with the `release/22.x` branch.

```bash
# Clone LLVM into the project directory (recommended)
cd /path/to/alive2
git clone --branch release/22.x --depth 1 https://github.com/llvm/llvm-project.git

# Configure & build
cd llvm-project
cmake -B build -S llvm -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_RTTI=ON \
  -DBUILD_SHARED_LIBS=ON \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_ENABLE_PROJECTS="llvm"
cmake --build build
```

> **Note**: If LLVM is cloned elsewhere, set `LOCAL_LLVM` before running `build.sh`:
> ```bash
> export LOCAL_LLVM=/path/to/your/llvm-project
> ```

#### Step 1: Build arm-lifter

```bash
./build.sh
```

#### Step 2: Lift a function from an ELF object

```bash
build/arm-lifter tests/lift/foo.o --src-bc=tests/lift/foo.bc --fn=main -o output/
```

### Testing & Recompilation

The `tests/lift/` directory contains a Makefile-driven workflow:

```bash
cd tests/lift

# Step 1: Cross-compile C source → ELF .o + bitcode .bc (via zig cc)
# Step 2: Lift all functions from .o → .ll
make lift

# Step 3: Recompile lifted .ll back into an executable
make recompile-arm64-linux    # → output/<name>_lifted_arm64_linux (AArch64)
make recompile-x64-linux      # → output/<name>_lifted_x64_linux   (x86_64)
```

The recompilation step compiles each `.lifted.ll` → `.lifted.o`, then links them into a runnable executable. This verifies that the lifted IR is syntactically valid and linkable. Cross-architecture recompilation (e.g. lifting ARM64 code but recompiling for x86_64) is possible because the lifted IR is target-independent LLVM IR.

To test a different source file, edit `SRC` in the Makefile:

```makefile
SRC=your_file.c
```

### arm-lifter CLI

```
arm-lifter <input.o> --src-bc=<input.bc> [--fn=<name>] [options]
```

| Option | Description |
|--------|-------------|
| `<input.o>` | Input AArch64 ELF `.o` file (positional, required) |
| `--src-bc=<file.bc>` | LLVM Bitcode with function signatures and extern declarations (required) |
| `--fn=<name>` | Function to lift; omit to lift all defined functions in src-bc (optional) |
| `-o / --output-dir=<dir>` | Output directory; files named `<basename>_<fn>.lifted.ll` (default: stdout) |
| `--optimize-tgt=<level>` | Optimization level for lifted code (default: `O3`) |
| `--run-replace-ptrtoint` | Replace `ptr→int→ptr` round trips with single GEP (default: true) |
| `--show-asm` | Print symbolized assembly to stdout for debugging (default: false) |

**About `--src-bc`**: The lifter needs precise function signatures (parameter types, return type) to produce correct LLVM IR. These are provided via a `.bc` file compiled from the same source alongside the `.o` file (e.g. `clang -emit-llvm -c foo.c -o foo.bc`). The bitcode also supplies extern global variable types, replacing the default `i8` heuristic.

**Output**: If `-o` is given, each lifted function is written to `<dir>/<input_basename>_<functionName>.lifted.ll`; otherwise the IR is printed to stdout.

### Key Files

| File | Role |
|------|------|
| `tools/arm-lifter.cpp` | Tool entry point |
| `backend_tv/binary_reader.h/cpp` | ELF reading & disassembly |
| `backend_tv/lifter.h/cpp` | `liftFunc()` — assembly → LLVM IR |
| `backend_tv/arm2llvm.h/cpp` | ARM instruction lifting |
| `backend_tv/mc2llvm.h/cpp` | MC → LLVM core engine |

### Known Limitations

- **Per-function isolation**: each `liftFunc()` call creates an independent `LLVMContext`/`Module`; cross-function calls (`bl`) appear as external declarations.
- **ELF format only**: Mach-O is not supported.
- **No variadic functions**: the lifter cannot handle `printf`-style variadic calls.
- **ADRP relocation**: basic ADRP+LDR pairs are handled; complex GOT patterns may need further work.
- **Coupled to Alive2 verification**: `arm-lifter` links against the full Alive2/Z3 dependency chain because `liftFunc()` internally performs SMT solving. Decoupling would require refactoring `liftFunc()` into separate lift and verify phases.

Full architectural details are in [AGENTS.md](AGENTS.md).

---

For upstream Alive2 documentation (alive-tv, Clang plugin, caching, etc.), see the [original Alive2 README](https://github.com/AliveToolkit/alive2/blob/master/README.md) and the [arm-tv branch](https://github.com/regehr/alive2/tree/arm-tv).
