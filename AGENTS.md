# AGENTS.md — Global Project Architecture & Standards

## Project Overview

**Alive2** is a formal verification tool for LLVM compiler transformations, built on SMT (Z3) solving. This is the **arm-tv fork** which extends Alive2 with ARM (AArch64) and RISC-V backend translation validation — lifting machine code back to LLVM IR for formal equivalence checking.

**Core mission of this fork**: Build `arm-lifter` — a standalone tool that lifts ARM64 assembly (from ELF object files) to LLVM IR, by borrowing and calling into `backend_tv`'s existing lifter infrastructure.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    Tools Layer                        │
│  alive  alive-tv  alive-exec  backend-tv  arm-lifter │
└──────────┬──────────┬──────────┬──────────────────────┘
           │          │          │
┌──────────▼──┐ ┌─────▼──────┐ ┌▼─────────────────────┐
│  llvm_util  │ │ backend_tv │ │       ir / smt        │
│  LLVM→Alive │ │ Lifter +   │ │  Alive2 core IR &     │
│  converter  │ │ ASLP bridge│ │  SMT abstraction      │
└─────────────┘ └────────────┘ └───────────────────────┘
                    │
              ┌─────▼──────┐
              │   aslp/    │
              │ ASL→LLVM   │
              │ semantics  │
              └────────────┘
```

### File Hierarchy

```
alive2/
├── tools/                # CLI entry points
│   ├── arm-lifter.cpp    # ★ ARM lifter tool (my code)
│   ├── backend-tv.cpp    # Original TV tool (reference)
│   ├── alive-tv.cpp      # Standalone TV tool
│   ├── alive-exec.cpp    # LLVM IR interpreter
│   └── alive.cpp         # Alive REPL
├── backend_tv/           # ARM/RISC-V lifter + ASLP
│   ├── lifter.h/cpp      # generateAsm() / liftFunc() API
│   ├── mc2llvm.h/cpp     # MC → LLVM core engine
│   ├── arm2llvm.h/cpp    # ARM instruction lifter
│   ├── arm2llvm_*.cpp    # ARM instruction categories
│   ├── binary_reader.h/cpp # ★ ELF/Mach-O reading (my code)
│   ├── streamerwrapper.h/cpp
│   ├── riscv2llvm.h/cpp  # RISC-V lifter
│   └── aslp/             # ASLp semantics bridge
├── ir/                   # Alive2 core IR
├── smt/                  # Z3/SMT abstraction
├── llvm_util/            # LLVM → Alive2 converter
├── tv/                   # Translation validation plugin
├── util/                 # Utilities
├── tests/                # Test suite
│   ├── lift/             # arm-lifter tests
│   └── arm-tv/           # ARM TV tests
├── docs/                 # Implementation plans & notes
├── scripts/              # Helper scripts
├── cache/                # Redis caching (optional)
└── llvm-project/         # Local LLVM build
```

We only focus on `backend_tv/` and `tools/arm-lifter` for this project.

### Key Data Flows

1. **Backend TV (arm-tv)**:
   ```
   LLVM IR → generateAsm() → assembly text → liftFunc() → LLVM IR (lifted)
                                                        → Alive2 refinement check
   ```

2. **arm-lifter (this fork's tool)**:
   ```
   C source → zig cc → ELF .o + .bc ──→ arm-lifter → LLVM IR (.ll)
                                           │
                        .o ─┬─ disassembleTextSection()  ─┐
                            └─ generateDataSections()   ──┤→ generateFullAsm() → liftFunc()
                              generateBSSDeclarations() ──┘
                        .bc ─→ function signatures + extern declarations (srcModule)
   ```

### arm-lifter CLI

```
arm-lifter <input.o> --src-bc=<input.bc> [--fn=<name>] [options]
```

- `<input.o>`: Input AArch64 ELF `.o` file (positional, required)
- `--src-bc=<file.bc>`: LLVM Bitcode with function signatures and extern declarations (required)
- `--fn=<name>`: Function to lift; omit to lift all defined functions in src-bc (optional)
- `-o / --output-dir=dir`: Output directory, files named `<basename>_<fn>.lifted.ll` (default: stdout)
- `--optimize-tgt`: Optimization level (default O3)
- `--run-replace-ptrtoint`: Replace `ptr→int→ptr` round trips with single GEP (default on)
- `--show-asm`: Print symbolized assembly to stdout for debugging (default off)

---

## Code Ownership & Modification Policy

### My code (free to modify)
| Component | Files |
|-----------|-------|
| Tool entry point | `tools/arm-lifter.cpp` |
| Binary reader / ELF support | `backend_tv/binary_reader.h/cpp` |

### Borrowed from `backend_tv` (minimize modifications)
| Component | Files | Role |
|-----------|-------|------|
| Lifter API | `backend_tv/lifter.h/cpp` | `liftFunc()` — parse assembly → LLVM IR |
| ARM instruction lifting | `backend_tv/arm2llvm.h/cpp` + `arm2llvm_*.cpp` | ARM MC → LLVM IR translation |
| MC→LLVM core | `backend_tv/mc2llvm.h/cpp` | Core MC instruction → LLVM IR engine |
| Streamer wrapper | `backend_tv/streamerwrapper.h/cpp` | MC streamer for instruction emission |
| RISC-V lifting | `backend_tv/riscv2llvm.h/cpp` + `riscv2llvm_insns.cpp` | RISC-V MC → LLVM IR |
| ASLP bridge | `backend_tv/aslp/` | ASLp semantics → LLVM IR |

**Design principle**: `backend_tv/` is treated as upstream reference code. Avoid modifying it when possible; instead, build new functionality in `tools/arm-lifter.cpp` or new files. Call into `backend_tv` APIs, don't refactor them. When modifications are necessary, document them clearly.

### Upstream Alive2 (do not modify without reason)
| Component | Files |
|-----------|-------|
| Alive2 IR | `ir/` |
| SMT layer | `smt/` |
| LLVM utilities | `llvm_util/` |
| Translation validation plugin | `tv/` |
| Core tools | `tools/alive.cpp`, `tools/alive-tv.cpp`, `tools/alive-exec.cpp` |
| Utilities | `util/` |

---

## Build System

- **Build tool**: CMake + Ninja
- **C++ standard**: C++20
- **Required flag**: `-DBUILD_TV=1` for arm-lifter / backend-tv
- **LLVM version**: Tested with `release/22.x` branch, built with RTTI enabled

### Prerequisite: Build local LLVM

arm-lifter requires a local LLVM build with RTTI enabled. Clone into the project root (recommended):

```bash
git clone --branch release/22.x --depth 1 https://github.com/llvm/llvm-project.git
cd llvm-project
cmake -B build -S llvm -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_RTTI=ON \
  -DBUILD_SHARED_LIBS=ON \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_ENABLE_PROJECTS="llvm"
