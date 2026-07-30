#!/usr/bin/env python3
"""Compare strict provenance-derived instruction fragments with llvm-mca.

The source object and target binary are correlated through arm-lifter's
instruction-map sidecar. A fragment is accepted only when every source
instruction has one contiguous, ordered target instruction run.
"""

import argparse
import json
import sys
from pathlib import Path

from asm_diff import parse_instruction_map
from cfg import (
    _mca_instruction_text,
    align_source_blocks_with_instruction_map,
    is_correlatable_source_instruction,
    load_machine_cfg,
    run_llvm_mca_regions,
)


def _flatten_blocks(blocks: list[tuple[str, list[dict]]]) -> list[dict]:
    return [
        instruction
        for _, instructions in blocks
        for instruction in instructions
    ]


def _instructions_are_contiguous(
    previous: dict,
    current: dict,
) -> bool:
    return previous["addr"] + previous["size"] == current["addr"]


def _is_untrusted_target_nop(
    instruction: dict,
    source_instruction: dict[str, object],
) -> bool:
    return (
        instruction.get("mnemonic", "").startswith("nop")
        and source_instruction.get("opcode") not in {"HINT", "NOP"}
    )


def validate_and_annotate_source(
    source_blocks: list[tuple[str, list[dict]]],
    source_instructions: dict[int, dict[str, object]],
) -> list[dict]:
    """Annotate source machine instructions and verify v1 map alignment."""
    align_source_blocks_with_instruction_map(source_blocks, source_instructions)
    source_records = [
        (arm_inst_id, instruction)
        for arm_inst_id, instruction in sorted(source_instructions.items())
        if is_correlatable_source_instruction(instruction)
    ]
    object_instructions = _flatten_blocks(source_blocks)

    for index, (object_instruction, (_, source_record)) in enumerate(
        zip(object_instructions, source_records, strict=True)
    ):
        if object_instruction["opcode_id"] != source_record["opcode_id"]:
            raise ValueError(
                "source object/map opcode mismatch at instruction "
                f"{index}: object={object_instruction['opcode_id']}, "
                f"map={source_record['opcode_id']}"
            )
    return object_instructions


def annotate_target(
    target_instructions: list[dict],
    source_instructions: dict[int, dict[str, object]],
) -> set[int]:
    """Attach arm IDs to target instructions and return unsafe NOP IDs."""
    line_to_arm_inst = {
        instruction["dwarf_line"]: arm_inst_id
        for arm_inst_id, instruction in source_instructions.items()
        if is_correlatable_source_instruction(instruction)
    }
    unsafe_nop_ids = set()
    for instruction in target_instructions:
        arm_inst_id = line_to_arm_inst.get(instruction.get("dwarf_line"))
        instruction["arm_inst_id"] = arm_inst_id
        if arm_inst_id is None:
            continue
        if _is_untrusted_target_nop(
            instruction,
            source_instructions[arm_inst_id],
        ):
            unsafe_nop_ids.add(arm_inst_id)
            instruction["arm_inst_id"] = None
    return unsafe_nop_ids


def collect_target_runs(
    target_instructions: list[dict],
) -> dict[int, list[tuple[int, int]]]:
    """Collect contiguous target runs for each arm instruction ID."""
    runs: dict[int, list[tuple[int, int]]] = {}
    run_start = None
    previous_index = None
    previous_arm_inst_id = None

    for index, instruction in enumerate(target_instructions):
        arm_inst_id = instruction.get("arm_inst_id")
        can_extend = (
            arm_inst_id is not None
            and previous_index is not None
            and arm_inst_id == previous_arm_inst_id
            and _instructions_are_contiguous(
                target_instructions[previous_index],
                instruction,
            )
        )
        if can_extend:
            previous_index = index
            continue

        if run_start is not None:
            assert previous_arm_inst_id is not None
            runs.setdefault(previous_arm_inst_id, []).append(
                (run_start, previous_index)
            )

        if arm_inst_id is None:
            run_start = None
            previous_index = None
            previous_arm_inst_id = None
        else:
            run_start = index
            previous_index = index
            previous_arm_inst_id = arm_inst_id

    if run_start is not None:
        assert previous_arm_inst_id is not None
        runs.setdefault(previous_arm_inst_id, []).append(
            (run_start, previous_index)
        )
    return runs


