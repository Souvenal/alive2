# Alive2 arm-lifter fork

This fork focuses on lifting AArch64 machine code to LLVM IR and measuring the
code generated from that lifted IR. The current workflow has two primary
components:

1. **`arm-lifter`**: lifts an AArch64 ELF object file into LLVM IR.
2. **`scripts/lift_fragment_mca.py`**: accepts a C source file, drives the
   complete compile/lift/recompile pipeline, and produces per-fragment
   `llvm-mca` comparison reports.

For normal development and corpus testing, build `arm-lifter` once and invoke
the script with a `.c` file. The script creates all intermediate files and
reports automatically.

## Prerequisites

`arm-lifter` requires a local LLVM build with RTTI enabled. The project is
tested with LLVM `release/22.x`, normally cloned as the sibling directory
`../llvm-project`.

```bash
cd /path/to
git clone --branch release/22.x --depth 1 \
  https://github.com/llvm/llvm-project.git

cd llvm-project
cmake -B build -S llvm -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_RTTI=ON \
  -DBUILD_SHARED_LIBS=ON \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_ENABLE_PROJECTS=llvm
cmake --build build
```

If LLVM is elsewhere, set `LOCAL_LLVM` to the LLVM project directory before
building this repository.

The analysis pipeline also requires:

- Python 3.11+ and `uv`
- `clang`, `llvm-dis`, and `llc`
- `qemu-aarch64` and `qemu-x86_64`
- Lima on macOS, or WSL on Windows

See [docs/usage.md](docs/usage.md) for detailed platform setup.

## Main Workflow

### 1. Build the lifter

From the repository root:

```bash
./build.sh
```

This builds:

```text
build/Release/arm-lifter
build/Release/machine-cfg-dump
```

`arm-lifter` runs natively on the host. The script dispatches cross-platform
compilation and binary analysis through the test infrastructure.

### 2. Analyze a C file

Pass the source file directly to the script:

```bash
uv run scripts/lift_fragment_mca.py tests/lift/cases/Maze.c
```

The script performs the complete workflow:

```text
C source
  -> AArch64 .o and LLVM bitcode
  -> arm-lifter
  -> lifted LLVM IR
  -> AArch64 and x86_64 recompilation
  -> provenance fragment extraction
  -> llvm-mca analysis
  -> JSON reports and improved summary
```

By default, output is written under `./output`. Use `-o` to select another
output root:

```bash
uv run scripts/lift_fragment_mca.py input.c -o /path/to/results
```

Use `--function` when only one mapped function is relevant:

```bash
uv run scripts/lift_fragment_mca.py input.c --function main
```

## Corpus Testing

Markdown is disabled by default. This is the intended mode for running many C
files because JSON is smaller and the summary file is sufficient for locating
interesting cases.

```bash
for source in tests/lift/cases/*.c; do
  uv run scripts/lift_fragment_mca.py "$source"
done
```

Each input produces `output/report/<name>.improved-summary.txt`. Check these
files first. A result with no improvements contains:

```text
Improved fragments
==================

No improved fragments found.
```

When improvements exist, the summary lists only the affected function,
architecture, and fragment IDs:

```text
Improved fragments
==================

main ARM64 improved:
  - fragment_3
  - fragment_7
```

After finding an interesting source file, rerun that file with `--write-md` to
generate human-readable reports for manual inspection:

```bash
uv run scripts/lift_fragment_mca.py path/to/interesting.c --write-md
```

The script replaces reports for the same source basename. Inputs processed into
one output directory should therefore have unique basenames.

## Output Directory

The output root is split into build artifacts and reports:

```text
output/
├── build/
│   ├── <name>.bc
│   ├── <name>.o
│   ├── <name>.ll
│   ├── <name>.nodbg.ll
│   ├── <name>.lifted.ll
│   ├── <name>.asm-map.json
│   ├── <name>.lift.log
│   ├── <name>.nodbg.s
│   ├── <name>.lifted.s
│   ├── <name>.lifted.arm64.o
│   ├── <name>.lifted_x86_64
│   └── other compiled objects and binaries
└── report/
    ├── <name>.improved-summary.txt
    ├── <name>.<function>.fragment-mca.json
    ├── <name>.<function>.fragment-mca-arm-improved.json
    ├── <name>.<function>.fragment-mca-x64.json
    ├── <name>.<function>.fragment-mca-x64-improved.json
    └── matching .md files when --write-md is used
```

### `output/build`

This directory contains intermediate and diagnostic artifacts:

| File | Purpose |
|---|---|
| `<name>.o` / `<name>.bc` | Original source compiled for the lifter |
| `<name>.lifted.ll` | LLVM IR produced by `arm-lifter` |
| `<name>.asm-map.json` | Machine-instruction provenance emitted by the lifter |
| `<name>.lift.log` | Full `arm-lifter` stdout and stderr |
| `<name>.nodbg.ll` | Source LLVM IR without debug metadata |
| `<name>.nodbg.s` / `<name>.lifted.s` | Original and lifted AArch64 assembly |
| `<name>.lifted.arm64.o` | Lifted IR recompiled to AArch64 for same-ISA MCA |
| `<name>.lifted_x86_64` | Lifted IR recompiled to x86_64 |

