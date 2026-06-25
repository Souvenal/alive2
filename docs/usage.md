# arm-lifter Usage Guide

This guide covers both workflows for using arm-lifter:

1. **Single-file lifting** — lift one `.o` at a time via the CLI
2. **Batch lifting** — lift an entire project from `compile_commands.json`

---

## Prerequisites

### Build arm-lifter

See [README.md](../README.md) § Quick Start for full LLVM and build instructions. In short:

```bash
# Clone & build LLVM (sibling directory)
git clone --branch release/22.x --depth 1 https://github.com/llvm/llvm-project.git
cd llvm-project
cmake -B build -S llvm -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_RTTI=ON \
  -DBUILD_SHARED_LIBS=ON -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_ENABLE_PROJECTS="llvm"
cmake --build build

# Build arm-lifter
cd /path/to/alive2
./build.sh
```

### Linux VM (macOS / Windows)

arm-lifter runs natively on the host. All compilation steps (`.c` → `.o`, `.c` → `.bc`, `.ll` → recompiled binary) run inside a Linux VM for cross-platform reproducibility.

| Host | VM | Install |
|------|----|---------|
| macOS | Lima | `brew install lima` |
| Linux | *(none)* | Native |
| Windows | WSL | Built-in |

In-VM tools needed:
```bash
sudo apt install clang llvm qemu-user qemu-user-static
```

On **x86_64 hosts**, also install cross-compiler support:
```bash
sudo apt install gcc-aarch64-linux-gnu libc6-dev-arm64-cross
```

---

## Workflow 1: Single-file lifting

Use this for debugging, testing, or lifting individual `.o` files.

### Step 1: Compile source to `.o` + `.bc`

```bash
# Inside the Linux VM (or natively on Linux):
clang -target aarch64-linux-gnu -O2 -c foo.c -o foo.o
clang -target aarch64-linux-gnu -O2 -S -emit-llvm foo.c -o foo.ll
```

On macOS, prefix with `lima --`:
```bash
lima -- clang -target aarch64-linux-gnu -O2 -c foo.c -o foo.o
lima -- clang -target aarch64-linux-gnu -O2 -S -emit-llvm foo.c -o foo.ll
```

> **Using `.ll` instead of `.bc`**: textual IR is recommended. It avoids version
> mismatches between the VM's LLVM and the LLVM arm-lifter was built against.

### Step 2: Run arm-lifter

```bash
# On the host (arm-lifter is a native binary):
build/Release/arm-lifter foo.o --src-bc=foo.ll -o foo.lifted.ll
```

### Step 3: Recompile and test

```bash
# Recompile lifted IR to x86_64 (inside VM):
lima -- clang -target x86_64-linux-gnu -static foo.lifted.ll -o foo_lifted_x86_64

# Run both binaries and compare:
lima -- qemu-aarch64 foo_arm64         # reference
lima -- qemu-x86_64 foo_lifted_x86_64  # lifted → x86_64
```

### CLI reference

```
arm-lifter <input.o> --src-bc=<input.bc> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `<input.o>` | *(required)* | AArch64 ELF `.o` file |
| `--src-bc=<file>` | *(required)* | `.bc` or `.ll` with function declarations and global types |
| `-o / --output-file=<file>` | `<input_basename>.lifted.ll` | Output path for lifted IR |
| `--run-replace-ptrtoint` | `true` | Replace `ptrtoint→add→inttoptr` chains with `getelementptr` |
| `--run-cleanup` | `true` | Run mem2reg, dce, simplifycfg, globaldce on lifted IR |
| `--show-asm` | `false` | Print intermediate re-assemblable assembly to stdout |

### Typical outputs

On success, arm-lifter prints:
```
Lifting 3 function(s) from foo.o

========== Lifting function: init ==========
========== Lifting function: compute ==========
========== Lifting function: main ==========
Lifted IR saved to foo.lifted.ll

========== Done: lifted 3 function(s) successfully ==========
```

On error (missing function in src-bc):
```
ERROR: Function 'helper' not found in src-bc module
```

On error (unrecognized external symbol):
```
ERROR: global symbol '__some_missing_symbol' not found
```

### Development workflow (`dev.py`)

For test cases, use the `dev.py` tool for a faster iteration loop:

```bash
cd tests/lift

# Lift a case — produces <name>.lifted.ll
uv run python dev.py lift minirepro

# Full pipeline — lift + recompile + assembly comparison
# Produces: .nodbg.ll, .lifted.ll, .nodbg.s, .lifted.s, binaries
uv run python dev.py full minirepro

