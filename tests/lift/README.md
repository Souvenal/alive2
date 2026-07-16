# arm-lifter end-to-end tests

## Architecture

All test compilation and binary execution happens inside a Linux VM for
cross-platform reproducibility. A platform-appropriate VM prefix dispatches
every command to the VM transparently:

| Host platform | VM prefix     | Shared filesystem      |
|---------------|---------------|------------------------|
| Linux         | *(none)*      | Native                 |
| macOS         | `lima --`     | Lima mounts `/Users/`  |
| Windows       | `wsl --`      | WSL mounts via `/mnt/` |

**`arm-lifter` is the only component that runs natively on the host** — it
is a macOS binary on Darwin, a Linux binary on Linux, etc.

Inside the Linux VM the standard LLVM toolchain (`clang`, `llvm-dis`, `llc`)
is used for all compilation steps, replacing the old `zig cc` approach.

## Prerequisites

### Host prerequisites

- **`arm-lifter`** built (`./build.sh` at project root, or set `ARM_LIFTER`)
- **macOS**: Lima VM running (`limactl start`). Homebrew: `brew install lima`
- **Windows**: WSL installed and configured

### Linux VM prerequisites (inside Lima or WSL)

```
sudo apt install clang llvm qemu-user gcc-x86-64-linux-gnu libc6-dev-amd64-cross
```

| Tool | Purpose |
|------|---------|
| `clang` | Compile C → ARM64 bitcode + object files |
| `llvm-dis` | Disassemble `.bc` → `.ll` for IR comparison |
| `llc` | LLVM static compiler for assembly comparison |
| `qemu-aarch64` | Run ARM64 test binaries |
| `qemu-x86_64` | Run recompiled x86_64 test binaries |
| `gcc-x86-64-linux-gnu` | Cross-linker for x86_64 recompilation on ARM64 hosts |
| `libc6-dev-amd64-cross` | x86_64 static libc for `-static` linking on ARM64 hosts |

On **x86_64 Linux** hosts the `gcc-x86-64-*` packages are unnecessary (native
linking); `gcc-aarch64-linux-gnu + libc6-dev-arm64-cross` is needed instead
for the ARM64 compile step.

Checks run automatically before each test session; missing tools produce a
clear skip message.

## Running tests

```bash
# Full suite (auto-skips if prerequisites missing)
cd tests/lift && uv run pytest -v

# Single case
cd tests/lift && uv run pytest -v -k maze_novarargs

# List all cases without running
cd tests/lift && uv run pytest --collect-only -v
```

### Test classification

Cases are classified in `test_lift.py::CASE_MARKS`:

| Mode | Behavior |
|------|----------|
| `must_pass` | Full pipeline: compile → lift → recompile → compare stdout + exit code between reference and recompiled binary |
| `xfail` | Expected failure (strict). Unexpected pass → test suite fails, forcing issue closure |

Current cases:

