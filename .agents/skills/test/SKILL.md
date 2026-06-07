---
name: test
description: Run arm-lifter tests. Full suite, single case, or dev.py inspection workflow.
---

# /test — Run arm-lifter tests

## Prerequisites

The test suite auto-skips if prerequisites are missing. Required:
- `arm-lifter` binary at `build/tools/arm-lifter`
- C compiler (`zig cc` on macOS/Windows, any CC on Linux)
- QEMU user-mode emulators (`qemu-aarch64`, `qemu-x86_64`)
  - macOS: provided via `lima`
- Python 3.11+ with `uv`

## Full test suite

```bash
uv run pytest tests/lift -v
```

This parameterizes over all cases in `tests/lift/cases/` and compares stdout + exit code between the ARM64 reference binary (run under QEMU) and the lifted → recompiled x86_64 binary.

```bash
# Single case
uv run pytest tests/lift -v -k <case_name>

# List all cases without running
uv run pytest tests/lift --collect-only -v
```

## Must-pass vs xfail discipline

Test marks are in `tests/lift/test_lift.py::CASE_MARKS`:

| Mark | Meaning | Example |
|------|---------|---------|
| `must_pass` | Full pipeline must succeed. CI regression if broken. | Maze_novarargs, struct_test, minirepro |
| `xfail(strict)` | Known-broken, linked to an issue. Unexpected pass → suite fails. | Maze (variadic), indirect_call (blr), init_fini_sections (TBZW crash) |

## Development workflow (`dev.py`)

When iterating on a specific test case to make it pass, use `dev.py` for faster feedback than the full pytest pipeline.

```bash
# Lift a single case → output/<case>.lifted.ll + output/<case>.lift.log
uv run python tests/lift/dev.py lift <case>

# Full pipeline → lift + recompile to x86_64 + reference ARM64 binary
uv run python tests/lift/dev.py full <case>

# Run an already-built binary in the test VM (compare behavior)
uv run python tests/lift/dev.py run <case> arm64       # reference ARM64
uv run python tests/lift/dev.py run <case> lifted_x64  # recompiled x86_64

# Clean output directory
uv run python tests/lift/dev.py clean
```

Common flag: `-o <dir>` for custom output path, `--show-asm` to print assembly.

### Output layout

```
output/
├── <case>.bc               # bitcode
├── <case>.o                # compiled object
├── <case>.lifted.ll        # lifted IR (inspect this)
├── <case>.lift.log         # arm-lifter stdout/stderr (instruction-level debug)
├── <case>.lifted_x86_64    # x86_64 binary (re-ran from lifted IR)
└── <case>_arm64            # ARM64 reference binary (compiled from original .c)
```

Typical workflow: run `dev.py lift <case>` → inspect `.lifted.ll` and `.lift.log` → fix the lifter → rebuild → repeat.

## Adding a new test case

1. Put `<name>.c` into `tests/lift/cases/`
2. Add an entry to `test_lift.py::CASE_MARKS` — `must_pass` or `xfail` (with reason)
3. If the program reads stdin, create `tests/lift/cases/<name>.stdin`
4. Verify: `uv run pytest tests/lift -v -k <name>`

## Test architecture

```
.c ──→ zig cc ──→ .o + .bc ──→ arm-lifter ──→ .lifted.ll ──→ zig cc ──→ x86_64 (subject)
  │                  │
  └──→ zig cc ──→ ARM64 ELF (reference) ── qemu-aarch64 ─┐
                          x86_64 (subject) ── qemu-x86_64 ─┼─→ compare stdout + exit code
                                                           ┘
```

## Cross-platform

On macOS and Windows, `conftest.py` forces `zig cc` (bundles Linux sysroot). On Linux, `CC` env var is respected.