cmake --build build
```

If LLVM is cloned elsewhere, set `LOCAL_LLVM` before building:
```bash
export LOCAL_LLVM=/path/to/your/llvm-project
```

### Build arm-lifter

```bash
./build.sh
```

`build.sh` uses `LOCAL_LLVM` (default: `./llvm-project`) to set `CMAKE_PREFIX_PATH`. Pass extra CMake args via `$@`.

### Manual CMake configure (alternative)

```bash
cmake -B build -S . \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.12 \
  -DCMAKE_PREFIX_PATH=$LOCAL_LLVM/build \
  -DBUILD_TV=1 \
  -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --target arm-lifter
```

### Environment
- `LOCAL_LLVM` — path to local LLVM checkout (default: `<workspace>/llvm-project`)

---

## Testing

### How to run arm-lifter tests
```bash
cd tests/lift
make lift  # compiles .c → .o + .bc → runs arm-lifter
```

### Recompilation (verify lifted IR is valid)
```bash
make recompile-arm64-linux  # compile lifted .ll → AArch64 executable
make recompile-x64-linux    # compile lifted .ll → x86_64 executable
```

**Makefile variables to configure**:
- `SRC` — which `.c` file to test
- `--fn=` parameter in `lift` target — which function to lift

### Available test cases
| File | Content | Difficulty |
|------|---------|-----------|
| `Maze_novarargs.c` | Maze without varargs | Complex |

### Requirements for test inputs
- Must be cross-compiled for AArch64 ELF (use `zig cc` or `clang -target aarch64-unknown-linux-gnu -c`)
- Mach-O format is NOT supported
- Must include DWARF debug info for function signature extraction

### Adding new test cases
1. Create `.c` file in `tests/lift/` (must be compatible with `aarch64-linux` target)
2. Edit Makefile: `SRC=<filename>.c`
3. Edit `--fn=` in `lift` target to specify function name
4. Run `make lift`

---

## Known Limitations

1. **Per-function isolation**: Each `liftFunc()` call creates independent `LLVMContext`/`Module`. Cross-function calls (`bl`) appear as external declarations. Merging into shared-context module requires `mc2llvm` refactoring.
2. **ELF format required**: `mc2llvm.cpp` uses `.starts_with(".rodata")` for constant section detection. Mach-O segment names don't match.
3. **ADRP relocation symbolization**: Basic cases (ADRP+LDR pairs) are handled, but complex GOT patterns may need further work.
4. **Variadic functions not supported**: The lifter cannot handle variadic calls (e.g., `printf`, `sprintf`). Variadic calling conventions pass arguments on the stack in a way the lifter does not model, leading to incorrect or missing arguments. Non-variadic libc calls (e.g., `strlen`, `puts`) work correctly when `--src-bc` is provided.
5. **`nocreateundeforpoison` attribute stripped**: LLVM 20+ intrinsic functions (e.g., `llvm.sadd.with.overflow.i32`) carry the `nocreateundeforpoison` attribute, which older LLVM versions used by `zig cc` don't recognize, causing "unterminated attribute group" parse errors. This attribute is automatically stripped from all functions before outputting the lifted `.ll` file (see `tools/arm-lifter.cpp:liftOneFunction()`, the `removeFnAttr(Attribute::NoCreateUndefOrPoison)` loop before `moduleToString()`).
6. **ASLP code can be removed**: The ASLP (ARM Spec Language Processor) bridge code in `backend_tv/aslp/` and its integration points (`arm2llvm_insns.cpp`, `arm2llvm.h/cpp`, `mc2llvm.h`) are currently guarded by `#ifdef BUILD_ASLP` and disabled by default. The `lifter_interface_llvm` base class has been moved from `backend_tv/aslp/interface.h` to `backend_tv/interface.h` so that `mc2llvm` can compile without ASLP. Since `arm-lifter` always runs with `ASLP=false` and the ASLP server is not used, the ASLP code is dead weight. Future cleanup could remove the ASLP code entirely, along with the `bridge` library dependency and the `BUILD_ASLP` CMake option.
7. **Lifter coupled to Alive2 verification pipeline**: `arm-lifter` links against the full Alive2 dependency chain (`ir`, `smt`, `Z3`) because `lifter::liftFunc()` internally creates alive2 IR types and may perform SMT solving (refinement checks) as part of the TV pipeline. `arm-lifter` only uses the lifted `F2` LLVM IR output and discards the source `F1`, but the verification code still executes inside `liftFunc()`. To decouple, `liftFunc()` would need to be refactored into two stages: a pure "lift" phase (MC → LLVM IR only) and a separate "verify" phase (alive2 IR construction + refinement check). This would allow `arm-lifter` to link only against `backend_tv` + `llvm_util` + `util`, dropping the `ir`/`smt`/`Z3` dependencies entirely.
8. **Local LLVM build requirement (unconfirmed)**: The current build setup uses a locally compiled LLVM (from `llvm-project/`) as both the compiler (`clang`/`clang++`) and the CMake dependency (`CMAKE_PREFIX_PATH`). It is unclear whether a system-installed LLVM (e.g. via Homebrew `llvm` package) would work instead. Potential issues include: (a) Alive2 requires LLVM built with `LLVM_ENABLE_RTTI=ON`, which system packages may not provide; (b) version mismatches between the compiler and the linked LLVM libraries; (c) missing CMake config files in system LLVM packages. This needs further investigation — if a system LLVM with RTTI works, the local `llvm-project/` build (~30GB) could be eliminated, significantly simplifying the setup.

---

## Conventions

- **C++ style**: BasedOnStyle: LLVM, 4-space indent
- **Naming**: `camelCase` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE` for constants
- **Error handling**: Use `assert()` for invariants; `exit()` with error message for unsupported operations (following existing codebase pattern)
- **LLVM API**: Use LLVM's raw API patterns (e.g., `new llvm::LoadInst(...)`, `llvm::ConstantInt::get(...)`)
- **Comments**: design decisions should be documented in English

---