def _serialize_instruction(instruction: dict) -> dict:
    return {
        "address": f"0x{instruction['addr']:x}",
        "text": _mca_instruction_text(instruction),
        "arm_inst_id": instruction.get("arm_inst_id"),
        "dwarf_line": instruction.get("dwarf_line"),
    }


def _serialize_fragment(
    fragment_id: str,
    source_instructions: list[dict],
    target_instructions: list[dict],
) -> dict:
    arm_inst_ids = [
        instruction["arm_inst_id"] for instruction in source_instructions
    ]
    return {
        "id": fragment_id,
        "status": "accepted",
        "arm_inst_ids": arm_inst_ids,
        "source": {
            "address_range": [
                f"0x{source_instructions[0]['addr']:x}",
                f"0x{source_instructions[-1]['addr']:x}",
            ],
            "instructions": [
                _serialize_instruction(instruction)
                for instruction in source_instructions
            ],
        },
        "target": {
            "address_range": [
                f"0x{target_instructions[0]['addr']:x}",
                f"0x{target_instructions[-1]['addr']:x}",
            ],
            "instructions": [
                _serialize_instruction(instruction)
                for instruction in target_instructions
            ],
        },
        "_source_instructions": source_instructions,
        "_target_instructions": target_instructions,
    }


def _excluded_record(
    source_instruction: dict,
    reason: str,
) -> dict:
    return {
        "status": "excluded",
        "reason": reason,
        "arm_inst_ids": [source_instruction["arm_inst_id"]],
        "source": {
            "address_range": [
                f"0x{source_instruction['addr']:x}",
                f"0x{source_instruction['addr']:x}",
            ],
            "instructions": [_serialize_instruction(source_instruction)],
        },
    }


def _rejected_record(
    source_instruction: dict,
    reason: str,
) -> dict:
    record = _excluded_record(source_instruction, reason)
    record["status"] = "rejected"
    return record


def build_provenance_fragments(
    source_instructions: list[dict],
    target_instructions: list[dict],
    unsafe_nop_ids: set[int],
    min_source_instructions: int = 2,
    min_target_instructions: int = 2,
) -> list[dict]:
    """Build maximal strict source/target provenance fragments."""
    target_runs = collect_target_runs(target_instructions)
    records = []
    fragment_source = []
    fragment_target = []
    previous_target_end = None
    next_fragment_index = 0

    def flush_fragment() -> None:
        nonlocal fragment_source, fragment_target, previous_target_end
        nonlocal next_fragment_index
        if fragment_source:
            fragment = _serialize_fragment(
                f"fragment_{next_fragment_index}",
                fragment_source,
                fragment_target,
            )
            if (len(fragment_source) < min_source_instructions
                    or len(fragment_target) < min_target_instructions):
                fragment["status"] = "rejected"
                fragment["reason"] = "too_short"
            records.append(fragment)
            next_fragment_index += 1
        fragment_source = []
        fragment_target = []
        previous_target_end = None

    for source_index, source_instruction in enumerate(source_instructions):
        arm_inst_id = source_instruction["arm_inst_id"]

        if source_instruction.get("is_call"):
            flush_fragment()
            records.append(_excluded_record(source_instruction, "source_call"))
            continue
        if source_instruction.get("is_terminator"):
            flush_fragment()
            records.append(
                _excluded_record(source_instruction, "source_terminator")
            )
            continue
        if arm_inst_id in unsafe_nop_ids:
            flush_fragment()
            records.append(
                _rejected_record(source_instruction, "untrusted_target_nop")
            )
            continue

        runs = target_runs.get(arm_inst_id, [])
        if not runs:
            flush_fragment()
            records.append(
                _rejected_record(source_instruction, "target_id_missing")
            )
            continue
        if len(runs) != 1:
            flush_fragment()
            records.append(
                _rejected_record(source_instruction, "target_id_split")
            )
            continue

        target_start, target_end = runs[0]
        target_run = target_instructions[target_start:target_end + 1]
        if any(
            instruction.get("is_call") or instruction.get("is_terminator")
            for instruction in target_run
        ):
            flush_fragment()
            records.append(
                _rejected_record(
                    source_instruction,
                    "target_control_flow",
                )
            )
            continue

        previous_source = (
            source_instructions[source_index - 1] if source_index else None
        )
        if (fragment_source
                and previous_source is not None
                and not _instructions_are_contiguous(
                    previous_source,
                    source_instruction,
                )):
            flush_fragment()

        if previous_target_end is not None:
            if target_start <= previous_target_end:
                flush_fragment()
                records.append(
                    _rejected_record(
                        source_instruction,
                        "target_id_reordered",
                    )
                )
                continue
            if target_start != previous_target_end + 1:
                flush_fragment()
                records.append(
                    _rejected_record(
                        source_instruction,
                        "target_not_contiguous",
                    )
                )
                continue

        fragment_source.append(source_instruction)
        fragment_target.extend(target_run)
        previous_target_end = target_end

    flush_fragment()
    return records