| Case | Class | Reason |
|------|-------|--------|
| `Maze_novarargs` | must_pass | Maze game, no variadic functions |
| `struct_test` | must_pass | Struct ABI exhaustive test |
| `minirepro` | must_pass | BL + strb minimal reproducer |
| `Maze` | must_pass | Full maze game |
| `indirect_call` | must_pass | BLT xN indirect calls |
| `compiler_rt_int128` | must_pass | 128-bit integer compiler-rt calls |
| `printf_minimal` | must_pass | Minimal printf usage |
| `printf_complex` | must_pass | Complex printf format strings |
| `matmul` | must_pass | Matrix multiplication benchmark |
| `init_fini_sections` | xfail | arm-lifter crashes on TBZW + SEH_Nop |
| `pgo_sections` | xfail | arm-lifter doesn't support PGO section partitioning |
| `stream` | xfail | arm-lifter crash on STREAM benchmark opcode |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ARM_LIFTER` | `build/Release/arm-lifter` | Path to arm-lifter binary |
| `CC` | `clang` | C compiler (resolved via VM PATH; must support `-target`) |
| `LLVM_DIS` | `llvm-dis` | LLVM disassembler (resolved via VM PATH) |
| `LLC` | `llc` | LLVM static compiler (resolved via VM PATH) |
| `CFLAGS` | `-target aarch64-linux-gnu -fno-sanitize=all -O2` | Compiler flags |

## Development workflow (`dev.py`)

`dev.py` is the ad-hoc tool for inspecting lift results without running the full test pipeline.
It shares the same pipeline functions as the test suite.

All dev.py commands should be run from `tests/lift/` so output lands in
`tests/lift/output/` (easier than navigating the project root):

```bash
cd tests/lift
uv run python dev.py lift minirepro
uv run python dev.py full minirepro
uv run python asm_diff.py output/minirepro.asm-map.json output/minirepro.lifted_x86_64 --fn main
```

For a one-command interactive correlation viewer from any C file:

```bash
uv run python dev.py viewer cases/minirepro.c
```

This compiles the source to ARM64 `.o` and `.bc`, lifts it with an instruction
map, recompiles the lifted IR to x86_64, writes
`output/minirepro.cfg-viewer.html`, and opens it in the default browser. It
includes every function shared by the instruction map, original object, and
lifted binary. Use the function selector in the HTML to switch views, or pass
`-f <function>` to generate a viewer containing only one function. Use
`--without-cleanup` to inspect the raw lifted IR, and repeat
`--cflag=<flag>` for source-specific compiler flags.

`dev.py full` also produces `<case>.nodbg.ll` — source IR compiled with `-g0`
(no debug metadata). Compare its instruction count against the lifted IR:

```bash
# Instruction count comparison (nodbg vs lifted)
grep -c '^  ' output/minirepro.nodbg.ll    # source baseline
grep -c '^  ' output/minirepro.lifted.ll   # lifted target (goal: approach nodbg)
```

Output directory (`output/` by default):

```
output/
├── minirepro.bc               # bitcode
├── minirepro.o                # compiled object
├── minirepro.nodbg.ll         # source IR, no debug info (comparison baseline)
├── minirepro.ll               # source IR, with dbg info
├── minirepro.lifted.ll        # lifted IR (inspect this)
├── minirepro.lift.log         # arm-lifter stdout/stderr (instruction-level debug)
├── minirepro.asm-map.json     # structured ARM instruction correlation records
├── minirepro.lifted_x86_64    # x86_64 binary (re-ran from lifted IR)
└── minirepro_arm64            # ARM64 reference binary (compiled from original .c)
```

**Primary goal**: lifted IR instruction count must approach `*.nodbg.ll`.
Every instruction in the lifted IR that doesn't appear in the nodbg baseline is
bloat from Issue 21 (register-alloca modeling).

Custom output directory:

```bash
cd tests/lift
uv run python dev.py -o /tmp/out lift maze_novarargs
```

### Instruction Correlation

`dev.py lift`, `full`, and `full-without-optimize` request
`<case>.asm-map.json` from `arm-lifter`. The versioned sidecar is the
machine-readable ARM-side source of truth for instruction correlation; the
`.lift.log` remains diagnostic output only.

Each `arm_inst_id` is local to one lifted function. `dwarf_line` is the
synthetic line number attached to lifted IR as `arm_inst_id + 1`, using
`arm_asm.s` as the debug file. `asm_diff.py` uses that line number to group
zero or more recompiled target instructions. An ARM instruction may therefore
be optimized away, and several target instructions may share one ARM record.

### Basic-Block Correlation

`cfg.py --asm-map` lifts the instruction-level DWARF relation into an
unweighted many-to-many relation between source MC blocks and target machine
blocks:

```bash
uv run python cfg.py output/minirepro.lifted_arm64 \
  --asm-map output/minirepro.asm-map.json \
  -f main --text
