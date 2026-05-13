# arm-lifter end-to-end tests

## Cross-compilation background

All test binaries target AArch64 ELF (Linux). On macOS/Windows, only `zig cc` bundles a
Linux sysroot — regular clang cannot cross-compile to Linux. `conftest.py` detects the
platform and forces `zig cc` on non-Linux, ignoring any `CC` environment variable.

## Prerequisites

- `arm-lifter` built (`./build.sh` at project root, or set `ARM_LIFTER`)
- Lima VM `lima-default` running (`limactl start`) — macOS only
- `qemu-aarch64` + `qemu-x86_64` available inside Lima/WSL or on PATH
- `zig` on PATH (required on macOS/Windows; optional on Linux with custom CC)

Checks run automatically before each test session; missing tools produce a clear skip message.

## Running tests

```bash
# Full suite (auto-skips if prerequisites missing)
uv run pytest tests/lift -v

# Single case
uv run pytest tests/lift -v -k maze_novarargs

# List all cases without running
uv run pytest tests/lift --collect-only -v
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
| `init_fini_sections` | xfail | arm-lifter crashes on TBZW + SEH_Nop |
| `pgo_sections` | xfail | arm-lifter doesn't support PGO section partitioning |
| `Maze` | xfail | Issue 03 — variadic functions |
| `indirect_call` | xfail | Issue 09 — blr xN indirect calls |

## Development workflow (`dev.py`)

`dev.py` is the ad-hoc tool for inspecting lift results without running the full test pipeline.
It shares the same pipeline functions as the test suite.

```bash
# Lift a single case → output/<case>.lifted.ll + output/<case>.lift.log
uv run python tests/lift/dev.py lift minirepro

# Full pipeline → lift + recompile to x86_64 + reference ARM64 binary
uv run python tests/lift/dev.py full minirepro

# Run an already-built binary in the test VM
uv run python tests/lift/dev.py run maze_novarargs arm64       # reference ARM64
uv run python tests/lift/dev.py run minirepro lifted_x64        # recompiled x86_64

# Clean output directory
uv run python tests/lift/dev.py clean
```

Output directory (`output/` by default):

```
output/
├── minirepro.bc               # bitcode
├── minirepro.o                # compiled object
├── minirepro.lifted.ll        # lifted IR (inspect this)
├── minirepro.lift.log         # arm-lifter stdout/stderr (instruction-level debug)
├── minirepro.lifted_x86_64    # x86_64 binary (re-ran from lifted IR)
└── minirepro_arm64            # ARM64 reference binary (compiled from original .c)
```

Custom output directory:

```bash
uv run python tests/lift/dev.py -o /tmp/out lift maze_novarargs
```

## Adding a new test case

1. Put `<name>.c` into `cases/`
2. Add an entry to `test_lift.py::CASE_MARKS` — `must_pass` or `xfail` (with reason)
3. If the program reads stdin, create `cases/<name>.stdin`:
   ```
   w
   ```
   Test runner automatically pipes stdin. If no sidecar, stdin is empty.
4. Verify: `uv run pytest tests/lift -v -k <name>`

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

## Cross-platform runner dispatch

Every test binary runs under QEMU for reproducible cross-architecture execution:

| Platform | AArch64 ref | x86_64 subject |
|----------|-------------|----------------|
| macOS    | `lima -- qemu-aarch64 <bin>` | `lima -- qemu-x86_64 <bin>` |
| Linux    | `qemu-aarch64 <bin>` | `qemu-x86_64 <bin>` |
| Windows  | `wsl -- qemu-aarch64 <wsl-path>` | `wsl -- qemu-x86_64 <wsl-path>` |