def _compare_mca_results(source: dict, target: dict) -> dict:
    comparison = {
        "status": (
            "ok"
            if source.get("status") == target.get("status") == "ok"
            else "error"
        ),
        "source": source,
        "target": target,
    }
    if comparison["status"] != "ok":
        return comparison

    metrics = (
        "instruction_count",
        "uops_per_iteration",
        "block_rthroughput",
        "cycles_per_iteration",
    )
    comparison["delta"] = {
        metric: target[metric] - source[metric] for metric in metrics
    }
    comparison["ratio"] = {
        metric: (
            target[metric] / source[metric] if source[metric] else None
        )
        for metric in metrics
    }
    return comparison


def analyze_fragments_with_mca(
    fragments: list[dict],
    source_arch: str,
    target_arch: str,
    source_mcpu: str,
    target_mcpu: str,
    source_mattr: str,
    target_mattr: str,
    iterations: int,
) -> dict:
    """Attach per-side MCA results and same-configuration comparisons."""
    accepted = [
        fragment for fragment in fragments
        if fragment["status"] == "accepted"
    ]
    source_regions = {
        fragment["id"]: fragment["_source_instructions"]
        for fragment in accepted
    }
    target_regions = {
        fragment["id"]: fragment["_target_instructions"]
        for fragment in accepted
    }
    source_results = run_llvm_mca_regions(
        source_regions,
        source_arch,
        source_mcpu,
        iterations,
        source_mattr,
    )
    target_results = run_llvm_mca_regions(
        target_regions,
        target_arch,
        target_mcpu,
        iterations,
        target_mattr,
    )

    comparable = (
        source_arch == target_arch
        and source_mcpu == target_mcpu
        and source_mattr == target_mattr
    )
    for fragment in accepted:
        source_result = source_results[fragment["id"]]
        target_result = target_results[fragment["id"]]
        if comparable:
            fragment["mca"] = _compare_mca_results(
                source_result,
                target_result,
            )
        else:
            fragment["mca"] = {
                "status": (
                    "cross_isa"
                    if source_arch != target_arch
                    else "incomparable_config"
                ),
                "source": source_result,
                "target": target_result,
            }

    return {
        "source_arch": source_arch,
        "target_arch": target_arch,
        "source_mcpu": source_mcpu,
        "target_mcpu": target_mcpu,
        "source_mattr": source_mattr,
        "target_mattr": target_mattr,
        "iterations": iterations,
        "comparison_status": (
            "comparable"
            if comparable
            else (
                "cross_isa"
                if source_arch != target_arch
                else "incomparable_config"
            )
        ),
    }


