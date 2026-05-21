from pathlib import Path

import pytest

from conftest import (
    CASES_DIR,
    compile_arm64_binary,
    compile_bc_and_o,
    get_stdin,
    recompile_x86_64,
    run_aarch64_linux_binary,
    run_arm_lifter,
    run_x86_64_linux_binary,
)

# --- Case classification ---
# Each entry: filename_stem -> (mode, reason)
#   mode: "must_pass" — full pipeline, behavioral comparison
#         "xfail"     — known-broken; strict=True means unexpected pass fails the suite
CASE_MARKS = {
    "Maze_novarargs": ("must_pass", None),
    "struct_test": ("must_pass", None),
    "minirepro": ("must_pass", None),
    "init_fini_sections": ("xfail", "arm-lifter crash on TBZW instruction with SEH_Nop"),
    "pgo_sections": ("xfail", "arm-lifter fails — PGO section / function partitioning"),
    "Maze": ("must_pass", None),
    "compiler_rt_int128": ("must_pass", None),
    "indirect_call": ("must_pass", None),
    "printf_minimal": ("must_pass", None),
    "printf_complex": ("must_pass", None),
    "matmul": ("must_pass", None),
}


def discover_cases() -> list[Path]:
    """Return sorted list of all .c files under cases/."""
    return sorted(CASES_DIR.glob("*.c"))


def build_params() -> list:  # list of pytest.ParameterSet
    """Build pytest.param list with marks baked in."""
    params = []
    for src_path in discover_cases():
        name = src_path.stem
        classification, reason = CASE_MARKS.get(name, ("must_pass", None))
        marks = []
        if classification == "xfail":
            marks.append(pytest.mark.xfail(reason=reason, strict=True))
        params.append(pytest.param(src_path, id=name, marks=marks))
    return params


@pytest.mark.parametrize("src_path", build_params())
def test_lift(src_path, tmp_path, workdir):
    """End-to-end test: compile -> lift -> recompile -> run -> compare."""
    name = src_path.stem

    # Step 1: Compile .bc + .o
    bc, o = compile_bc_and_o(src_path, tmp_path)

    # Step 2: Lift with arm-lifter
    lifted_ll = run_arm_lifter(o, bc, tmp_path)

    # Step 3: Compile reference ARM64 binary and recompile to x86_64
    ref = compile_arm64_binary(src_path, tmp_path)
    sub = recompile_x86_64(lifted_ll, tmp_path)

    # Copy binaries to workdir (project-tree path visible to all runners)
    ref_work = workdir / f"{name}_arm64"
    sub_work = workdir / f"{name}_x86_64"
    ref.replace(ref_work)
    sub.replace(sub_work)

    stdin_text = get_stdin(name)

    # Step 5: Run both binaries and compare
    ref_result = run_aarch64_linux_binary(ref_work, stdin_text=stdin_text)
    sub_result = run_x86_64_linux_binary(sub_work, stdin_text=stdin_text)

    assert ref_result.stdout == sub_result.stdout, (
        f"stdout mismatch for '{name}'"
        f"\n--- REF ---\n{ref_result.stdout}"
        f"\n--- SUB ---\n{sub_result.stdout}"
    )
    assert ref_result.returncode == sub_result.returncode, (
        f"exit code mismatch for '{name}': "
        f"ref={ref_result.returncode}, sub={sub_result.returncode}"
    )
