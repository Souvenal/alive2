# Phase 6: must-pass corpus end-to-end green

Status: `ready-for-agent`

## What to build

After the pytest test framework (issue 12), Phase 5 data optimizations (issue 15), and backend cleanups (issues 05, 06) have landed, iterate on the must-pass corpus until all classified must-pass cases run end-to-end green under the pytest runner.

The test runner drives the reference ARM64 binary natively in lima and the recompiled x86_64 binary via `qemu-x86_64`, asserting stdout and exit code match.

This is an iterative bug-fixing phase: as each test fails, identify the root cause in the lifter pipeline (obj2asm, arm2llvm, mc2llvm, or cleanup passes), fix it, verify the fix doesn't regress existing passes.

## Acceptance criteria

- [ ] All `.c` files in the test corpus are classified as must-pass or xfail (with linked issue number)
- [ ] must-pass cases are in scope for the recompilation comparison (stdout + exit code)
- [ ] xfail cases are marked `strict=True` so an unexpected pass breaks the build
- [ ] All must-pass cases pass end-to-end under `pytest tests/lift/ -v`
- [ ] Pipeline runs cleanly via `lima -- ... qemu-x86_64 ...` for subject binaries

## Blocked by

- issue 12 (pytest test framework — required to run any end-to-end comparison)
- issue 15 (Phase 5 — data sections must be reasonable before corpus-wide testing)
- issue 05 (Remove ASLP — cleans up build)
- issue 06 (Drop Alive2/Z3 link — avoids linking dead dependencies into all test runs)
