# Remove remaining non-arm-lifter code (deferred)

Status: `needs-triage`

## Problem

After completing the work in issues `05-remove-aslp-code` and `06-drop-alive2-link`, the project tree still carries large amounts of code that arm-lifter does not use. Per ADR-0001 these are "removable as dead code", but the deletion is deferred until the lifter has been validated end-to-end on a real project — premature removal risks cascading build breakage that distracts from shipping.

## Scope

The following are candidates for removal (rough order of dependency, deepest first):

| # | Path | Note |
|---|---|---|
| 1 | `tools/quick-fuzz.cpp` | Old Alive2 fuzzer, unused |
| 2 | `tools/alive.cpp`, `tools/alive_lexer.*`, `tools/alive_parser.*`, `tools/transform.{cpp,h}`, `tools/tokens.h` | Old Alive REPL + its parser |
| 3 | `tools/alive-tv.cpp`, `tools/alive-exec.cpp`, `tools/alive-jobserver.cpp` | Other Alive frontends |
| 4 | `tools/backend-tv.cpp` | The original backend-TV tool; becomes dead once `ir/`/`smt/` are gone |
| 5 | `tv/` | LLVM-pass TV plugin |
| 6 | `ir/` | Alive2 internal IR |
| 7 | `smt/` | Z3 abstraction (already mostly de-linked by issue 06) |
| 8 | `cache/` | Redis SMT result cache |
| 9 | `llvm_util/` | LLVM↔Alive2 conversion + utilities — **needs surgery**: arm-lifter currently uses `llvm_util/utils.h`. Either move that single header into `lifter_util/`, or thin the directory down to just what arm-lifter needs. |
| 10 | `tests/arm-tv/`, `tests/alive-tv/`, `tests/alive/`, other Alive2 test dirs | Old test corpora unrelated to lifting |
| 11 | `CMakeLists.txt` | Remove the now-unused targets, libs, and Z3 finder |

## Prerequisite

- Issue 05 (remove ASLP) closed
- Issue 06 (drop Alive2/Z3 link) closed
- arm-lifter green on the QEMU validation pipeline against at least one real project (e.g. Maze, or one entry in `compile_commands.json` from a larger codebase)

## Why deferred

The deletion surface is roughly five-figures of LoC across ~50 files. Done after validation, the lifter stays green throughout. Done before, every transient CMake error becomes a blocker for the actual mission.

## Per ADR-0001

Every deletion lands with a corresponding `docs/changelog/` entry.
