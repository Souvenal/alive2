# 0001 — backend_tv ownership policy: pruned-owned

`backend_tv/` was inherited from upstream alive2 as a TV (translation-validation) engine. The `object-lifter` branch repurposes it as the lifting backend for `arm-lifter`. The early development guideline was "don't modify upstream", but real-world bugs and the cleanup goals (remove ASLP, drop Z3 link, etc.) forced revisions. We codify the resulting policy here.

## Decision

`backend_tv/` is treated as **owned but pruned**:

- **Removable as dead code:** the `backend_tv/aslp/` subtree, every `#ifdef BUILD_ASLP` block, `backend_tv/riscv2llvm*`, and the SMT/Alive2 refinement path (Z3 link, `adjustSrc`, `fixupOptimizedTgt`, surrounding helpers).
- **Stable — change with care:** `backend_tv/arm2llvm*.cpp` (ARM instruction translators), `backend_tv/mc2llvm.cpp` (MC→IR engine), `backend_tv/lifter.cpp` (public API). Modify these only to fix concrete bugs surfaced by tests; do not refactor gratuitously.
- **Freely changeable:** new entry points in `backend_tv/lifter.h` (e.g. `liftFuncToModule`), CMake glue, wrapper types.

**Every modification to `backend_tv/` MUST be recorded in `docs/changelog/`**, with a dated filename and a short "why" — both bug fixes and deletions. This is the trail we use to reason about divergence when something breaks.

## Considered alternatives

- **Frozen upstream.** Would force every fix to live in a wrapper layer in `lifter_util/`. Rejected: bugs in `arm2llvm` are not wrappable, and 5 patches already exist (see CLAUDE.local.md).
- **Wide-open / fully owned.** Would allow free refactoring. Rejected: ARM instruction translators carry hard-won correctness for thousands of opcodes; aimless restructuring is high-risk for no gain.

## Consequences

- The "minimize modifications" / "borrowed code" language scattered across CLAUDE.md and module READMEs is now superseded by this ADR. Existing wording is left in place where it is still useful as a tone-setter ("change with care"), but the hard restriction is gone.
- `docs/changelog/` is load-bearing: if a change is not recorded there, it is — by policy — not a sanctioned change.
