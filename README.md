Alive2 — arm-lifter fork
=======================

This is a fork of the [arm-tv branch](https://github.com/regehr/alive2/tree/arm-tv) of Alive2, extended with **arm-lifter** — a tool that lifts ARM64 (AArch64) machine code from ELF object files into semantically faithful LLVM IR.

### What arm-lifter does

```
C source ──→ ELF .o + .bc ──→ arm-lifter ──→ LLVM IR (.ll) ──→ recompile to any target
```

arm-lifter reads an AArch64 ELF `.o` file and a matching `.bc` (or `.ll`) bitcode file, disassembles the object's `.text` section, symbolizes data/BSS sections, and translates every ARM instruction into LLVM IR. All functions in the object are lifted into a single shared `Module` — cross-function calls resolve directly, and global variables retain their full types and initializers from the bitcode.

The lifted IR can be recompiled to any LLVM-supported target (AArch64, x86_64, etc.) for cross-architecture binary translation or backend comparison.

### Quick Start

#### Step 0: Build local LLVM

arm-lifter requires a local LLVM build with RTTI enabled. Tested with the `release/22.x` branch.

```bash
# Clone LLVM as a sibling directory
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

> If LLVM is cloned elsewhere, set `LOCAL_LLVM` before building:
> ```bash
> export LOCAL_LLVM=/path/to/your/llvm-project
> ```

#### Step 1: Build arm-lifter

```bash
./build.sh
```

#### Step 2: Lift a single file

Compile your C source to both `.o` and `.bc` (or `.ll`), then run arm-lifter:

```bash
# Inside a Linux VM (or natively on Linux):
clang -target aarch64-linux-gnu -O2 -c foo.c -o foo.o
clang -target aarch64-linux-gnu -O2 -S -emit-llvm foo.c -o foo.bc

# On the host (arm-lifter runs natively):
build/Release/arm-lifter foo.o --src-bc=foo.bc -o foo.lifted.ll
```

#### Step 3: Batch-lift a whole project

For multi-file projects, use `lift_compile_commands.py` to drive arm-lifter from a `compile_commands.json`:

```bash
python3 scripts/lift_compile_commands.py /path/to/compile_commands.json \
    --arm-lifter=build/Release/arm-lifter
```

> **Important**: The target project **must** be compiled with `CC=clang`. The script validates this on startup and aborts if non-clang entries are found. See [docs/usage.md](docs/usage.md) for details.

#### Try it: batch-lift the bundled example project

The repository includes [`example_project/`](example_project/), a multi-file C project ready for testing:

```bash
# Build the example project (native ARM64, inside Linux VM)
cd example_project
cmake -S . -B build -DCMAKE_C_COMPILER=clang -DCMAKE_BUILD_TYPE=Release
cmake --build build

# Batch-lift all 3 translation units
cd /path/to/alive2
python3 scripts/lift_compile_commands.py example_project/build/compile_commands.json \
    --arm-lifter=build/Release/arm-lifter \
    --link-output=example_project/example_lifted
```

Expected output:
```
Loaded 3 entries from example_project/build/compile_commands.json
Lifting 3 function(s) from main.o
...
Lifting 2 function(s) from calc.o
...
Lifting 1 function(s) from util.o
...
Done. 3/3 succeeded.
```

**Note**: The lifted binary currently crashes (SIGSEGV) due to register-alloca
modeling issues — see Known Limitations below and issue 21. The pipeline itself
succeeds: all 3 TU's lift to valid IR without errors.

### CLI Reference

```
arm-lifter <input.o> --src-bc=<input.bc> [options]
```

| Option | Description |
|--------|-------------|
| `<input.o>` | Input AArch64 ELF `.o` file (positional, required) |
| `--src-bc=<file>` | LLVM bitcode (`.bc`) or textual IR (`.ll`) with function signatures and extern declarations (required) |
| `-o / --output-file=<file>` | Output file path for lifted LLVM IR (default: `<input_basename>.lifted.ll`) |
| `--run-replace-ptrtoint` | Replace `ptr→int→ptr` round trips with single GEP (default: `true`) |
| `--run-cleanup` | Run cleanup passes: mem2reg, dce, simplifycfg, globaldce (default: `true`) |
| `--show-asm` | Print the generated re-assemblable assembly to stdout for debugging (default: `false`) |

**About `--src-bc`**: The lifter needs precise function signatures (parameter types, return type) to produce correct LLVM IR. These are provided via a `.bc`/`.ll` file compiled from the same source alongside the `.o` file. The bitcode also supplies extern global variable types and initializers. Both bitcode and textual IR formats are accepted (textual IR avoids cross-version BC format mismatches).

**Output**: A single `.ll` file containing all functions from the input object lifted into one LLVM `Module`, along with all imported global variables.

### Testing

End-to-end testing lives in `tests/lift/` and uses pytest + the `dev.py` development tool. See [tests/lift/README.md](tests/lift/README.md) for the complete workflow.

```bash
# Run the full test suite (from tests/lift/)
cd tests/lift && uv run pytest -v

# Lift a single case and inspect the output
cd tests/lift && uv run python dev.py lift minirepro

# Full pipeline: lift + recompile + assembly comparison
cd tests/lift && uv run python dev.py full minirepro
```

### Architecture

```
┌──────────────────────────────────┐
│          Tools Layer             │
│         arm-lifter               │
└──────────┬───────────────────────┘
           │
┌──────────▼──────────┐  ┌────────────────────┐
│    lifter_util/     │  │    backend_tv/      │
│  ELF → assembly     │  │  ARM instruction    │
│  Shared Module mgmt │  │  → LLVM IR engine   │
│  Cleanup passes     │  │  (arm2llvm, mc2llvm)│
│  Compiler-rt ABI    │  └─────────┬───────────┘
└─────────────────────┘            │
                          ┌────────▼──────────┐
                          │   Alive2 core     │
                          │   (ir/smt/tv)     │
                          └───────────────────┘
```

### Key Files

| File | Role |
|------|------|
| `tools/arm-lifter.cpp` | CLI entry point, orchestrates the pipeline |
| `lifter_util/obj2asm.h/cpp` | ELF → re-assemblable GAS assembly |
| `lifter_util/binary_reader.h/cpp` | ELF reading, symbol map construction |
| `lifter_util/object_lift_context.h/cpp` | Shared Module ownership, global variable lookup |
| `lifter_util/codegen_runtime_abi.h/cpp` | Compiler-rt symbol ABI table |
| `lifter_util/lifter_cleanup.h/cpp` | Post-lift cleanup passes (mem2reg, dce, etc.) |
| `backend_tv/lifter.h/cpp` | `liftFuncToModule()` — assembly → LLVM IR |
| `backend_tv/arm2llvm.h/cpp` + `arm2llvm_*.cpp` | ARM MC instruction → LLVM IR translation |
| `backend_tv/mc2llvm.h/cpp` | MC → LLVM IR core engine |
| `scripts/lift_compile_commands.py` | Batch lifting from `compile_commands.json` |
| `example_project/` | Ready-to-run multi-file C project for testing batch lifting |

### Known Limitations

- **Register-alloca modeling (issue 21)**: The lifter models all ARM registers as stack `alloca` slots, producing bloated IR with `freeze poison → zext → shl → or → trunc` round-trips for every parameter. Multi-function programs with printf may crash (SIGSEGV). Single functions and simple arithmetic work correctly.
- **ELF format only**: Mach-O and PE are not supported.
- **Variadic functions**: partially supported; complex `printf` patterns may still fail (see issue 26).
- **ADRP relocation**: basic ADRP+LDR pairs are handled; complex GOT patterns may need further work (issue 02).
- **Alive2/Z3 linkage**: arm-lifter still links against the Alive2/Z3 dependency chain. Decoupling is tracked in issue 06.
- **Non-.text executable sections**: only `.text` is disassembled; PGO-partitioned sections (`.text.hot`, `.text.cold`) are not yet handled (issue 08).

Full architectural details and code ownership policy: [AGENTS.md](AGENTS.md).
Detailed usage guide: [docs/usage.md](docs/usage.md).
Feature completeness evaluation: [docs/feature-completeness.md](docs/feature-completeness.md).

---

For upstream Alive2 documentation (alive-tv, Clang plugin, caching, etc.), see the [original Alive2 README](https://github.com/AliveToolkit/alive2/blob/master/README.md) and the [arm-tv branch](https://github.com/regehr/alive2/tree/arm-tv).
