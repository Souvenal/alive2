#!/usr/bin/env python3
"""Run the complete lift and provenance-fragment MCA workflow for one C file.

The script keeps arm-lifter native to the host while running clang and LLVM
tools through the same platform VM dispatcher used by tests/lift.

Usage:
    uv run python scripts/lift_fragment_mca.py input.c
    uv run python scripts/lift_fragment_mca.py input.c --write-md
    uv run python scripts/lift_fragment_mca.py input.c -o "$HOME/lift-output"

The output root contains build artifacts under build/ and JSON/Markdown
reports under report/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIFT_TEST_DIR = PROJECT_ROOT / "tests" / "lift"
sys.path.insert(0, str(LIFT_TEST_DIR))

from asm_diff import parse_instruction_map
from conftest import (  # noqa: E402
    ARM_LIFTER,
    CFLAGS,
    CLANG,
    LLC,
    LLVM_DIS,
    _to_vm_path,
    compile_arm64_binary,
    compile_bc_and_o,
    recompile_arm64,
    recompile_x86_64,
    run_command,
    run_in_vm,
)
from fragment_mca import (  # noqa: E402
    _json_report,
    build_fragment_report,
    render_markdown_report,
)


def _check_source(source: Path) -> Path:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"source file not found: {source}")
    if source.suffix != ".c":
        raise ValueError(f"source file must end in .c: {source}")
    return source


def _output_dirs(output_dir: Path | None) -> tuple[Path, Path, Path]:
    root = (
        output_dir.expanduser().resolve()
        if output_dir is not None
        else (Path.cwd() / "output").resolve()
    )
    build_dir = root / "build"
    report_dir = root / "report"
    build_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    return root, build_dir, report_dir


def _require_vm_shared_path(path: Path, description: str) -> None:
    if sys.platform == "darwin" and not path.is_relative_to("/Users"):
        raise ValueError(
            f"{description} must be under /Users on macOS so Lima can "
            f"access it: {path}"
        )


def _clean_outputs(stem: str, output_dir: Path) -> None:
    generated_names = {
        f"{stem}.asm-map.json",
        f"{stem}.bc",
        f"{stem}.lift.log",
        f"{stem}.lifted.arm64.o",
        f"{stem}.lifted.ll",
        f"{stem}.lifted.o",
        f"{stem}.lifted.s",
        f"{stem}.lifted_arm64",
        f"{stem}.lifted_x86_64",
        f"{stem}.ll",
        f"{stem}.nodbg.ll",
        f"{stem}.nodbg.s",
        f"{stem}.o",
        f"{stem}_arm64",
        f"{stem}.improved-summary.txt",
    }
    for path in output_dir.iterdir():
        is_fragment_report = (
            path.name.startswith(f"{stem}.")
            and (
                path.name.endswith(".fragment-mca.json")
                or path.name.endswith(".fragment-mca.md")
                or path.name.endswith(".fragment-mca-improved.json")
                or path.name.endswith(".fragment-mca-improved.md")
                or path.name.endswith(".fragment-mca-arm-improved.json")
                or path.name.endswith(".fragment-mca-arm-improved.md")
                or path.name.endswith(".fragment-mca-x64.json")
                or path.name.endswith(".fragment-mca-x64.md")
                or path.name.endswith(".fragment-mca-x64-improved.json")
                or path.name.endswith(".fragment-mca-x64-improved.md")
            )
        )
        if (
            (path.name in generated_names or is_fragment_report)
            and (path.is_file() or path.is_symlink())
        ):
            path.unlink()


def _run_checked(
    command: list[str],
    *,
    description: str,
    vm: bool = False,
) -> None:
    result = run_in_vm(command) if vm else run_command(command)
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        suffix = f":\n{details}" if details else ""
        raise RuntimeError(
            f"{description} failed (exit {result.returncode}){suffix}"
        )


def _run_lifter(
    source_object: Path,
    source_bc: Path,
    lifted_ll: Path,
    log_path: Path,
    asm_map: Path,
    arm_lifter: Path,
) -> None:
    result = run_command(
        [
            str(arm_lifter),
            str(source_object),
            "--src-bc",
            str(source_bc),
            "-o",
            str(lifted_ll),
            "--asm-map",
            str(asm_map),
        ]
    )
    log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(
            f"arm-lifter failed (exit {result.returncode}); see {log_path}"
        )


def _compile_source_ir(
    source: Path,
    source_bc: Path,
    source_ll: Path,
    nodbg_ll: Path,
) -> None:
    _run_checked(
        [LLVM_DIS, str(_to_vm_path(source_bc)), "-o", str(_to_vm_path(source_ll))],
        description="llvm-dis",
        vm=True,
    )
    _run_checked(
        [
            CLANG,
            *CFLAGS,
            "-g0",
            "-S",
            "-emit-llvm",
            str(_to_vm_path(source)),
            "-o",
            str(_to_vm_path(nodbg_ll)),
        ],
        description="no-debug source compile",
        vm=True,
    )


def _compile_arm_assembly(
    nodbg_ll: Path,
    lifted_ll: Path,
    nodbg_s: Path,
    lifted_s: Path,
) -> None:
    for input_ll, output_s in ((nodbg_ll, nodbg_s), (lifted_ll, lifted_s)):
        _run_checked(
            [
                LLC,
                "-mtriple=aarch64-linux-gnu",
                "-O2",
                str(_to_vm_path(input_ll)),
                "-o",
                str(_to_vm_path(output_s)),
            ],
            description=f"ARM assembly compile for {input_ll.name}",
            vm=True,
        )


def _safe_function_name(function: str) -> str:
    return quote(function, safe="._-") or "unnamed"


def _is_improved_fragment(fragment: dict) -> bool:
    if fragment.get("status") != "accepted":
        return False
    mca = fragment.get("mca")
    if not isinstance(mca, dict):
        return False
    source = mca.get("source")
    target = mca.get("target")
    if not isinstance(source, dict) or not isinstance(target, dict):
        return False
    if source.get("status") != "ok" or target.get("status") != "ok":
        return False
    source_cycles = source.get("cycles_per_iteration")
    target_cycles = target.get("cycles_per_iteration")
    if (
        not isinstance(source_cycles, (int, float))
        or isinstance(source_cycles, bool)
        or not isinstance(target_cycles, (int, float))
        or isinstance(target_cycles, bool)
    ):
        return False
    return target_cycles < source_cycles


def _improved_report(report: dict) -> dict:
    comparison_status = report.get("mca", {}).get("comparison_status")
    return {
        **report,
        "fragment_filter": {
            "kind": "improved_cycles_per_iteration",
            "condition": (
                "target.cycles_per_iteration < "
                "source.cycles_per_iteration"
            ),
            "cross_isa": comparison_status == "cross_isa",
        },
        "fragments": [
            fragment
            for fragment in report["fragments"]
            if _is_improved_fragment(fragment)
        ],
    }


def _render_improved_markdown(report: dict) -> str:
    markdown = render_markdown_report(report)
    lines = markdown.splitlines()
    if lines:
        lines[0] = lines[0].replace(
            "Provenance Fragment MCA",
            "Improved Provenance Fragment MCA",
        )
    note = (
        "> Filter: accepted fragments where target cycles / iteration "
        "is lower than source."
    )
    if report["fragment_filter"]["cross_isa"]:
        note += (
            " Source and target ISAs differ, so these cycle values are "
            "not directly comparable."
        )
    lines[1:1] = ["", note]
    return "\n".join(lines) + "\n"


def _write_report_files(
    report: dict,
    report_path: Path,
    *,
    improved: bool = False,
    write_md: bool = False,
) -> tuple[Path, Path | None]:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path = report_path.with_suffix(".md")
    report_path.write_text(
        json.dumps(_json_report(report), indent=2) + "\n",
        encoding="utf-8",
    )
    if not write_md:
        return report_path, None
    markdown_path.write_text(
        _render_improved_markdown(report)
        if improved
        else render_markdown_report(report),
        encoding="utf-8",
    )
    return report_path, markdown_path


def _write_fragment_reports(
    source_object: Path,
    target: Path,
    asm_map: Path,
    function: str,
    output_dir: Path,
    *,
    report_suffix: str,
    improved_suffix: str,
    write_md: bool,
    mcpu: str,
    mattr: str,
    iterations: int,
    min_source_instructions: int,
    min_target_instructions: int,
) -> tuple[Path, Path | None, Path, Path | None]:
    report_path = output_dir / (
        f"{source_object.stem}.{_safe_function_name(function)}"
        f".fragment-mca{report_suffix}.json"
    )
    report = build_fragment_report(
        str(source_object),
        str(target),
        str(asm_map),
        function,
        mcpu,
        mcpu,
        mattr,
        mattr,
        iterations,
        min_source_instructions,
        min_target_instructions,
    )
    all_json, all_markdown = _write_report_files(
        report,
        report_path,
        write_md=write_md,
    )
    improved_path = report_path.with_name(
        f"{source_object.stem}.{_safe_function_name(function)}"
        f".fragment-mca{improved_suffix}.json"
    )
    improved_report = _improved_report(report)
    improved_json, improved_markdown = _write_report_files(
        improved_report,
        improved_path,
        improved=True,
        write_md=write_md,
    )
    return all_json, all_markdown, improved_json, improved_markdown


def _write_improved_summary(
    source_stem: str,
    report_dir: Path,
    reports: list[tuple[str, Path, Path | None]],
) -> Path:
    summary_path = report_dir / f"{source_stem}.improved-summary.txt"
    entries: list[tuple[str, list[str]]] = []
    for label, report_path, _ in reports:
        if not label.endswith(" improved"):
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        fragment_ids = [
            str(fragment["id"])
            for fragment in report.get("fragments", [])
            if fragment.get("id") is not None
        ]
        if fragment_ids:
            entries.append((label, fragment_ids))

    lines = ["Improved fragments", "==================", ""]
    if not entries:
        lines.append("No improved fragments found.")
    else:
        for label, fragment_ids in entries:
            lines.append(f"{label}:")
            lines.extend(f"  - {fragment_id}" for fragment_id in fragment_ids)
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path


def run_pipeline(
    source: Path,
    output_dir: Path | None,
    *,
    arm_lifter: Path,
    machine_cfg_dump: Path | None,
    mcpu: str,
    mattr: str,
    iterations: int,
    min_source_instructions: int,
    min_target_instructions: int,
    function: str | None = None,
    write_md: bool = False,
) -> list[tuple[str, Path, Path | None]]:
    """Run lift and write ARM64 plus x86_64 fragment MCA reports."""
    source = _check_source(source)
    output_root, build_dir, report_dir = _output_dirs(output_dir)
    _require_vm_shared_path(source, "source file")
    _require_vm_shared_path(output_root, "output directory")
    arm_lifter = arm_lifter.expanduser().resolve()
    if not arm_lifter.is_file():
        raise ValueError(f"arm-lifter binary not found: {arm_lifter}")
    if machine_cfg_dump is not None:
        machine_cfg_dump = machine_cfg_dump.expanduser().resolve()
        if not machine_cfg_dump.is_file():
            raise ValueError(
                f"machine-cfg-dump binary not found: {machine_cfg_dump}"
            )
        os.environ["MACHINE_CFG_DUMP"] = str(machine_cfg_dump)

    stem = source.stem
    _clean_outputs(stem, build_dir)
    _clean_outputs(stem, report_dir)
    source_bc, source_object = compile_bc_and_o(source, build_dir)
    source_ll = build_dir / f"{stem}.ll"
    nodbg_ll = build_dir / f"{stem}.nodbg.ll"
    lifted_ll = build_dir / f"{stem}.lifted.ll"
    lift_log = build_dir / f"{stem}.lift.log"
    asm_map = build_dir / f"{stem}.asm-map.json"

    _compile_source_ir(source, source_bc, source_ll, nodbg_ll)
    _run_lifter(
        source_object,
        source_bc,
        lifted_ll,
        lift_log,
        asm_map,
        arm_lifter,
    )
    lifted_x86_64 = recompile_x86_64(lifted_ll, build_dir)
    recompile_arm64(lifted_ll, build_dir)
    compile_arm64_binary(source, build_dir)
    lifted_arm64_object = build_dir / f"{lifted_ll.stem}.arm64.o"
    _compile_arm_assembly(
        nodbg_ll,
        lifted_ll,
        build_dir / f"{stem}.nodbg.s",
        build_dir / f"{stem}.lifted.s",
    )

    _, instruction_map = parse_instruction_map(str(asm_map))
    functions = sorted(instruction_map)
    if function is not None:
        if function not in instruction_map:
            raise ValueError(
                f"function '{function}' not found in instruction map"
            )
        functions = [function]
    if not functions:
        raise ValueError(f"no functions found in instruction map: {asm_map}")

    reports: list[tuple[str, Path, Path | None]] = []
    for function_name in functions:
        arm_json, arm_md, arm_improved_json, arm_improved_md = (
            _write_fragment_reports(
                source_object,
                lifted_arm64_object,
                asm_map,
                function_name,
                report_dir,
                report_suffix="",
                improved_suffix="-arm-improved",
                mcpu=mcpu,
                mattr=mattr,
                iterations=iterations,
                min_source_instructions=min_source_instructions,
                min_target_instructions=min_target_instructions,
                write_md=write_md,
            )
        )
        x86_json, x86_md, x86_improved_json, x86_improved_md = (
            _write_fragment_reports(
                source_object,
                lifted_x86_64,
                asm_map,
                function_name,
                report_dir,
                report_suffix="-x64",
                improved_suffix="-x64-improved",
                mcpu=mcpu,
                mattr=mattr,
                iterations=iterations,
                min_source_instructions=min_source_instructions,
                min_target_instructions=min_target_instructions,
                write_md=write_md,
            )
        )
        reports.extend(
            [
                (f"{function_name} ARM64 all", arm_json, arm_md),
                (
                    f"{function_name} ARM64 improved",
                    arm_improved_json,
                    arm_improved_md,
                ),
                (f"{function_name} x86_64 all", x86_json, x86_md),
                (
                    f"{function_name} x86_64 improved",
                    x86_improved_json,
                    x86_improved_md,
                ),
            ]
        )
    _write_improved_summary(stem, report_dir, reports)
    return reports


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lift one C file and analyze provenance fragments with llvm-mca"
    )
    parser.add_argument("source", type=Path, help="input C source file")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help=(
            "output root containing build/ and report/ "
            "(default: ./output)"
        ),
    )
    parser.add_argument(
        "--arm-lifter",
        type=Path,
        default=Path(
            os.environ.get("ARM_LIFTER", str(ARM_LIFTER))
        ),
        help="arm-lifter binary path",
    )
    parser.add_argument(
        "--machine-cfg-dump",
        type=Path,
        default=(
            Path(os.environ["MACHINE_CFG_DUMP"])
            if os.environ.get("MACHINE_CFG_DUMP")
            else None
        ),
        help="machine-cfg-dump binary path",
    )
    parser.add_argument(
        "--function",
        help="analyze only this function (default: all mapped functions)",
    )
    parser.add_argument(
        "--write-md",
        action="store_true",
        help="also write Markdown reports (default: JSON only)",
    )
    parser.add_argument("--mcpu", default="generic")
    parser.add_argument("--mattr", default="")
    parser.add_argument("--mca-iterations", type=int, default=100)
    parser.add_argument("--min-source-instructions", type=int, default=2)
    parser.add_argument("--min-target-instructions", type=int, default=2)
    return parser


def _print_improved_summary(report_path: Path) -> None:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    fragments = report.get("fragments", [])
    if not fragments:
        print("  >>> IMPROVED: none (0 fragments)")
        return

    print(f"  >>> IMPROVED: {len(fragments)} fragment(s) found")
    for fragment in fragments:
        mca = fragment["mca"]
        source_cycles = mca["source"]["cycles_per_iteration"]
        target_cycles = mca["target"]["cycles_per_iteration"]
        delta = target_cycles - source_cycles
        print(
            f"      {fragment['id']}: "
            f"cycles/iteration {source_cycles:.2f} -> "
            f"{target_cycles:.2f} ({delta:+.2f})"
        )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    for name in (
        "mca_iterations",
        "min_source_instructions",
        "min_target_instructions",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")

    try:
        reports = run_pipeline(
            args.source,
            args.output_dir,
            arm_lifter=args.arm_lifter,
            machine_cfg_dump=args.machine_cfg_dump,
            mcpu=args.mcpu,
            mattr=args.mattr,
            iterations=args.mca_iterations,
            min_source_instructions=args.min_source_instructions,
            min_target_instructions=args.min_target_instructions,
            function=args.function,
            write_md=args.write_md,
        )
    except (OSError, RuntimeError, SystemExit, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    for label, report_path, markdown_path in reports:
        print(f"{label}:")
        if label.endswith(" improved"):
            _print_improved_summary(report_path)
        print(f"  JSON     → {report_path}")
        if markdown_path is not None:
            print(f"  Markdown → {markdown_path}")
    _, _, report_dir = _output_dirs(args.output_dir)
    print("Improved summary:")
    print(f"  TXT      → {report_dir / f'{args.source.stem}.improved-summary.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