# Compare instruction counts:
grep -c '^  ' output/minirepro.nodbg.ll     # source baseline
grep -c '^  ' output/minirepro.lifted.ll    # lifted target

# Run the recompiled binary:
uv run python dev.py run minirepro arm64         # reference
uv run python dev.py run minirepro lifted_x64    # lifted → x86_64
```

---

## Workflow 2: Batch lifting (compile_commands.json)

Use `scripts/lift_compile_commands.py` to lift every `.o` in a project.

### Critical prerequisite: Clang compiler

**The target project MUST be compiled with `clang`.** arm-lifter needs `.ll` files
(produced by `clang -S -emit-llvm`) for function signatures and global types.
GCC cannot produce LLVM bitcode — the script will refuse to run.

The script validates this on startup:
```
ERROR: compile_commands.json contains non-clang compiler entries.
arm-lifter requires clang to generate .ll files (-emit-llvm).
Found 3 non-clang entries:
  [0] gcc  (file: src/main.c)
  [1] gcc  (file: src/util.c)
  [2] gcc  (file: src/parse.c)

Fix: rebuild the target project with CC=clang CXX=clang++
  e.g.: cmake -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ ..
  or:   CC=clang CXX=clang++ make
```

**How to set up the target project:**

```bash
# CMake projects:
cmake -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
      -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ..
make -j$(nproc)

# Makefile projects:
CC=clang CXX=clang++ make -j$(nproc)

# Autotools:
CC=clang CXX=clang++ ./configure && make -j$(nproc)
```

Then use `bear` to capture compile_commands.json if the build system doesn't
generate it automatically:
```bash
bear -- make -j$(nproc)
```

### Usage

```bash
python3 scripts/lift_compile_commands.py /path/to/compile_commands.json \
    --arm-lifter=build/Release/arm-lifter \
    --linker="clang -target aarch64-linux-gnu" \
    --link-output=lifted_binary
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `compile_commands` | *(required)* | Path to `compile_commands.json` |
| `--arm-lifter=<path>` | `build/Release/arm-lifter` | Path to arm-lifter binary |
| `--linker=<cmd>` | `clang -target aarch64-linux-gnu` | Linker command for final binary |
| `--link-output=<name>` | `a.out` | Output binary name |
| `--dry-run` | — | Print commands without executing |
| `--keep-bc` | — | Keep intermediate `.bc`/`.ll` files |
| `--skip-link` | — | Skip final link step (only produce `.lifted.o` files) |
| `--log=<path>` | `lift_errors.log` | Error log file path |
| `--cc=<cmd>` | *(compiler from json)* | Override compiler (bypasses clang validation) |
| `--vm-prefix=<cmd>` | Auto-detected | VM prefix (e.g. `lima --`) |

### Pipeline per entry

For each entry in `compile_commands.json`, the script runs:

```
[0/4] Compile .c → .o       (in VM, clang)
[1/4] Emit .c → .ll         (in VM, clang -S -emit-llvm)
[2/4] arm-lifter .o + .ll   (native host)
[3/4] Recompile .ll → .o    (in VM, clang)
```

Then optionally links all `.lifted.o` files into a single binary.

### Example: Lift CoreMark

```bash
# 1. Build CoreMark with clang and capture compile_commands.json
cd coremark
CC=clang make PORT_DIR=linux64 compile_commands.json
# (or: bear -- make PORT_DIR=linux64)

# 2. Dry-run to verify entries
python3 /path/to/alive2/scripts/lift_compile_commands.py \
    compile_commands.json --dry-run

# 3. Run the lift
python3 /path/to/alive2/scripts/lift_compile_commands.py \
    compile_commands.json \
    --arm-lifter=/path/to/alive2/build/Release/arm-lifter \
    --link-output=coremark_lifted

# 4. Test the lifted binary
lima -- qemu-x86_64 ./coremark_lifted
```

### Common issues

**"no output file" skip**: The compile_commands entry has no `-o` flag or
`output` field. These entries are skipped — the script can't determine the
object file path.

**"already processed" skip**: Duplicate source files (e.g. both clang-driver
and `-cc1` entries for the same `.c`). Only the first entry per source file
is processed.

**"arm-lifter" failed**: Check the error log (`lift_errors.log` by default).
Common causes: unrecognized instruction, missing compiler-rt symbol, ELF format
issues.