def build_fragment_report(
    source_path: str,
    target_path: str,
    asm_map_path: str,
    function: str,
    source_mcpu: str,
    target_mcpu: str,
    source_mattr: str,
    target_mattr: str,
    iterations: int,
    min_source_instructions: int = 2,
    min_target_instructions: int = 2,
) -> dict:
    """Load one function and return a provenance-fragment MCA report."""
    debug_file, instruction_map = parse_instruction_map(asm_map_path)
    if function not in instruction_map:
        raise ValueError(
            f"function '{function}' not found in instruction map"
        )

    source_arch, source_functions = load_machine_cfg(
        source_path,
        function_filter=function,
    )
    target_arch, target_functions = load_machine_cfg(
        target_path,
        debug_file,
        function,
    )
    if function not in source_functions:
        raise ValueError(f"function '{function}' not found in source object")
    if function not in target_functions:
        raise ValueError(f"function '{function}' not found in target binary")

    source_graph = source_functions[function]
    target_graph = target_functions[function]
    source_instructions = validate_and_annotate_source(
        source_graph["blocks"],
        instruction_map[function],
    )
    target_instructions = target_graph["instructions"]
    unsafe_nop_ids = annotate_target(
        target_instructions,
        instruction_map[function],
    )
    fragments = build_provenance_fragments(
        source_instructions,
        target_instructions,
        unsafe_nop_ids,
        min_source_instructions,
        min_target_instructions,
    )
    mca = analyze_fragments_with_mca(
        fragments,
        source_arch,
        target_arch,
        source_mcpu,
        target_mcpu,
        source_mattr,
        target_mattr,
        iterations,
    )
    return {
        "version": 1,
        "function": function,
        "source": {
            "path": str(Path(source_path).resolve()),
            "arch": source_arch,
        },
        "target": {
            "path": str(Path(target_path).resolve()),
            "arch": target_arch,
        },
        "policy": {
            "strict_continuity": True,
            "calls": "boundary",
            "terminators": "boundary",
            "untrusted_target_nops": "reject",
            "min_source_instructions": min_source_instructions,
            "min_target_instructions": min_target_instructions,
        },
        "mca": mca,
        "fragments": fragments,
    }


def _json_report(report: dict) -> dict:
    return {
        **report,
        "fragments": [
            {
                key: value
                for key, value in fragment.items()
                if not key.startswith("_")
            }
            for fragment in report["fragments"]
        ],
    }


def _format_number(value: object, *, signed: bool = False) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return f"{value:+d}" if signed else str(value)
    if isinstance(value, float):
        return f"{value:+.2f}" if signed else f"{value:.2f}"
    return str(value)


def _format_arm_ids(arm_inst_ids: list[int]) -> str:
    if not arm_inst_ids:
        return "-"
    ranges = []
    start = arm_inst_ids[0]
    previous = start
    for arm_inst_id in arm_inst_ids[1:]:
        if arm_inst_id == previous + 1:
            previous = arm_inst_id
            continue
        ranges.append(
            f"#{start}" if start == previous else f"#{start}-#{previous}"
        )
        start = arm_inst_id
        previous = arm_inst_id
    ranges.append(f"#{start}" if start == previous else f"#{start}-#{previous}")
    return ", ".join(ranges)


def _markdown_instruction_block(fragment: dict, side: str) -> list[str]:
    return [
        f"{instruction['address']}: {instruction['text']}"
        for instruction in fragment[side]["instructions"]
    ]


def _markdown_mca_table(mca: dict) -> list[str]:
    source = mca["source"]
    target = mca["target"]
    lines = [
        "| Metric | Source | Target | Delta | Ratio |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for metric, label in (
        ("instruction_count", "Instructions"),
        ("uops_per_iteration", "uOps / iteration"),
        ("block_rthroughput", "Block throughput"),
        ("cycles_per_iteration", "Cycles / iteration"),
    ):
        delta = mca.get("delta", {}).get(metric)
        ratio = mca.get("ratio", {}).get(metric)
        lines.append(
            f"| {label} | {_format_number(source.get(metric))} | "
            f"{_format_number(target.get(metric))} | "
            f"{_format_number(delta, signed=True)} | "
            f"{_format_number(ratio)} |"
        )
    return lines


