# arm-lifter

This branch (`lifter`) is a standalone fork dedicated to **arm-lifter**: a tool that takes an AArch64 ELF `.o` file and produces semantically-equivalent LLVM IR.

The ultimate purpose is a **compiler backend optimization cross-validation platform**:
1. **Lift faithfully** — translate AArch64 machine code to LLVM IR preserving all observable behavior.
2. **Recompile to ARM64** — verify the lifted IR re-compiles to ARM64 assembly whose instruction count is comparable to the original (no systemic bloat), so it can serve as a baseline for efficiency comparisons.
3. **Cross-compile to x86_64** — recompile the lifted IR to x86_64 via LLVM, then contrast the two backends' codegen quality. When the x86_64 output is meaningfully better than the original ARM64, the ARM backend has an optimization to learn.

The branch will never be merged back to master. Code unrelated to arm-lifter is removable.

## Language

**arm-lifter**:
The CLI tool (`tools/arm-lifter.cpp`). Reads an ELF `.o`, produces an `.ll` file.
_Avoid_: "the lifter" without qualification (ambiguous — `backend_tv` also "lifts").

**Lifting**:
Translating AArch64 machine code from an ELF object into LLVM IR. *Not* decompilation (no high-level structure recovery), *not* refinement checking (no SMT).
_Avoid_: "decompilation", "recompilation" (the latter is a separate downstream step).

**src-bc**:
LLVM Bitcode (`.bc`) produced by the original C→`.o` compile, supplied to arm-lifter via `--src-bc=...`. Provides function signatures, global-variable initializers, and *most* extern declarations.

**src-bc is the authoritative source for function signatures and global types** — DWARF describes source-level types and can diverge from the ABI-lowered types the lifter needs (e.g. an aggregate parameter is one `struct` in DWARF but two i64s in src-bc on AArch64).

**src-bc is not guaranteed to be complete**: symbols injected by LLVM codegen after IR generation — compiler-rt builtins (`__udivdi3`, `__aarch64_*`), stack-protector hooks (`__stack_chk_*`), `__chkstk`, TLS access helpers, C++ runtime — appear in the `.o` but not in src-bc. arm-lifter resolves these through a multi-step fallback chain:
1. Look up the symbol in **src-bc globals** (already imported into the Shared Module).
2. Check **implicit intrinsics** (hardcoded LLVM intrinsic types).
3. Check the **compiler-rt ABI table** (`lookupRuntimeFunctionType` in `codegen_runtime_abi.cpp`) — a curated table of known runtime symbols and their canonical function types.
4. If none match, arm-lifter exits with error. There is **no type-guessing fallback** — an unrecognised symbol is a hard failure.

_Avoid_: "source IR", "reference IR".

**Shared Module**:
The single `llvm::Module` that all lifted functions land in (as opposed to one Module per function). Created by arm-lifter; populated by `liftFuncToModule()` calls. Enables cross-function direct calls and complete global definitions.

**ObjectLiftContext** (`lifter_util/object_lift_context.h`):
The object that owns the **Shared Module** and the symbol-resolution policy. Its responsibilities:
- **Import src-bc globals** — `importSrcBcGlobals()` copies all global variables (with initializers) and function declarations from src-bc into the Shared Module once, before any lifting begins.
- **Global variable lookup** — `lookupGlobal()` wraps `Module::getGlobalVariable()`; `getOrCreateGlobalDecl()` wraps `Module::getOrInsertGlobal()` for variables that need an external declaration.
- **Section label mapping** — `registerSectionLabel()` / `getSectionLabel()` records which synthetic global label (e.g. `__sec_5`) corresponds to which ELF section (e.g. `.rodata.str1.1`).
- **Offset-based global resolution** — `buildGlobalOffsetMap()` / `lookupGlobalAtOffset()` matches ELF section bytes at a given offset against src-bc global variable initializers, recovering the correct global name for section+addend relocation references (primarily string literals).

**backend_tv** (`backend_tv/`):
The legacy ARM/RISC-V → LLVM-IR lifting engine inherited from upstream alive2. arm-lifter calls into it (`liftFunc`, `liftFuncToModule`, `arm2llvm`, `mc2llvm`, `streamerwrapper`). Owned but pruned (see ADR-0001) — the ARM lifting subset is treated as "stable, change with care"; ASLP, RISC-V, and the SMT/refinement path are removable dead code.

