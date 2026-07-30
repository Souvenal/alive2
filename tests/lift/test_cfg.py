import json
import subprocess
from pathlib import Path

import pytest

from asm_diff import parse_instruction_map
from dev import cmd_viewer
import cfg
from cfg import (
    align_source_blocks_with_instruction_map,
    build_address_block_relations,
    build_block_similarity_records,
    build_block_relations,
    build_combined_cfg_dot,
    build_combined_cfg_models,
    build_cfg,
    build_missing_function_report,
    build_relation_components,
    dump_block_matches,
    extract_target_addr,
    parse_objdump_output,
    render_interactive_viewer,
    run_llvm_mca_for_block,
    select_one_to_one_block_matches,
)
from toolchain import ToolchainError, find_host_llvm_tool


def test_parse_instruction_map_requires_opcode(
    tmp_path,
) -> None:
    map_path = tmp_path / "bad-map.json"
    map_path.write_text(
        json.dumps(
            {
                "version": 1,
                "debug_file": "arm_asm.s",
                "functions": [
                    {
                        "name": "foo",
                        "instructions": [
                            {
                                "arm_inst_id": 0,
                                "dwarf_line": 1,
                                "mc_block": "foo",
                                "asm": "ret",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="Malformed instruction record"):
        parse_instruction_map(str(map_path))


def test_parse_objdump_output_tracks_synthetic_debug_lines() -> None:
    output = """
sample: file format elf64-x86-64
0000000000000010 <foo>:
; foo():
  10: movl $0x1, %eax
; ./arm_asm.s:3
  15: addl $0x2, %eax
  18: jne 0x20 <foo+0x10>
; source.c:9
  1a: retq
"""

    arch, instructions = parse_objdump_output(output, "arm_asm.s")

    assert arch == "x86-64"
    assert [i["dwarf_line"] for i in instructions] == [None, 3, 3, None]


def test_parse_objdump_output_matches_normalized_debug_path() -> None:
    output = """
sample: file format elf64-x86-64
0000000000000010 <foo>:
; ./synthetic/arm_asm.s:3
  10: addl $0x2, %eax
"""

    _, instructions = parse_objdump_output(
        output, "synthetic/arm_asm.s"
    )

    assert instructions[0]["dwarf_line"] == 3


def test_parse_objdump_output_rejects_same_basename_at_other_path() -> None:
    output = """
sample: file format elf64-x86-64
0000000000000010 <foo>:
; /tmp/arm_asm.s:3
  10: addl $0x2, %eax
"""

    _, instructions = parse_objdump_output(output, "arm_asm.s")

    assert instructions[0]["dwarf_line"] is None


def test_build_cfg_preserves_structured_instructions() -> None:
    instructions = [
        {
            "func": "foo",
            "addr": 0x10,
            "raw": "10: cmp $0x0, %eax",
            "mnemonic": "cmp",
            "operands": "$0x0, %eax",
            "dwarf_line": 3,
        },
        {
            "func": "foo",
            "addr": 0x12,
            "raw": "12: jne 0x18 <foo+0x8>",
            "mnemonic": "jne",
            "operands": "0x18 <foo+0x8>",
            "dwarf_line": 3,
        },
        {
            "func": "foo",
            "addr": 0x14,
            "raw": "14: retq",
            "mnemonic": "retq",
            "operands": "",
            "dwarf_line": 4,
        },
        {
            "func": "foo",
            "addr": 0x18,
            "raw": "18: retq",
            "mnemonic": "retq",
            "operands": "",
            "dwarf_line": 5,
        },
    ]

    blocks, edges = build_cfg(instructions, "x86-64")

    assert [name for name, _ in blocks] == ["L10", "L14", "L18"]
    assert blocks[0][1][0]["dwarf_line"] == 3
    assert ("L10", "L18", "T") in edges
    assert ("L10", "L14", "F") in edges


def test_build_cfg_splits_dead_code_after_unconditional_branch() -> None:
    instructions = [
        {
            "func": "foo",
            "addr": 0x10,
            "raw": "10: jmp 0x18 <foo+0x8>",
            "mnemonic": "jmp",
            "operands": "0x18 <foo+0x8>",
            "dwarf_line": 3,
        },
        {
            "func": "foo",
            "addr": 0x12,
            "raw": "12: nop",
            "mnemonic": "nop",
            "operands": "",
            "dwarf_line": 3,
        },
        {
            "func": "foo",
            "addr": 0x18,
            "raw": "18: retq",
            "mnemonic": "retq",
            "operands": "",
            "dwarf_line": 4,
        },
    ]

    blocks, edges = build_cfg(instructions, "x86-64")

    assert [name for name, _ in blocks] == ["L10", "L12", "L18"]
    assert edges == [
        ("L10", "L18", "J"),
        ("L12", "L18", ""),
    ]


def test_extract_target_addr_rejects_indirect_branches() -> None:
    x86 = {
        "mnemonic": "jmpq",
        "operands": "*0x20(%rax)",
    }
    arm = {
        "mnemonic": "br",
        "operands": "x8",
    }

    assert extract_target_addr(x86, "x86-64") is None
    assert extract_target_addr(arm, "aarch64") is None


def test_build_block_relations_is_many_to_many_and_unweighted() -> None:
    source_instructions = {
        0: {
            "arm_inst_id": 0,
            "dwarf_line": 1,
            "mc_block": "entry",
            "opcode": "B",
            "asm": "b #0x4",
        },
        1: {
            "arm_inst_id": 1,
            "dwarf_line": 2,
            "mc_block": "A",
            "opcode": "SEH_Nop",
            "asm": "",
        },
        2: {
            "arm_inst_id": 2,
            "dwarf_line": 3,
            "mc_block": "A",
            "opcode": "ADDWri",
            "asm": "add w0, w0, #1",
        },
        3: {
            "arm_inst_id": 3,
            "dwarf_line": 4,
            "mc_block": "A",
            "opcode": "Bcc",
            "asm": "b.eq .L1",
        },
        4: {
            "arm_inst_id": 4,
            "dwarf_line": 5,
            "mc_block": "B",
            "opcode": "RET",
            "asm": "ret",
        },
        5: {
            "arm_inst_id": 5,
            "dwarf_line": 6,
            "mc_block": "B",
            "opcode": "SUBWri",
            "asm": "sub w0, w0, #1",
        },
    }
    blocks = [
        (
            "L10",
            [
                {"dwarf_line": 3},
                {"dwarf_line": 3},
                {"dwarf_line": 4},
            ],
        ),
        (
            "L20",
            [
                {"dwarf_line": 4},
                {"dwarf_line": 5},
            ],
        ),
        (
            "L30",
            [
                {"dwarf_line": 5, "mnemonic": "nopw"},
                {"dwarf_line": None, "mnemonic": "retq"},
            ],
        ),
    ]

    report = build_block_relations(blocks, source_instructions)

    assert report["relations"] == [
        {
            "source_block": "A",
            "target_block": "L10",
            "arm_inst_ids": [2, 3],
        },
        {
            "source_block": "A",
            "target_block": "L20",
            "arm_inst_ids": [3],
        },
        {
            "source_block": "B",
            "target_block": "L20",
            "arm_inst_ids": [4],
        },
    ]
    assert report["unmatched_source_blocks"] == []
    assert report["unmatched_arm_inst_ids"] == [5]
    assert report["unmatched_target_blocks"] == ["L30"]


def test_build_block_relations_keeps_real_nop_but_drops_padding() -> None:
    source_instructions = {
        1: {
            "arm_inst_id": 1,
            "dwarf_line": 2,
            "mc_block": "A",
            "opcode": "HINT",
            "asm": "hint #0",
        },
        2: {
            "arm_inst_id": 2,
            "dwarf_line": 3,
            "mc_block": "A",
            "opcode": "RET",
            "asm": "ret",
        },
    }
    blocks = [
        ("L10", [{"dwarf_line": 2, "mnemonic": "nop"}]),
        ("L20", [{"dwarf_line": 3, "mnemonic": "nopw"}]),
    ]

    report = build_block_relations(blocks, source_instructions)

    assert report["relations"] == [
        {
            "source_block": "A",
            "target_block": "L10",
            "arm_inst_ids": [1],
        }
    ]
    assert report["unmatched_arm_inst_ids"] == [2]
    assert report["unmatched_target_blocks"] == ["L20"]


def test_build_missing_function_report_is_explicit() -> None:
    source_instructions = {
        2: {
            "arm_inst_id": 2,
            "dwarf_line": 3,
            "mc_block": "A",
            "opcode": "ADDWri",
            "asm": "add w0, w0, #1",
        }
    }

    report = build_missing_function_report(
        "missing", "x86-64", source_instructions
    )

    assert report["target_function_missing"] is True
    assert report["unmatched_source_blocks"] == ["A"]
    assert report["unmatched_arm_inst_ids"] == [2]


def test_align_source_blocks_with_instruction_map() -> None:
    source_blocks = [
        ("L10", [{"raw": "10: add"}, {"raw": "14: b"}]),
        ("L20", [{"raw": "20: ret"}]),
    ]
    source_instructions = {
        0: {
            "arm_inst_id": 0,
            "dwarf_line": 1,
            "mc_block": "entry",
            "opcode": "B",
            "asm": "b #4",
        },
        1: {
            "arm_inst_id": 1,
            "dwarf_line": 2,
            "mc_block": "A",
            "opcode": "ADDWri",
            "asm": "add w0, w0, #1",
        },
        2: {
            "arm_inst_id": 2,
            "dwarf_line": 3,
            "mc_block": "A",
            "opcode": "Bcc",
            "asm": "b.eq .L1",
        },
        3: {
            "arm_inst_id": 3,
            "dwarf_line": 4,
            "mc_block": "B",
            "opcode": "RET",
            "asm": "ret",
        },
    }

    aligned = align_source_blocks_with_instruction_map(
        source_blocks, source_instructions
    )

    assert aligned == {1: "L10", 2: "L10", 3: "L20"}
    assert source_blocks[0][1][0]["arm_inst_id"] == 1
    assert source_blocks[1][1][0]["mc_block"] == "B"


def test_relation_components_group_many_to_many_blocks() -> None:
    report = {
        "relations": [
            {
                "source_block": "A",
                "target_block": "T1",
                "arm_inst_ids": [1],
            },
            {
                "source_block": "A",
                "target_block": "T2",
                "arm_inst_ids": [2],
            },
            {
                "source_block": "B",
                "target_block": "T2",
                "arm_inst_ids": [3],
            },
        ]
    }
    arm_to_source = {1: "S1", 2: "S1", 3: "S2"}

    address_relations = build_address_block_relations(
        report, arm_to_source
    )
    components = build_relation_components(address_relations)

    assert components == [
        {
            "source_blocks": ["S1", "S2"],
            "target_blocks": ["T1", "T2"],
            "arm_inst_ids": [1, 2, 3],
        }
    ]


def test_block_similarity_uses_coverage_purity_and_collapsed_order() -> None:
    source_blocks = [
        (
            "S1",
            [
                {"arm_inst_id": 1, "mnemonic": "add"},
                {"arm_inst_id": 2, "mnemonic": "cmp"},
                {"arm_inst_id": 3, "mnemonic": "b.eq"},
            ],
        ),
        ("S2", [{"arm_inst_id": 4, "mnemonic": "ret"}]),
    ]
    target_blocks = [
        (
            "T1",
            [
                {"arm_inst_id": 1, "mnemonic": "add"},
                {"arm_inst_id": 1, "mnemonic": "mov"},
                {"arm_inst_id": 2, "mnemonic": "cmp"},
                {"arm_inst_id": 3, "mnemonic": "je"},
            ],
        ),
        (
            "T2",
            [
                {"arm_inst_id": 2, "mnemonic": "mov"},
                {"arm_inst_id": 4, "mnemonic": "ret"},
            ],
        ),
    ]

    records = build_block_similarity_records(
        source_blocks,
        target_blocks,
        "aarch64",
    )
    by_pair = {
        (record["source_block"], record["target_block"]): record
        for record in records
    }

    exact = by_pair[("S1", "T1")]
    assert exact["source_coverage"] == pytest.approx(1.0)
    assert exact["target_purity"] == pytest.approx(1.0)
    assert exact["order_similarity"] == pytest.approx(1.0)
    assert exact["score"] == pytest.approx(1.0)
    assert exact["arm_inst_ids"] == [1, 2, 3]

    contaminated = by_pair[("S1", "T2")]
    assert contaminated["source_coverage"] < exact["source_coverage"]
    assert contaminated["target_purity"] < exact["target_purity"]
    assert contaminated["score"] < exact["score"]


def test_one_to_one_matching_accepts_only_unambiguous_mutual_best() -> None:
    records = [
        {
            "source_block": "S1",
            "target_block": "T1",
            "score": 0.95,
            "matched_weight": 2.0,
        },
        {
            "source_block": "S1",
            "target_block": "T2",
            "score": 0.40,
            "matched_weight": 1.0,
        },
        {
            "source_block": "S2",
            "target_block": "T1",
            "score": 0.30,
            "matched_weight": 1.0,
        },
        {
            "source_block": "S2",
            "target_block": "T2",
            "score": 0.90,
            "matched_weight": 2.0,
        },
    ]

    result = select_one_to_one_block_matches(
        records,
        ["S1", "S2"],
        ["T1", "T2"],
    )

    assert [
        (match["source_block"], match["target_block"])
        for match in result["matches"]
    ] == [("S1", "T1"), ("S2", "T2")]
    assert result["unmatched_source_blocks"] == []
    assert result["unmatched_target_blocks"] == []


def test_one_to_one_matching_rejects_ambiguous_candidates() -> None:
    records = [
        {
            "source_block": "S1",
            "target_block": "T1",
            "score": 0.90,
            "matched_weight": 2.0,
        },
        {
            "source_block": "S1",
            "target_block": "T2",
            "score": 0.85,
            "matched_weight": 2.0,
        },
    ]

    result = select_one_to_one_block_matches(
        records,
        ["S1"],
        ["T1", "T2"],
    )

    assert result["matches"] == []
    assert result["unmatched_source_blocks"] == ["S1"]
    assert result["unmatched_target_blocks"] == ["T1", "T2"]
    assert result["ambiguous_source_blocks"] == ["S1"]


def test_run_llvm_mca_for_block_parses_json(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(cfg, "require_tool", lambda name: "/llvm-mca")

    def run(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return cfg.subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({
                "TargetInfo": {
                    "Resources": ["ALU.\u0000"],
                },
                "CodeRegions": [{
                    "Name": "block",
                    "Instructions": ["add w0, w0, #1", "ret"],
                    "ResourcePressureView": {
                        "ResourcePressureInfo": [{
                            "InstructionIndex": 2,
                            "ResourceIndex": 0,
                            "ResourceUsage": 1.5,
                        }],
                    },
                    "SummaryView": {
                        "BlockRThroughput": 1.5,
                        "DispatchWidth": 4,
                        "IPC": 2.0,
                        "Instructions": 200,
                        "Iterations": 100,
                        "TotalCycles": 100,
                        "TotaluOps": 300,
                        "uOpsPerCycle": 3.0,
                    },
                }],
            }),
            stderr="",
        )

    monkeypatch.setattr(cfg.subprocess, "run", run)

    result = run_llvm_mca_for_block(
        [
            {"mnemonic": "add", "operands": "w0, w0, #1"},
            {"mnemonic": "b.ne", "operands": "0x8 <foo+0x8>"},
        ],
        "aarch64",
        "cortex-x2",
        100,
    )

    assert result["status"] == "ok"
    assert result["instruction_count"] == 2
    assert result["uops_per_iteration"] == pytest.approx(3.0)
    assert result["block_rthroughput"] == pytest.approx(1.5)
    assert result["resource_pressure"] == {"ALU.0": 1.5}
    assert calls["command"] == [
        "/llvm-mca",
        "-mtriple=aarch64-linux-gnu",
        "-mcpu=cortex-x2",
        "--iterations=100",
        "--json",
        "-",
    ]
    assert calls["kwargs"]["input"] == (
        ".text\n"
        "# LLVM-MCA-BEGIN block\n"
        "add w0, w0, #1\n"
        "b.ne 0x8\n"
        "# LLVM-MCA-END block\n"
    )


def test_mca_region_policy_excludes_only_terminator() -> None:
    instructions = [
        {"mnemonic": "add", "is_terminator": False},
        {"mnemonic": "b.ne", "is_terminator": True},
    ]

    assert cfg._mca_region_instructions(instructions, "body") == [
        instructions[0]
    ]
    assert cfg._mca_region_instructions(instructions, "full") == instructions


def test_analyze_block_matches_batches_body_and_full_regions(
    monkeypatch,
) -> None:
    instruction = {
        "mnemonic": "add",
        "operands": "w0, w0, #1",
        "is_terminator": False,
        "is_call": False,
    }
    terminator = {
        "mnemonic": "ret",
        "operands": "",
        "is_terminator": True,
        "is_call": False,
    }
    model = {
        "source": {"arch": "aarch64"},
        "target": {"arch": "aarch64"},
        "_source_blocks": [("S1", [instruction, terminator])],
        "_target_blocks": [("T1", [instruction, terminator])],
        "report": {
            "block_matching": {
                "matches": [{
                    "source_block": "S1",
                    "target_block": "T1",
                }],
            },
        },
    }
    calls = []

    def run(regions, arch, mcpu, iterations, mattr=""):
        calls.append(regions)
        return {
            name: {
                "status": "ok",
                "instruction_count": len(region),
                "uops_per_iteration": float(len(region)),
                "block_rthroughput": float(len(region)),
                "cycles_per_iteration": float(len(region)),
            }
            for name, region in regions.items()
        }

    monkeypatch.setattr(cfg, "run_llvm_mca_regions", run)

    cfg.analyze_block_matches_with_mca(model, "generic", 100)

    assert len(calls) == 2
    assert [len(region) for region in calls[0].values()] == [1, 2]
    match = model["report"]["block_matching"]["matches"][0]
    assert match["mca"]["body"]["source"]["instruction_count"] == 1
    assert match["mca"]["full"]["source"]["instruction_count"] == 2


def test_dump_block_matches_reports_insufficient_evidence(capsys) -> None:
    model = {
        "function": "foo",
        "source": {
            "arch": "aarch64",
            "blocks": [{"id": "S1", "instructions": []}],
        },
        "target": {
            "arch": "aarch64",
            "blocks": [{"id": "T1", "instructions": []}],
        },
        "report": {
            "block_matching": {
                "matches": [],
                "unmatched_source_blocks": ["S1"],
                "unmatched_target_blocks": ["T1"],
                "ambiguous_source_blocks": [],
                "ambiguous_target_blocks": [],
                "similarities": [{
                    "source_block": "S1",
                    "target_block": "T1",
                    "score": 1.0,
                    "matched_weight": 0.25,
                }],
                "thresholds": {
                    "min_score": 0.75,
                    "min_margin": 0.15,
                    "min_matched_weight": 1.0,
                },
            },
        },
    }

    dump_block_matches(model)

    output = capsys.readouterr().out
    assert output.count("insufficient evidence") == 2


def test_combined_cfg_dot_frames_relation_components() -> None:
    source_blocks = [
        (
            "S1",
            [{
                "raw": "10: add",
                "arm_inst_id": 1,
            }],
        )
    ]
    target_blocks = [("T1", [{"raw": "20: add"}])]
    components = [{
        "source_blocks": ["S1"],
        "target_blocks": ["T1"],
        "arm_inst_ids": [1],
    }]

    dot = build_combined_cfg_dot(
        "foo",
        "aarch64",
        source_blocks,
        [],
        "x86-64",
        target_blocks,
        [],
        components,
    )

    assert "cluster_source_group_0" in dot
    assert "cluster_target_group_0" in dot
    assert "dir=both" in dot
    assert "ltail=cluster_source_group_0" in dot
    assert "lhead=cluster_target_group_0" in dot


def test_render_interactive_viewer_embeds_cfg_data(tmp_path) -> None:
    model = {
        "function": "foo",
        "source": {
            "arch": "aarch64",
            "blocks": [],
            "edges": [],
        },
        "target": {
            "arch": "x86-64",
            "blocks": [],
            "edges": [],
        },
        "report": {
            "components": [],
            "address_relations": [],
            "unmatched_source_blocks": [],
            "unmatched_target_blocks": [],
            "unmatched_arm_inst_ids": [],
        },
    }

    path = render_interactive_viewer(
        model, str(tmp_path), "source.o", "lifted_x86_64"
    )
    document = Path(path).read_text(encoding="utf-8")

    assert "const MODELS =" in document
    assert "Source group" in document
    assert "Group-level CFG" in document
    assert "Unmatched source" in document
    assert "DWARF provenance evidence" in document
    assert "Relation pairs for" in document
    assert "ARM IDs" in document
    assert "function-select" in document
    assert "Select a block to inspect instructions" in document


def test_build_combined_cfg_models_defaults_to_shared_functions(monkeypatch) -> None:
    function_sets = iter([
        {"source_only": {}, "foo": {}, "bar": {}},
        {"target_only": {}, "foo": {}, "bar": {}},
    ])
    monkeypatch.setattr(
        cfg,
        "load_machine_cfg",
        lambda path, debug_file=None, function_filter=None: (
            "arch",
            next(function_sets),
        ),
    )
    built = []
    monkeypatch.setattr(
        cfg,
        "_build_combined_cfg_model",
        lambda *args: built.append(args[-1]) or {"function": args[-1]},
    )

    models = build_combined_cfg_models(
        "source.o",
        "target",
        {"foo": {}, "bar": {}, "map_only": {}},
        "arm_asm.s",
    )

    assert list(models) == ["bar", "foo"]
    assert built == ["bar", "foo"]


def test_load_machine_cfg_builds_blocks_edges_and_debug_lines(
    monkeypatch,
) -> None:
    bundle = {
        "version": 1,
        "binary": {"architecture": "x86_64"},
        "functions": [{
            "name": "foo",
            "section": ".text",
            "address": 0x10,
            "size": 4,
            "errors": [],
            "instructions": [
                {
                    "address": 0x10,
                    "size": 2,
                    "bytes": "90",
                    "opcode": "NOOP",
                    "opcode_id": 1,
                    "assembly": "nop",
                    "mnemonic": "nop",
                    "operands": "",
                    "flow_kind": "none",
                    "is_terminator": False,
                    "is_call": False,
                    "direct_target": None,
                    "relocation_symbol": None,
                },
                {
                    "address": 0x12,
                    "size": 2,
                    "bytes": "c3",
                    "opcode": "RET64",
                    "opcode_id": 2,
                    "assembly": "retq",
                    "mnemonic": "retq",
                    "operands": "",
                    "flow_kind": "return",
                    "is_terminator": True,
                    "is_call": False,
                    "direct_target": None,
                    "relocation_symbol": None,
                },
            ],
            "blocks": [{
                "id": "L10",
                "instruction_indices": [0, 1],
            }],
            "edges": [{
                "source": "L10",
                "target": None,
                "kind": "return",
                "resolution": "terminal",
                "target_address": None,
                "target_symbol": None,
            }],
        }],
    }
    monkeypatch.setattr(cfg, "require_machine_cfg_dump", lambda: "/cfg-dump")
    monkeypatch.setattr(
        cfg.subprocess,
        "run",
        lambda *args, **kwargs: cfg.subprocess.CompletedProcess(
            args[0], 0, stdout=json.dumps(bundle), stderr=""
        ),
    )
    monkeypatch.setattr(
        cfg,
        "objdump",
        lambda path, debug_file=None: (
            "x86-64",
            [{
                "func": "foo",
                "addr": 0x10,
                "dwarf_line": 7,
            }],
        ),
    )

    arch, functions = cfg.load_machine_cfg("foo.o", "arm_asm.s")

    assert arch == "x86-64"
    block_name, instructions = functions["foo"]["blocks"][0]
    assert block_name == "L10"
    assert [instruction["addr"] for instruction in instructions] == [
        0x10,
        0x12,
    ]
    assert instructions[0]["dwarf_line"] == 7
    assert instructions[1]["dwarf_line"] is None
    assert functions["foo"]["edges"] == []
    assert functions["foo"]["cfg_edges"][0]["kind"] == "return"


def test_machine_cfg_dump_recovers_aarch64_direct_control_flow(
    tmp_path,
) -> None:
    try:
        llvm_mc = find_host_llvm_tool("llvm-mc")
    except ToolchainError:
        llvm_mc = None
    try:
        machine_cfg_dump = cfg.require_machine_cfg_dump()
    except RuntimeError:
        pytest.skip("machine-cfg-dump is not built")
    if llvm_mc is None:
        pytest.skip("llvm-mc is not available")

    assembly = tmp_path / "foo.s"
    object_path = tmp_path / "foo.o"
    assembly.write_text(
        """
.text
.globl foo
.type foo, %function
foo:
  cmp w0, #0
  b.eq .Lret
  add w0, w0, #1
.Lret:
  ret
.size foo, .-foo
""",
        encoding="utf-8",
    )
    assembled = subprocess.run(
        [
            llvm_mc,
            "-triple=aarch64-linux-gnu",
            "-filetype=obj",
            str(assembly),
            "-o",
            str(object_path),
        ],
        capture_output=True,
        text=True,
    )
    assert assembled.returncode == 0, assembled.stderr

    dumped = subprocess.run(
        [machine_cfg_dump, str(object_path), "--function=foo"],
        capture_output=True,
        text=True,
    )
    assert dumped.returncode == 0, dumped.stderr
    function = json.loads(dumped.stdout)["functions"][0]

    assert [block["id"] for block in function["blocks"]] == [
        "L0",
        "L8",
        "Lc",
    ]
    assert [block["key"] for block in function["blocks"]] == [
        "foo+0x0",
        "foo+0x8",
        "foo+0xc",
    ]
    assert function["instructions"][0]["id"] == "foo+0x0"
    assert [
        (edge["source"], edge["target"], edge["kind"])
        for edge in function["edges"]
    ] == [
        ("L0", "Lc", "branch_taken"),
        ("L0", "L8", "branch_not_taken"),
        ("L8", "Lc", "fallthrough"),
        ("Lc", None, "return"),
    ]


def test_machine_cfg_dump_keeps_calls_and_marks_indirect_exits(
    tmp_path,
) -> None:
    try:
        llvm_mc = find_host_llvm_tool("llvm-mc")
    except ToolchainError:
        llvm_mc = None
    try:
        machine_cfg_dump = cfg.require_machine_cfg_dump()
    except RuntimeError:
        pytest.skip("machine-cfg-dump is not built")
    if llvm_mc is None:
        pytest.skip("llvm-mc is not available")

    assembly = tmp_path / "flows.s"
    object_path = tmp_path / "flows.o"
    assembly.write_text(
        """
.text
.globl caller
.type caller, %function
caller:
  bl external
  add w0, w0, #1
  ret
.size caller, .-caller

.globl indirect
.type indirect, %function
indirect:
  br x0
.size indirect, .-indirect
""",
        encoding="utf-8",
    )
    assembled = subprocess.run(
        [
            llvm_mc,
            "-triple=aarch64-linux-gnu",
            "-filetype=obj",
            str(assembly),
            "-o",
            str(object_path),
        ],
        capture_output=True,
        text=True,
    )
    assert assembled.returncode == 0, assembled.stderr

    dumped = subprocess.run(
        [machine_cfg_dump, str(object_path)],
        capture_output=True,
        text=True,
    )
    assert dumped.returncode == 0, dumped.stderr
    functions = {
        function["name"]: function
        for function in json.loads(dumped.stdout)["functions"]
    }

    caller = functions["caller"]
    assert len(caller["blocks"]) == 1
    assert caller["blocks"][0]["contains_call"] is True
    assert caller["instructions"][0]["relocation_symbol"] == "external"
    assert [edge["kind"] for edge in caller["edges"]] == ["return"]

    indirect = functions["indirect"]
    assert indirect["blocks"][0]["has_unresolved_exit"] is True
    assert indirect["edges"] == [{
        "source": "Lc",
        "source_key": "indirect+0x0",
        "target": None,
        "target_key": None,
        "kind": "indirect",
        "resolution": "unresolved",
        "target_address": None,
        "target_symbol": None,
    }]


def test_cmd_viewer_builds_and_opens_html(tmp_path, monkeypatch) -> None:
    source = tmp_path / "sample.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    bc = output / "sample.bc"
    source_object = output / "sample.o"
    lifted_ll = output / "sample.lifted.ll"
    asm_map = output / "sample.asm-map.json"
    lifted_binary = output / "sample.lifted_x86_64"
    viewer = output / "sample.cfg-viewer.html"
    calls = {}

    monkeypatch.setattr("dev._require_arm_lifter", lambda: None)
    monkeypatch.setattr("dev._clean_case", lambda name, outdir: None)
    monkeypatch.setattr(
        "dev.compile_bc_and_o",
        lambda src, outdir, extra_cflags: (bc, source_object),
    )
    monkeypatch.setattr(
        "dev._lift",
        lambda *args, **kwargs: calls.setdefault("lift", (args, kwargs)) and 0,
    )
    monkeypatch.setattr("dev.recompile_x86_64", lambda ll, outdir: lifted_binary)
    monkeypatch.setattr(
        "dev.parse_instruction_map",
        lambda path: ("arm_asm.s", {"main": {}}),
    )
    monkeypatch.setattr(
        "dev.build_combined_cfg_models",
        lambda *args: calls.setdefault("models", args) or {"main": {"function": "main"}},
    )

    def render(model, outdir, src_obj, target, output_path):
        calls["render"] = (model, outdir, src_obj, target, output_path)
        Path(output_path).write_text("<html></html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr("dev.render_interactive_viewer", render)
    monkeypatch.setattr(
        "dev.webbrowser.open",
        lambda uri: calls.setdefault("browser_uri", uri) or True,
    )

    cmd_viewer(
        source,
        output,
        None,
        cleanup=True,
        extra_cflags=["-DMODE=1"],
    )

    lift_args, lift_kwargs = calls["lift"]
    assert lift_args[:4] == (source_object, bc, lifted_ll, output / "sample.lift.log")
    assert lift_kwargs == {"cleanup": True, "asm_map": asm_map}
    assert calls["models"] == (
        str(source_object),
        str(lifted_binary),
        {"main": {}},
        "arm_asm.s",
        None,
    )
    assert calls["render"][-1] == str(viewer)
    assert calls["browser_uri"] == viewer.resolve().as_uri()
