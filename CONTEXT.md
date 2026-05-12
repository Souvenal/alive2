# arm-lifter

This branch (`object-lifter`) is a standalone fork dedicated to **arm-lifter**: a tool that takes an AArch64 ELF `.o` file and produces semantically-equivalent LLVM IR, so that the IR can be re-compiled by LLVM to an x86_64 binary whose observable behavior matches the original ARM64 binary.

The branch will never be merged back to master. Code unrelated to arm-lifter is removable.

## Language

**arm-lifter**:
The CLI tool (`tools/arm-lifter.cpp`). Reads an ELF `.o`, produces an `.ll` file.
_Avoid_: "the lifter" without qualification (ambiguous — `backend_tv` also "lifts").

**Lifting**:
Translating AArch64 machine code from an ELF object into LLVM IR. *Not* decompilation (no high-level structure recovery), *not* refinement checking (no SMT).
_Avoid_: "decompilation", "recompilation" (the latter is a separate downstream step).

**src-bc**:
LLVM Bitcode (`.bc`) produced by the original C→`.o` compile, supplied to arm-lifter via `--src-bc=...`. Provides function signatures, global-variable initializers, and *most* extern declarations. **src-bc is the authoritative source for function signatures** — DWARF describes source-level types and can diverge from the ABI-lowered types the lifter needs (e.g. an aggregate parameter is one `struct` in DWARF but two i64s in src-bc on AArch64). **src-bc is not guaranteed to be complete**: symbols injected by LLVM codegen after IR generation — compiler-rt builtins (`__udivdi3`, `__aarch64_*`), stack-protector hooks (`__stack_chk_*`), `__chkstk`, TLS access helpers, C++ runtime — appear in the `.o` but not in src-bc. arm-lifter falls back to `ObjectLiftContext::getOrCreateGlobalDecl` for missing symbols; the fallback creates an extern declaration whose type is inferred from the call site and may not match the real ABI (see issue 11).
_Avoid_: "source IR", "reference IR".

**Shared Module**:
The single `llvm::Module` that all lifted functions land in (as opposed to one Module per function). Created by arm-lifter; populated by `liftFuncToModule()` calls. Enables cross-function direct calls and complete global definitions.

**ObjectLiftContext** (`lifter_util/object_lift_context.h`):
The object that owns the **Shared Module** and the symbol-resolution policy. Imports src-bc globals up-front; serves as the lookup table when lifting code references a global.

**Validation pipeline** (`scripts/lift_compile_commands.py`):
External scaffolding, *not part of arm-lifter itself*. Takes a `compile_commands.json` from another project, runs arm-lifter on every `.o`, re-codegens each `.ll` to x86_64, links, runs the resulting binary in a VM, and checks observable behavior against the original ARM64 build.

**backend_tv** (`backend_tv/`):
The legacy ARM/RISC-V → LLVM-IR lifting engine inherited from upstream alive2. arm-lifter calls into it (`liftFunc`, `liftFuncToModule`, `arm2llvm`, `mc2llvm`, `streamerwrapper`). Owned but pruned (see ADR-0001) — the ARM lifting subset is treated as "stable, change with care"; ASLP, RISC-V, and the SMT/refinement path are removable dead code. **Every modification must be recorded in `docs/changelog/`.**

## Contracts

The lifter and the validation pipeline have **different** contracts. This matters because it determines what counts as a lifter bug.

| Layer | Contract | Failure mode |
|---|---|---|
| **arm-lifter** | Produce LLVM IR that (a) parses & verifies, (b) re-codegens to x86_64 without errors | IR fails `opt -verify`; codegen crashes |
| **Validation pipeline** | The re-codegened x86_64 binary behaves identically to the original ARM64 binary under the chosen workload | VM run produces different output / exit code / syscall trace |

The lifter itself does **not** guarantee end-to-end behavioral equivalence in-process. That is the validation pipeline's job, and divergences surfaced there are fed back as lifter bugs.

## Relationships

- **arm-lifter** consumes one **ELF `.o`** + one **src-bc**, produces one **`.ll`**.
- All functions in the `.o` are lifted into a single **Shared Module**, owned by an **ObjectLiftContext**.
- **arm-lifter** calls into **backend_tv** for the per-instruction machine-code → IR translation.
- **Validation pipeline** orchestrates many independent **arm-lifter** invocations across a real project's `compile_commands.json`.

## Example dialogue

> **Dev:** "The lifted IR for `main` passes `opt -verify`, but the recompiled x86_64 binary exits with a different code under QEMU. Whose bug is this?"
> **Domain expert:** "It's an arm-lifter bug — the lifter's contract is only (a)+(b), but the validation pipeline surfacing this means the lifted IR doesn't faithfully model the original. Reduce the failing test case, then fix the corresponding instruction handler in `backend_tv/arm2llvm_*.cpp`."

## Flagged ambiguities

- "lifter" was used to mean both **arm-lifter** (the tool) and **backend_tv** (the engine it calls). Resolved: prefer "arm-lifter" for the tool, "backend_tv" for the engine.
- "equivalent" was used to mean both *valid IR* and *behaviorally equivalent binary*. Resolved: those are the two distinct **Contracts** above.
