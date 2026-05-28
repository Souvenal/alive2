import re
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

def verify_lifted_globals(lifted_ll: Path, case_name: str) -> None:
    """Structural IR assertions for Issue 20: string globals recovery.

    Checks that the lifted IR contains named @.str.N globals with correct
    ArrayType (not blob StructType), and limits remaining blob globals.
    """
    text = lifted_ll.read_text()

    lines = text.splitlines()

    # Count @.str.N and @str definitions
    str_defs = [l for l in lines if re.match(r'^@(\.str|str)(\.[0-9]+)?\s*=', l)]
    str_refs = len(re.findall(r'@(\.str|str)(\.[0-9]+)?', text))

    # Count @__sec_N references (blob globals)
    blob_refs = len(re.findall(r'@__sec_\d+', text))

    # For must_pass cases with known expected string counts
    # (source BC globals, excluding zeroinitializer which is ConstantAggregateZero)
    expected_str_defs = {
        "Maze_novarargs": 15,  # all globals including zeroinitializer
        "Maze": 13,             # all globals including zeroinitializer
        "minirepro": 0,
        "struct_test": 10,  # 10 @.str globals recovered
    }

    # Verify string globals exist
    if case_name in expected_str_defs:
        exp = expected_str_defs[case_name]
        assert len(str_defs) == exp, (
            f"Expected {exp} string globals in '{case_name}', "
            f"got {len(str_defs)}"
        )

    # Verify blob globals are eliminated — all strings should be resolved
    # to @.str.N globals via the offset-based byte matching.
    if case_name in ("Maze_novarargs", "Maze", "minirepro", "struct_test"):
        assert blob_refs == 0, (
            f"Expected 0 @__sec_ references in "
            f"'{case_name}', got {blob_refs}"
        )

    # Verify linkage — globals should not be private (they're set to weak)
    for line in str_defs:
        if "private" in line:
            print(f"Warning: string global uses private linkage: {line}")

    print(f"[verify_globals] {case_name}: {len(str_defs)} string defs, "
          f"{str_refs} refs; {blob_refs} @__sec_ refs")


# --- Case classification ---
# Each entry: filename_stem -> (mode, reason, extra_cflags)
#   mode: "must_pass" — full pipeline, behavioral comparison
#         "xfail"     — known-broken; strict=True means unexpected pass fails the suite
#   reason: None or str (xfail reason)
#   extra_cflags: list of extra compiler flags (empty list if none needed)
CASE_MARKS: dict[str, tuple] = {
    "Maze_novarargs": ("must_pass", None, []),
    "struct_test": ("must_pass", None, []),
    "minirepro": ("must_pass", None, []),
    "init_fini_sections": ("xfail", "arm-lifter crash on TBZW instruction with SEH_Nop", []),
    "pgo_sections": ("xfail", "arm-lifter fails — PGO section / function partitioning", []),
    "Maze": ("must_pass", None, []),
    "compiler_rt_int128": ("must_pass", None, []),
    "indirect_call": ("must_pass", None, []),
    "printf_minimal": ("must_pass", None, []),
    "printf_complex": ("must_pass", None, []),
    "matmul": ("must_pass", None, []),
    "stream": ("must_pass", None, ["-DSTREAM_ARRAY_SIZE=200", "-DNTIMES=2"]),
}

# Cases where stdout differs between QEMU-aarch64 and QEMU-x86_64
# (e.g., timing-dependent output). Only exit code is compared.
EXIT_CODE_ONLY_NAMES: frozenset[str] = frozenset({"stream"})


def discover_cases() -> list[Path]:
    """Return sorted list of all .c files under cases/."""
    return sorted(CASES_DIR.glob("*.c"))


def build_params() -> list:  # list of pytest.ParameterSet
    """Build pytest.param list with marks baked in."""
    params = []
    for src_path in discover_cases():
        name = src_path.stem
        classification, reason, _ = CASE_MARKS.get(name, ("must_pass", None, []))
        marks = []
        if classification == "xfail":
            marks.append(pytest.mark.xfail(reason=reason, strict=True))
        params.append(pytest.param(src_path, id=name, marks=marks))
    return params


@pytest.mark.parametrize("src_path", build_params())
def test_lift(src_path, tmp_path, workdir):
    """End-to-end test: compile -> lift -> recompile -> run -> compare."""
    name = src_path.stem
    _, _, extra_cflags = CASE_MARKS.get(name, ("must_pass", None, []))

    # Step 1: Compile .bc + .o
    bc, o = compile_bc_and_o(src_path, tmp_path, extra_cflags=extra_cflags)

    # Step 2: Lift with arm-lifter
    lifted_ll = run_arm_lifter(o, bc, tmp_path)

    # Step 3: Verify structural IR properties (Issue 20: string globals recovery)
    verify_lifted_globals(lifted_ll, name)

    # Step 4: Compile reference ARM64 binary and recompile to x86_64
    ref = compile_arm64_binary(src_path, tmp_path, extra_cflags=extra_cflags)
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

    if name in EXIT_CODE_ONLY_NAMES:
        # Timing-dependent output (e.g., STREAM benchmark): only compare exit code
        assert ref_result.returncode == sub_result.returncode, (
            f"exit code mismatch for '{name}': "
            f"ref={ref_result.returncode}, sub={sub_result.returncode}"
        )
    else:
        assert ref_result.stdout == sub_result.stdout, (
            f"stdout mismatch for '{name}'"
            f"\n--- REF ---\n{ref_result.stdout}"
            f"\n--- SUB ---\n{sub_result.stdout}"
        )
        assert ref_result.returncode == sub_result.returncode, (
            f"exit code mismatch for '{name}': "
            f"ref={ref_result.returncode}, sub={sub_result.returncode}"
        )