Use these files when debugging lifting, provenance, compilation, or report
generation. They are not the first place to look during corpus triage.

### `output/report`

Read report output in this order:

1. **`<name>.improved-summary.txt`**
   - The primary corpus-triage file.
   - Lists only functions and fragments whose target
     `cycles_per_iteration` is lower than the source.
   - Explicitly says when no improved fragment was found.

2. **`fragment-mca-arm-improved.json`**
   - Contains only improved AArch64-to-AArch64 fragments.
   - This is the reliable same-ISA comparison.
   - For each fragment, compare
     `mca.source.cycles_per_iteration` with
     `mca.target.cycles_per_iteration`.

3. **`fragment-mca-x64-improved.json`**
   - Contains fragments where the reported x86_64 target cycle count is lower.
   - The source is AArch64 and the target is x86_64, so the report is marked
     `cross_isa`.
   - Treat this as a candidate-finding heuristic, not a direct performance
     claim across architectures.

4. **Full `fragment-mca*.json` reports**
   - `fragment-mca.json` is the full AArch64 same-ISA report.
   - `fragment-mca-x64.json` is the full AArch64-to-x86_64 report.
   - These include accepted fragments plus rejected or excluded provenance
     records and are useful when investigating why a fragment was not selected.

5. **Optional Markdown reports**
   - Generated only with `--write-md`.
   - Include summary tables, instruction listings, cycle values, throughput,
     uOps, ratios, and rejection details.
   - Intended for manual inspection after the TXT summary identifies an
     interesting file.

An improved JSON report contains only accepted fragments satisfying:

```text
target.cycles_per_iteration < source.cycles_per_iteration
```

Equal cycles, regressions, rejected fragments, excluded fragments, missing MCA
results, and MCA errors are omitted.

## Script Options

```text
lift_fragment_mca.py <source.c> [options]
```

| Option | Default | Description |
|---|---|---|
| `<source.c>` | required | C source file to compile, lift, and analyze |
| `-o`, `--output-dir` | `./output` | Output root containing `build/` and `report/` |
| `--function` | all mapped functions | Analyze one function only |
| `--write-md` | off | Also generate Markdown reports |
| `--arm-lifter` | configured test path | Override the `arm-lifter` binary |
| `--machine-cfg-dump` | environment/default | Override `machine-cfg-dump` |
| `--mcpu` | `generic` | `llvm-mca` CPU model |
| `--mattr` | empty | `llvm-mca` target attributes |
| `--mca-iterations` | `100` | Number of MCA iterations |
| `--min-source-instructions` | `2` | Minimum source instructions per fragment |
| `--min-target-instructions` | `2` | Minimum target instructions per fragment |

## The Two Core Components

### `arm-lifter`

The native C++ tool reads an AArch64 ELF object and matching LLVM bitcode or
textual IR:

```text
arm-lifter <input.o> --src-bc=<input.bc-or-ll> -o <output.ll>
```

It disassembles executable code, symbolizes data and BSS sections, lifts ARM
instructions into LLVM IR, resolves functions and globals through the source
module, and writes all lifted functions into one LLVM module.

The direct CLI remains useful for lifter debugging, but normal comparison work
should use `scripts/lift_fragment_mca.py` so compilation, provenance collection,
recompilation, and MCA reporting stay consistent.

### `scripts/lift_fragment_mca.py`

This is the main comparison entry point. It owns the end-to-end experiment:

- compiles the source for AArch64;
- invokes the native `arm-lifter`;
- recompiles lifted IR for AArch64 and x86_64;
- correlates source and target instructions using the lifter's assembly map;
- runs `llvm-mca` on strict provenance fragments;
- writes full and improved JSON reports;
- writes one TXT summary for fast triage;
- optionally writes Markdown for manual review.

## Testing

The focused script and fragment-report tests are:

```bash
cd tests/lift
uv run pytest -v test_lift_fragment_mca_script.py test_fragment_mca.py
```

The complete arm-lifter test suite is:

```bash
cd tests/lift
uv run pytest -v
```

See [tests/lift/README.md](tests/lift/README.md) for the full testing guide.

## Project Layout

| Path | Role |
|---|---|
| `tools/arm-lifter.cpp` | Native lifter CLI |
| `lifter_util/` | ELF reading, symbolization, shared-module management, cleanup |
| `backend_tv/` | AArch64 instruction-to-LLVM lifting engine |
| `scripts/lift_fragment_mca.py` | Main single-source comparison workflow |
| `tests/lift/fragment_mca.py` | Provenance fragment construction and MCA reports |
| `tests/lift/` | End-to-end tests and cross-platform tool dispatch |

Architecture and ownership rules are documented in [AGENTS.md](AGENTS.md).
Detailed direct-lifter and legacy project workflows are in
[docs/usage.md](docs/usage.md).