**Linked executable output**: If the compile_commands entry produces an
executable (not a `.o`), the script derives per-TU object paths from source
file stems. This handles projects where all translation units compile to one
executable directly.

---

## Testing

### Run the test suite

```bash
cd tests/lift && uv run pytest -v
```

### Test classification

| Category | Behavior |
|----------|----------|
| `must_pass` | Full pipeline succeeds; regression → CI fails |
| `xfail` (strict) | Expected failure; unexpected pass → CI fails (forces issue closure) |

### Current test cases (12)

| Case | Status | Notes |
|------|--------|-------|
| `minirepro` | must_pass | BL + strb minimal reproducer |
| `struct_test` | must_pass | Struct ABI exhaustive test |
| `Maze_novarargs` | must_pass | Maze game, no variadic calls |
| `Maze` | must_pass | Full maze game with printf |
| `indirect_call` | must_pass | BLT xN indirect calls |
| `compiler_rt_int128` | must_pass | 128-bit integer compiler-rt calls |
| `printf_minimal` | must_pass | Minimal printf usage |
| `printf_complex` | must_pass | Complex printf format strings |
| `matmul` | must_pass | Matrix multiplication |
| `init_fini_sections` | xfail | TBZW + SEH_Nop crash (issue 08) |
| `pgo_sections` | xfail | PGO section partitioning (issue 08) |
| `stream` | xfail | STREAM benchmark crash (issue 26) |

---

## Troubleshooting

### arm-lifter exits with "Cannot create ObjectFile"

The input file is not a valid ELF. Check that:
- The file exists and is readable
- It was compiled for AArch64 (`-target aarch64-linux-gnu`)
- It's a `.o` file, not a linked executable

### "Only AArch64 ELF objects are supported"

The input ELF is for a different architecture. Use `file foo.o` to verify:
```
foo.o: ELF 64-bit LSB relocatable, ARM aarch64
```

### "function not found in src-bc module"

A function exists in the `.o`'s symbol table but not in the `.bc`/`.ll` file.
This can happen when:
- You compiled with different source files for `.o` and `.bc`
- The source file was modified between the two compilations
- The function is a codegen-injected symbol (compiler-rt, stack protector)

### "global symbol not found"

The assembly references an external symbol (via `bl`, `adrp`, etc.) that
couldn't be resolved through any of:
1. The src-bc module's imported globals
2. Implicit LLVM intrinsics
3. The compiler-rt ABI table

Check the symbol name in the error message. If it's a known compiler-rt
builtin, it may need to be added to `lifter_util/codegen_runtime_abi.cpp`.

### Lifted IR passes `opt -verify` but recompiled binary crashes

First, check if the crash reproduces with `--run-cleanup=false`. The cleanup
passes (particularly mem2reg and simplifycfg) can sometimes transform valid
IR into incorrect IR. If disabling cleanup fixes the crash, it's a cleanup
pass bug.

Otherwise, compare the original and recompiled ARM64 assembly:
```bash
cd tests/lift && uv run python dev.py full <case>
diff -u output/<case>.nodbg.s output/<case>.lifted.s
```

Look for:
- Missing instructions in the lifted output
- Incorrect register usage (x8 as indirect result, x16/x17 as intra-procedure scratch)
- Wrong memory access patterns (replaced GEP, broken aliasing)

### ptrtoint→GEP replacement breaks my code

The `--run-replace-ptrtoint` pass replaces `ptrtoint→add→inttoptr` chains
with `GEP` instructions, which preserves pointer provenance. In rare cases
(casts between unrelated pointer types, misaligned arithmetic), this can
produce incorrect code. Disable with `--run-replace-ptrtoint=false`.

---

## Further Reading

- [AGENTS.md](../AGENTS.md) — Architecture, code ownership, conventions
- [CONTEXT.md](../CONTEXT.md) — Domain glossary and contracts (tier definitions)
- [docs/adr/0001-backend-tv-ownership-policy.md](adr/0001-backend-tv-ownership-policy.md) — backend_tv modification policy
- [docs/adr/0002-shared-module-lifting.md](adr/0002-shared-module-lifting.md) — Shared Module architecture decision
- [docs/backend-tv-dataflow.md](backend-tv-dataflow.md) — Data flow from assembly to LLVM IR
- [docs/feature-completeness.md](feature-completeness.md) — Feature completeness evaluation
- [docs/design/ptrtoint-to-gep.md](design/ptrtoint-to-gep.md) — Why ptrtoint→GEP replacement is correct
- [tests/lift/README.md](../tests/lift/README.md) — Test suite details