```

Write the relation evidence as JSON:

```bash
uv run python cfg.py output/minirepro.lifted_x86_64 \
  --asm-map output/minirepro.asm-map.json \
  -f main --relations-json output/minirepro.block-relations.json
```

Each relation records the deduplicated `arm_inst_ids` that connect a source
`mc_block` to a target address block. Empty assembly records, `SEH_Nop`, and
the synthetic entry branch are excluded. No score or one-to-one matching is
imposed.

Render the original and lifted CFGs in one image:

```bash
uv run python cfg.py output/minirepro.lifted_x86_64 \
  --source-object output/minirepro.o \
  --asm-map output/minirepro.asm-map.json \
  -f main
```

The source CFG is drawn on the left and the lifted CFG on the right. Each
connected component of the many-to-many block relation gets matching colored
frames on both sides and a bidirectional arrow between the frames. Original
object instructions are aligned to non-synthetic `arm_inst_id` records in
instruction order; a count mismatch is reported as an error.

Generate a self-contained interactive HTML viewer:

```bash
uv run python cfg.py output/minirepro.lifted_x86_64 \
  --source-object output/minirepro.o \
  --asm-map output/minirepro.asm-map.json \
  -f main --viewer
```

The initial view shows only relation groups. Select a group to inspect its
source and lifted blocks, then select a block to inspect its instructions.
Use `--open` with `--viewer` to open the HTML viewer directly.

Selecting a block shows its direct block-relation pairs. Select one pair to
show its `ARM ID | Original | Lifted` DWARF provenance evidence. Either side
may contain multiple instructions for one ARM ID; this records correlation
without implying a one-to-one semantic mapping.

The overview is also a group-level CFG: blue edges aggregate original CFG
edges, orange edges aggregate lifted CFG edges, and dashed `<->` edges carry
the cross-side instruction evidence. Gray nodes represent unmatched source
blocks, unmatched target blocks, or source instructions without direct target
block evidence.

## Adding a new test case

1. Put `<name>.c` into `cases/`
2. Add an entry to `test_lift.py::CASE_MARKS` — `must_pass` or `xfail` (with reason)
3. If the program reads stdin, create `cases/<name>.stdin`:
   ```
   w
   ```
   Test runner automatically pipes stdin. If no sidecar, stdin is empty.
4. Verify: `cd tests/lift && uv run pytest -v -k <name>`

## File structure

```
tests/lift/
├── conftest.py        # Pipeline functions + cross-platform runners + fixture
├── test_lift.py       # Parametrized pytest, one case per .c file
├── dev.py             # Development CLI for ad-hoc lift/recompile/run
├── cases/*.c          # Test case source files
├── cases/*.stdin      # Optional stdin sidecar files
├── output/            # dev.py output (gitignored)
├── build/             # pytest runtime artifacts (gitignored)
└── README.md
```

## Cross-platform dispatch

All commands (compilation, disassembly, and binary execution) are dispatched
through `_vm_prefix()` which prepends the platform-appropriate VM launcher:

| Task | macOS | Linux | Windows |
|------|-------|-------|---------|
| `clang ...` | `lima -- clang ...` | `clang ...` | `wsl -- clang ...` |
| `llvm-dis ...` | `lima -- llvm-dis ...` | `llvm-dis ...` | `wsl -- llvm-dis ...` |
| `llc ...` | `lima -- llc ...` | `llc ...` | `wsl -- llc ...` |
| `qemu-aarch64 <bin>` | `lima -- qemu-aarch64 <bin>` | `qemu-aarch64 <bin>` | `wsl -- qemu-aarch64 <wsl-path>` |
| `qemu-x86_64 <bin>` | `lima -- qemu-x86_64 <bin>` | `qemu-x86_64 <bin>` | `wsl -- qemu-x86_64 <wsl-path>` |
| `arm-lifter` | **Direct (native)** | **Direct (native)** | **Direct (native)** |