def render_markdown_report(report: dict) -> str:
    """Render a concise human-readable view of the JSON report."""
    fragments = report["fragments"]
    accepted = [
        fragment for fragment in fragments
        if fragment["status"] == "accepted"
    ]
    rejected = [
        fragment for fragment in fragments
        if fragment["status"] == "rejected"
    ]
    excluded = [
        fragment for fragment in fragments
        if fragment["status"] == "excluded"
    ]
    mca = report["mca"]
    lines = [
        f"# Provenance Fragment MCA: `{report['function']}`",
        "",
        f"- Source: `{report['source']['path']}` ({report['source']['arch']})",
        f"- Target: `{report['target']['path']}` ({report['target']['arch']})",
        (
            "- MCA: "
            f"source `{mca['source_mcpu']}` / `{mca['source_mattr'] or '-'}`, "
            f"target `{mca['target_mcpu']}` / `{mca['target_mattr'] or '-'}`, "
            f"{mca['iterations']} iterations"
        ),
        f"- Comparison: `{mca['comparison_status']}`",
        "",
        "## Summary",
        "",
        "| Accepted | Rejected | Excluded |",
        "| ---: | ---: | ---: |",
        f"| {len(accepted)} | {len(rejected)} | {len(excluded)} |",
    ]

    if accepted:
        lines.extend([
            "",
            "## Accepted Fragments",
            "",
            (
                "| Fragment | ARM IDs | Source instructions | "
                "Target instructions | Throughput |"
            ),
            "| --- | --- | ---: | ---: | --- |",
        ])
        for fragment in accepted:
            source = fragment["mca"]["source"]
            target = fragment["mca"]["target"]
            lines.append(
                f"| [{fragment['id']}](#{fragment['id']}) | "
                f"{_format_arm_ids(fragment['arm_inst_ids'])} | "
                f"{len(fragment['source']['instructions'])} | "
                f"{len(fragment['target']['instructions'])} | "
                f"{_format_number(source.get('block_rthroughput'))} -> "
                f"{_format_number(target.get('block_rthroughput'))} |"
            )

        for fragment in accepted:
            lines.extend([
                "",
                f"### {fragment['id']}",
                "",
                (
                    f"ARM IDs: {_format_arm_ids(fragment['arm_inst_ids'])}  "
                    f"  Source: `{fragment['source']['address_range'][0]}` "
                    f"to `{fragment['source']['address_range'][1]}`  "
                    f"  Target: `{fragment['target']['address_range'][0]}` "
                    f"to `{fragment['target']['address_range'][1]}`"
                ),
                "",
            ])
            if fragment["mca"]["status"] != "ok":
                lines.extend([
                    (
                        "> Direct delta is unavailable because the ISA or "
                        "MCA configuration differs."
                    ),
                    "",
                ])
            lines.extend(_markdown_mca_table(fragment["mca"]))
            lines.extend([
                "",
                "#### Source",
                "",
                "```asm",
                *_markdown_instruction_block(fragment, "source"),
                "```",
                "",
                "#### Target",
                "",
                "```asm",
                *_markdown_instruction_block(fragment, "target"),
                "```",
            ])

    if rejected or excluded:
        lines.extend([
            "",
            "## Rejected And Excluded",
            "",
            "| Status | Reason | ARM IDs | Source address |",
            "| --- | --- | --- | --- |",
        ])
        for fragment in [*rejected, *excluded]:
            source = fragment["source"]
            lines.append(
                f"| {fragment['status']} | `{fragment['reason']}` | "
                f"{_format_arm_ids(fragment['arm_inst_ids'])} | "
                f"`{source['address_range'][0]}` |"
            )
    return "\n".join(lines) + "\n"


