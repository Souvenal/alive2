# AGENTS.md — Global Project Architecture & Standards

## Project Overview

**Alive2** is a formal verification tool for LLVM compiler transformations, built on SMT (Z3) solving. This is the **arm-tv fork** which extends Alive2 with ARM (AArch64) and RISC-V backend translation validation — lifting machine code back to LLVM IR for formal equivalence checking.

**Core mission of this fork**: Build `arm-lifter` — a standalone tool that lifts ARM64 assembly (from ELF object files) to LLVM IR, targeting recompilation to semantically equivalent x86_64 assembly. The goal is **asm-to-asm accuracy**: the final x86_64 binary should be functionally indistinguishable from the original ARM64 binary, preserving observable behavior (returns, memory writes, side effects) at the assembly level.

### Branch Policy

This is a **standalone branch** (`lifter`) with a single goal: `arm-lifter`. It will **never be merged** into master, and master will never be merged into it. Code unrelated to arm-lifter **can be removed** to reduce clutter and improve code navigation — but deletions must be flagged for **user review** before execution.
This is a **standalone branch** (`lifter`) with a single goal: `arm-lifter`. It will **never be merged** into master, and master will never be merged into it. Code unrelated to arm-lifter **can be removed** to reduce clutter and improve code navigation — but deletions must be flagged for **user review** before execution.

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
├── lifter_util/          # ★ arm-lifter utilities (my code)
│   ├── binary_reader.h/cpp  # ELF reading / DWARF / symbol map
│   ├── obj2asm.h/cpp     # ELF → assembly MemoryBuffer generation
│   └── object_lift_context.h/cpp # Shared Module global lookup
├── backend_tv/           # ARM lifter + ASLP
│   ├── lifter.h/cpp      # generateAsm() / liftFunc() / liftFuncToModule()
│   ├── mc2llvm.h/cpp     # MC → LLVM core engine
│   ├── arm2llvm.h/cpp    # ARM instruction lifter
│   ├── arm2llvm_*.cpp    # ARM instruction categories
│   ├── streamerwrapper.h/cpp
│   └── aslp/             # ASLp semantics bridge
├── ir/                   # Alive2 core IR
├── smt/                  # Z3/SMT abstraction
├── llvm_util/            # LLVM → Alive2 converter
├── tv/                   # Translation validation plugin
├── util/                 # Utilities
├── tests/                # Test suite
│   ├── lift/             # arm-lifter tests (see `tests/lift/README.md`)
│   │   └── cases/        # Test case C sources
│   ├── lift/             # arm-lifter tests (see `tests/lift/README.md`)
│   │   └── cases/        # Test case C sources
│   └── arm-tv/           # ARM TV tests
├── docs/                 # Implementation plans & notes
├── scripts/              # Helper scripts
├── cache/                # Redis caching (optional)
│
├── ../llvm-project/      # Shared LLVM build (sibling directory)
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
                        .o ─┬─ Obj2Asm::convertFullAsm() ─┐
                            │  (disassembleTextSection,   │→ AsmBuffer
                            │   generateDataSections,     │     │
                            │   generateBSSDeclarations) ─┘     │
                        .bc ─→ srcFnDecl (real Function*) ─────┘
                                                                  │
                             SharedModule ← lifted F2
                             (all functions in one Module, adjustSrc skipped)
   ```

### arm-lifter CLI

```
arm-lifter <input.o> --src-bc=<input.bc> [options]
```

- `<input.o>`: Input AArch64 ELF `.o` file (positional, required)
- `--src-bc=<file.bc>`: LLVM Bitcode with function signatures and extern declarations (required)
- `-o / --output-file=file`: Output file path for lifted LLVM IR (default: `<input_basename>.lifted.ll` in current directory)
- `--optimize-tgt`: Optimization level (default O3)
- `--run-replace-ptrtoint`: Replace `ptr→int→ptr` round trips with single GEP (default on)
- `--show-asm`: Print symbolized assembly to stdout for debugging (default off)

---

## Code Ownership & Modification Policy

### My code (free to modify)
| Component | Files |
|-----------|-------|
| Tool entry point | `tools/arm-lifter.cpp` |
| ELF → assembly utilities | `lifter_util/binary_reader.h/cpp`, `lifter_util/obj2asm.h/cpp`, `lifter_util/object_lift_context.h/cpp` |

### Shared with `backend_tv` (see ADR-0001)
### Shared with `backend_tv` (see ADR-0001)
| Component | Files | Role |
|-----------|-------|------|
| Lifter API | `backend_tv/lifter.h/cpp` | `liftFunc()` / `liftFuncToModule()` — parse assembly → LLVM IR |
| ARM instruction lifting | `backend_tv/arm2llvm.h/cpp` + `arm2llvm_*.cpp` | ARM MC → LLVM IR translation |
| MC→LLVM core | `backend_tv/mc2llvm.h/cpp` | Core MC instruction → LLVM IR engine |
| Streamer wrapper | `backend_tv/streamerwrapper.h/cpp` | MC streamer for instruction emission |
| ASLP bridge | `backend_tv/aslp/` | ASLp semantics → LLVM IR |

**Design principle**: `backend_tv/` is pruned-owned (see ADR-0001 in `docs/adr/`). Modify deliberately — prefer extending `tools/arm-lifter.cpp` or new files when adding functionality, but feel free to change `backend_tv/` when the architecture calls for it (e.g., fixing bugs, simplifying flow). Avoid gratuitous refactoring.
**Design principle**: `backend_tv/` is pruned-owned (see ADR-0001 in `docs/adr/`). Modify deliberately — prefer extending `tools/arm-lifter.cpp` or new files when adding functionality, but feel free to change `backend_tv/` when the architecture calls for it (e.g., fixing bugs, simplifying flow). Avoid gratuitous refactoring.

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

- **Build tool**: CMake + Ninja, C++20, requires `-DBUILD_TV=1`
- **LLVM**: `release/22.x` with RTTI, cloned as a sibling directory (`../llvm-project`)
- **Quick**: `./build.sh` or `cmake --build build --target arm-lifter`
- **Env**: `LOCAL_LLVM` overrides LLVM path (default: `../llvm-project`)
- **Full build/LLVM setup guide**: see `/build` skill
- **Build tool**: CMake + Ninja, C++20, requires `-DBUILD_TV=1`
- **LLVM**: `release/22.x` with RTTI, cloned as a sibling directory (`../llvm-project`)
- **Quick**: `./build.sh` or `cmake --build build --target arm-lifter`
- **Env**: `LOCAL_LLVM` overrides LLVM path (default: `../llvm-project`)
- **Full build/LLVM setup guide**: see `/build` skill

---

## Testing

- **Framework**: pytest under `tests/lift/` (see `tests/lift/README.md`)
- **Full suite**: `uv run pytest tests/lift -v`
- **Single case**: `uv run python tests/lift/dev.py lift <case>`
- **Must-pass** — CI regression if broken. **xfail** (strict) — known-broken, linked to an issue.
- **Full test guide**: see `/test` skill

@tests/lift/README.md
- **Framework**: pytest under `tests/lift/` (see `tests/lift/README.md`)
- **Full suite**: `uv run pytest tests/lift -v`
- **Single case**: `uv run python tests/lift/dev.py lift <case>`
- **Must-pass** — CI regression if broken. **xfail** (strict) — known-broken, linked to an issue.
- **Full test guide**: see `/test` skill

@tests/lift/README.md

---

## Known Limitations

Open issues are tracked in `.scratch/arm-lifter/issues/`:

| # | Issue |
|---|-------|
| 01 | Mach-O format not supported |
| 02 | ADRP relocation: complex GOT patterns |
| 03 | Variadic functions not supported |
| 04 | `nocreateundeforpoison` attribute workaround |
| 05 | Remove dead ASLP code |
| 06 | Drop unnecessary Alive2/Z3 linkage |
| 08 | Disassemble all executable sections |
| 10 | Delete remaining non-lifter code |
| 11 | Codegen-injected runtime symbol ABI table |
| 15 | Data segment optimization |
| 16 | Must-pass corpus green |

**Aggregate arguments**: Supported since 2026-05-07 for integer/pointer element types. See `docs/changelog/2026-05-07-aggregate-args.md`.

**Full feature completeness evaluation**: See [docs/feature-completeness.md](docs/feature-completeness.md).

---

## Conventions

- **C++ style**: BasedOnStyle: LLVM, 4-space indent
- **Naming**: `camelCase` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE` for constants
- **Error handling**: Use `assert()` for invariants; `exit()` with error message for unsupported operations (following existing codebase pattern)
- **LLVM API**: Use LLVM's raw API patterns (e.g., `new llvm::LoadInst(...)`, `llvm::ConstantInt::get(...)`)
- **Comments**: design decisions should be documented in English

## Agent skills

### Issue tracker

Issues and PRDs live as markdown files in `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles have default strings (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo: `CONTEXT.md` + `docs/adr/` at the root (both exist as of 2026-05). See `docs/agents/domain.md`.
Single-context repo: `CONTEXT.md` + `docs/adr/` at the root (both exist as of 2026-05). See `docs/agents/domain.md`.

---