**Validation pipeline** (`scripts/lift_compile_commands.py`):
External scaffolding, *not part of arm-lifter itself*. Takes a `compile_commands.json` from another project, runs arm-lifter on every `.o`, re-codegens each `.ll` to x86_64, links, runs the resulting binary in a VM, and checks observable behavior against the original ARM64 build. Currently **dormant** — kept for future use after arm-lifter matures (see Future Topics).

**Machine CFG**:
The intraprocedural control-flow graph recovered from one machine-code function. Its blocks, instructions, and edges are facts about one binary and do not contain cross-binary matches or performance estimates.
_Avoid_: Combined CFG, correlated CFG

**Block Correlation**:
Evidence and a decision relating one original Machine CFG block to one recompiled Machine CFG block through shared `arm_inst_id` provenance. A correlation is not a control-flow edge.
_Avoid_: CFG edge, block group

**MCA Analysis Region**:
An instruction sequence derived from a correlated machine block for isolated `llvm-mca` analysis. A region may contain the block body only or the complete block including its terminator.
_Avoid_: Basic block when referring to the performance-analysis input

**Cost Comparison**:
A comparison of two MCA Analysis Region results produced with the same ISA, CPU model, feature set, and analysis policy. It is a static scheduling-model comparison, not an execution-time measurement.
_Avoid_: Benchmark result, runtime speedup

## Contracts

arm-lifter has three tiers of contract. The first is strict (broken = bug), the second is measured (tracked as a metric), the third is aspirational (future goal).

| Tier | Contract | Failure / tracking |
|---|---|---|
| **1 — Correctness** | Produce LLVM IR that (a) parses & verifies, (b) faithfully models the original ARM64 semantics. The lifted IR, when recompiled to any target and run, must produce the same observable behavior (return code, memory writes, stdout) as the original binary. | Recompiled binary diverges under QEMU; `opt -verify` fails |
| **2 — Compactness** | The lifted IR, when recompiled to ARM64 via `llc`, must produce assembly whose instruction count is comparable to the original ARM64 assembly. Systemic bloat (e.g. register→alloca lowering adding ~3× instructions) defeats the purpose of backend comparison. | Instruction count gap vs original ARM64 assembly |
| **3 — Backend insight** | The lifted IR should serve as a neutral input for comparing LLVM's AArch64 and x86_64 backends. When x86_64 codegen is measurably better, the ARM backend has an optimization to learn. | *Aspirational — methodology still being defined* |

## Relationships

- **arm-lifter** consumes one **ELF `.o`** + one **src-bc**, produces one **`.ll`**.
- All functions in the `.o` are lifted into a single **Shared Module**, owned by an **ObjectLiftContext**.
- **arm-lifter** calls into **backend_tv** for the per-instruction machine-code → IR translation.
- **Validation pipeline** orchestrates many independent **arm-lifter** invocations across a real project's `compile_commands.json`.

## Example dialogue

> **Dev:** "The lifted IR for `main` passes `opt -verify`, but the recompiled ARM64 binary has 3× the instructions of the original assembly. Whose bug is this?"
> **Domain expert:** "It's Tier-2 — Compactness — not a correctness bug, but it defeats the project's purpose. Open an issue, profile which instructions are causing the bloat, and fix the corresponding code in `arm2llvm_*` or `lifter_cleanup`."

> **Dev:** "The lifted IR recompiles to both ARM64 and x86_64 with the same instruction count, and both binaries execute correctly. How do I compare backend quality?"
> **Domain expert:** "This is Tier 3 — the methodology is still being defined. Start by inspecting the two `llc` outputs side by side, looking for optimisations present in one backend but absent in the other."

## Future Topics

**Whole-project validation (`scripts/lift_compile_commands.py`)**:
Once arm-lifter is reliable on individual `.o` files, the validation pipeline should be resurrected and pointed at real C/C++ projects (e.g. SPEC CPU, sqlite, ffmpeg). This will surface cross-module issues, linking challenges, and `__attribute__((constructor))` / init-array patterns that single-file testing misses.

**Backend efficiency comparison methodology**:
How exactly to compare LLVM's AArch64 and x86_64 codegen via lifted IR is still an open question. Candidates include: instruction count deltas per function, dynamic instruction counts under QEMU, static analysis of `llc` output passes, or benchmarking execution time inside the VM. A formal methodology will be designed once the correctness and compactness contracts are solid.

## Flagged ambiguities

- "lifter" was used to mean both **arm-lifter** (the tool) and **backend_tv** (the engine it calls). Resolved: prefer "arm-lifter" for the tool, "backend_tv" for the engine.
- "equivalent" was used to mean both *valid IR* and *behaviorally equivalent binary*. Resolved: these are now Tiers 1 and 2 of the **Contracts** above.