def print_summary(report: dict) -> None:
    fragments = report["fragments"]
    accepted = [
        fragment for fragment in fragments
        if fragment["status"] == "accepted"
    ]
    rejected = [
        fragment for fragment in fragments
        if fragment["status"] == "rejected"
    ]
    excluded = [
        fragment for fragment in fragments
        if fragment["status"] == "excluded"
    ]
    print(f"=== Provenance fragment MCA: {report['function']} ===")
    print(
        f"Source: {report['source']['arch']}  "
        f"Target: {report['target']['arch']}"
    )
    print(
        f"Accepted: {len(accepted)}  Rejected: {len(rejected)}  "
        f"Excluded: {len(excluded)}"
    )
    print(f"MCA comparison: {report['mca']['comparison_status']}")

    for fragment in accepted:
        mca = fragment["mca"]
        source = mca["source"]
        target = mca["target"]
        print(
            f"\n{fragment['id']} "
            f"source={fragment['source']['address_range']} "
            f"target={fragment['target']['address_range']}"
        )
        print(
            f"  instructions: {source.get('instruction_count')} -> "
            f"{target.get('instruction_count')}"
        )
        print(
            f"  throughput:   {source.get('block_rthroughput')} -> "
            f"{target.get('block_rthroughput')}"
        )
        if mca["status"] == "ok":
            print(
                f"  delta:        "
                f"{mca['delta']['block_rthroughput']:+.2f}"
            )


def _default_report_path(target_path: str) -> Path:
    target = Path(target_path)
    return target.with_name(f"{target.name}.fragment-mca.json")


def _default_markdown_path(report_path: Path) -> Path:
    return report_path.with_suffix(".md")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze strict provenance fragments with llvm-mca",
    )
    parser.add_argument("source_object", help="Original source object file")
    parser.add_argument(
        "target",
        help="Lifted-IR recompiled object file or executable",
    )
    parser.add_argument(
        "--asm-map",
        required=True,
        help="Instruction map JSON emitted by arm-lifter",
    )
    parser.add_argument(
        "-f",
        "--function",
        required=True,
        help="Exact function name to analyze",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="JSON report path (default: <target>.fragment-mca.json)",
    )
    parser.add_argument(
        "--markdown-report",
        type=Path,
        help="Markdown report path (default: JSON report path with .md)",
    )
    parser.add_argument(
        "--no-markdown",
        action="store_true",
        help="Do not write the default human-readable Markdown report",
    )
    parser.add_argument("--source-mcpu", default="generic")
    parser.add_argument("--target-mcpu", default="generic")
    parser.add_argument("--source-mattr", default="")
    parser.add_argument("--target-mattr", default="")
    parser.add_argument("--mca-iterations", type=int, default=100)
    parser.add_argument("--min-source-instructions", type=int, default=2)
    parser.add_argument("--min-target-instructions", type=int, default=2)
    args = parser.parse_args()

    if args.mca_iterations <= 0:
        parser.error("--mca-iterations must be positive")
    if args.min_source_instructions <= 0:
        parser.error("--min-source-instructions must be positive")
    if args.min_target_instructions <= 0:
        parser.error("--min-target-instructions must be positive")
    for path in (args.source_object, args.target, args.asm_map):
        if not Path(path).is_file():
            parser.error(f"file not found: {path}")

    try:
        report = build_fragment_report(
            args.source_object,
            args.target,
            args.asm_map,
            args.function,
            args.source_mcpu,
            args.target_mcpu,
            args.source_mattr,
            args.target_mattr,
            args.mca_iterations,
            args.min_source_instructions,
            args.min_target_instructions,
        )
    except (RuntimeError, ValueError) as error:
        sys.exit(f"Error: {error}")

    report_path = args.report or _default_report_path(args.target)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(_json_report(report), indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path = None
    if not args.no_markdown:
        markdown_path = args.markdown_report or _default_markdown_path(
            report_path
        )
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(
            render_markdown_report(report),
            encoding="utf-8",
        )
    print_summary(report)
    print(f"\nReport: {report_path}")
    if markdown_path is not None:
        print(f"Markdown: {markdown_path}")


if __name__ == "__main__":
    main()
