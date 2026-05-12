# pytest end-to-end test runner

Status: `ready-for-agent`

## What to build

Replace the current manual Makefile-driven test workflow (`SRC=` variable edited by hand) with a pytest runner that drives the full test corpus. For each `.c` test case, the runner:

1. Compiles the ARM64 reference binary (natively in Lima)
2. Cross-compiles `.o` + `.bc`, runs arm-lifter, recompiles to x86_64
3. Runs both binaries in Lima (reference natively, x86_64 via `qemu-x86_64`)
4. Asserts stdout + exit code match

Must-pass tests block on failure. Xfail tests (known-broken, linked to open issue) use `strict=True` — an unexpected pass also fails.

### Directory layout target

```
tests/lift/
├── conftest.py          # pytest fixtures: compile, lift, recompile, lima
├── test_lift.py         # parameterized test discovering cases/*.c
├── cases/
│   ├── maze_novarargs.c     # must-pass
│   ├── struct_test.c        # must-pass
│   ├── init_fini_sections.c # must-pass (TBD)
│   ├── maze.c               # xfail (issue 03)
│   ├── indirect_call.c      # xfail (issue 09)
│   └── ...                  # other cases
└── Makefile                  # thin wrapper: make test → pytest
```

### Test skeleton

```python
@pytest.mark.parametrize("case", discover_cases())
def test_lift(case):
    bc, o = compile_bc_and_o(case.src)
    ll = run_arm_lifter(o, bc)
    ref = compile_arm64(case.src)
    sub = recompile_x64(ll)

    ref_out = lima_run(["lima", "--", str(ref)])
    sub_out = lima_run(["lima", "--", "qemu-x86_64", str(sub)])

    assert ref_out.stdout == sub_out.stdout
    assert ref_out.returncode == sub_out.returncode
```

xfail marking via `pytest.mark.xfail(reason="issue 09", strict=True)`.

### Comparison scope

- stdout + exit code (required)
- stderr: skipped (compiler warnings differ between targets)
- syscall trace: skipped (reserved for `lift_compile_commands.py`)
- stdin/argv: sidecar `<name>.input.txt` / `<name>.args.txt` if needed (optional)

## Acceptance criteria

- [ ] `conftest.py` provides fixtures for: compile `.c` to `.bc`+`.o`, call arm-lifter, recompile `.ll` to arm64 and x86_64, run binary in lima
- [ ] `test_lift.py` discovers all `.c` files in `cases/` and runs each as a parameterized test
- [ ] At least one must-pass case (Maze_novarargs) runs end-to-end and passes
- [ ] At least one xfail case exists with `strict=True` — it shows as `xfailed` in the report, not as `failed`
- [ ] `make test` in `tests/lift/` is a thin wrapper around `pytest tests/lift -v`
- [ ] Existing `make lift` + `make recompile-x64-linux` workflow still works for ad-hoc single-case debugging
- [ ] Existing `.c` files are moved into `cases/` and the flat-list `.c` files removed

## Blocked by

None - can start immediately

## Out of scope

- CI integration
- Parallel test execution (pytest can do it, but lima may serialize)
- Coverage of compile flags beyond `-target aarch64-linux -O0 -fno-sanitize=all`
