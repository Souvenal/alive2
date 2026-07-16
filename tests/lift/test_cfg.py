import json

import pytest

from asm_diff import parse_instruction_map
from cfg import (
    align_source_blocks_with_instruction_map,
    build_address_block_relations,
    build_block_relations,
    build_combined_cfg_dot,
    build_cfg,
    build_missing_function_report,
    build_relation_components,
    extract_target_addr,
    parse_objdump_output,
)


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
