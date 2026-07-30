import pytest

import fragment_mca as fm


def instruction(
    address: int,
    arm_inst_id: int | None,
    *,
    size: int = 4,
    opcode_id: int | None = None,
    is_call: bool = False,
    is_terminator: bool = False,
) -> dict:
    return {
        "addr": address,
        "size": size,
        "mnemonic": "add",
        "operands": "w0, w0, #1",
        "opcode_id": opcode_id if opcode_id is not None else arm_inst_id,
        "arm_inst_id": arm_inst_id,
        "dwarf_line": (
            arm_inst_id + 1 if arm_inst_id is not None else None
        ),
        "is_call": is_call,
        "is_terminator": is_terminator,
    }


def source_map(*ids: int) -> dict[int, dict[str, object]]:
    return {
        arm_inst_id: {
            "arm_inst_id": arm_inst_id,
            "dwarf_line": arm_inst_id + 1,
            "mc_block": "foo",
            "opcode": "ADDXri",
            "opcode_id": arm_inst_id,
            "asm": "\tadd\tx0, x0, #1",
        }
        for arm_inst_id in ids
    }


def test_validate_and_annotate_source_checks_opcode_ids() -> None:
    source = [instruction(0, None, opcode_id=7)]
    blocks = [("L0", source)]
    records = source_map(0)
    records[0]["opcode_id"] = 8

    with pytest.raises(ValueError, match="opcode mismatch"):
        fm.validate_and_annotate_source(blocks, records)


def test_build_fragments_accepts_contiguous_target_expansion() -> None:
    source = [
        instruction(0x0, 0),
        instruction(0x4, 1),
    ]
    target = [
        instruction(0x20, 0),
        instruction(0x24, 0),
        instruction(0x28, 1),
    ]

    fragments = fm.build_provenance_fragments(source, target, set())

    assert len(fragments) == 1
    fragment = fragments[0]
    assert fragment["status"] == "accepted"
    assert fragment["arm_inst_ids"] == [0, 1]
    assert len(fragment["target"]["instructions"]) == 3


def test_build_fragments_rejects_too_short_regions_by_default() -> None:
    fragments = fm.build_provenance_fragments(
        [instruction(0x0, 0)],
        [instruction(0x20, 0)],
        set(),
    )

    assert fragments[0]["status"] == "rejected"
    assert fragments[0]["reason"] == "too_short"


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ([instruction(0x20, 0)], "target_id_missing"),
        (
            [
                instruction(0x20, 0),
                instruction(0x24, 1),
                instruction(0x28, 0),
            ],
            "target_id_split",
        ),
        (
            [
                instruction(0x20, 1),
                instruction(0x24, 0),
            ],
            "target_id_reordered",
        ),
        (
            [
                instruction(0x20, 0),
                instruction(0x24, None),
                instruction(0x28, 1),
            ],
            "target_not_contiguous",
        ),
    ],
)
def test_build_fragments_rejects_non_strict_provenance(
    target: list[dict],
    reason: str,
) -> None:
    source = [
        instruction(0x0, 0),
        instruction(0x4, 1),
    ]

    fragments = fm.build_provenance_fragments(source, target, set())

    assert any(
        fragment["status"] == "rejected" and fragment["reason"] == reason
        for fragment in fragments
    )


def test_build_fragments_splits_at_call_and_terminator() -> None:
    source = [
        instruction(0x0, 0),
        instruction(0x4, 1),
        instruction(0x8, 2, is_call=True),
        instruction(0xC, 3),
        instruction(0x10, 4),
        instruction(0x14, 5, is_terminator=True),
    ]
    target = [
        instruction(0x20, 0),
        instruction(0x24, 1),
        instruction(0x28, 2),
        instruction(0x2C, 3),
        instruction(0x30, 4),
        instruction(0x34, 5),
    ]

    fragments = fm.build_provenance_fragments(source, target, set())

    assert [
        (fragment["status"], fragment.get("reason"))
        for fragment in fragments
    ] == [
        ("accepted", None),
        ("excluded", "source_call"),
        ("accepted", None),
        ("excluded", "source_terminator"),
    ]


def test_build_fragments_rejects_untrusted_target_nop() -> None:
    source = [instruction(0x0, 0)]
    target = [instruction(0x20, 0)]

    fragments = fm.build_provenance_fragments(source, target, {0})

    assert fragments[0]["status"] == "rejected"
    assert fragments[0]["reason"] == "untrusted_target_nop"


def test_build_fragments_rejects_target_control_flow() -> None:
    source = [instruction(0x0, 0)]
    target = [instruction(0x20, 0, is_terminator=True)]

    fragments = fm.build_provenance_fragments(source, target, set())

    assert fragments[0]["status"] == "rejected"
    assert fragments[0]["reason"] == "target_control_flow"


def test_analyze_fragments_marks_cross_isa_without_delta(monkeypatch) -> None:
    source = [instruction(0x0, 0)]
    target = [instruction(0x20, 0)]
    fragments = fm.build_provenance_fragments(source, target, set(), 1, 1)

    def run(regions, arch, mcpu, iterations, mattr=""):
        return {
            name: {
                "status": "ok",
                "instruction_count": len(region),
                "uops_per_iteration": 1.0,
                "block_rthroughput": 1.0,
                "cycles_per_iteration": 1.0,
            }
            for name, region in regions.items()
        }

    monkeypatch.setattr(fm, "run_llvm_mca_regions", run)

    mca = fm.analyze_fragments_with_mca(
        fragments,
        "aarch64",
        "x86-64",
        "generic",
        "generic",
        "",
        "",
        100,
    )

    assert mca["comparison_status"] == "cross_isa"
    assert fragments[0]["mca"]["status"] == "cross_isa"
    assert "delta" not in fragments[0]["mca"]


def test_analyze_fragments_compares_same_configuration(monkeypatch) -> None:
    source = [instruction(0x0, 0)]
    target = [instruction(0x20, 0), instruction(0x24, 0)]
    fragments = fm.build_provenance_fragments(source, target, set(), 1, 1)

    def run(regions, arch, mcpu, iterations, mattr=""):
        multiplier = 1 if arch == "aarch64" and mcpu == "generic" else 2
        return {
            name: {
                "status": "ok",
                "instruction_count": len(region),
                "uops_per_iteration": float(len(region) * multiplier),
                "block_rthroughput": float(len(region) * multiplier),
                "cycles_per_iteration": float(len(region) * multiplier),
            }
            for name, region in regions.items()
        }

    monkeypatch.setattr(fm, "run_llvm_mca_regions", run)

    mca = fm.analyze_fragments_with_mca(
        fragments,
        "aarch64",
        "aarch64",
        "generic",
        "generic",
        "",
        "",
        100,
    )

    assert mca["comparison_status"] == "comparable"
    assert fragments[0]["mca"]["status"] == "ok"
    assert fragments[0]["mca"]["delta"]["instruction_count"] == 1


def test_build_fragment_report_integrates_loading_and_mca(monkeypatch) -> None:
    source = [instruction(0x0, None, opcode_id=0)]
    target = [instruction(0x20, None)]
    target[0]["dwarf_line"] = 1
    source_graph = {
        "blocks": [("L0", source)],
        "instructions": source,
    }
    target_graph = {
        "blocks": [("L20", target)],
        "instructions": target,
    }

    monkeypatch.setattr(
        fm,
        "parse_instruction_map",
        lambda path: ("arm_asm.s", {"foo": source_map(0)}),
    )
    monkeypatch.setattr(
        fm,
        "load_machine_cfg",
        lambda path, debug_file=None, function_filter=None: (
            "aarch64",
            {"foo": source_graph if path == "source.o" else target_graph},
        ),
    )
    monkeypatch.setattr(
        fm,
        "run_llvm_mca_regions",
        lambda regions, arch, mcpu, iterations, mattr="": {
            name: {
                "status": "ok",
                "instruction_count": len(region),
                "uops_per_iteration": float(len(region)),
                "block_rthroughput": float(len(region)),
                "cycles_per_iteration": float(len(region)),
            }
            for name, region in regions.items()
        },
    )

    report = fm.build_fragment_report(
        "source.o",
        "target.o",
        "map.json",
        "foo",
        "generic",
        "generic",
        "",
        "",
        100,
        1,
        1,
    )

    assert report["version"] == 1
    assert report["mca"]["comparison_status"] == "comparable"
    assert report["fragments"][0]["status"] == "accepted"
    assert "_source_instructions" not in fm._json_report(report)["fragments"][0]


def test_render_markdown_report_shows_code_and_costs() -> None:
    source = [instruction(0x0, 0)]
    target = [instruction(0x20, 0), instruction(0x24, 0)]
    fragments = fm.build_provenance_fragments(source, target, set(), 1, 1)
    fragments[0]["mca"] = {
        "status": "ok",
        "source": {
            "instruction_count": 1,
            "uops_per_iteration": 1.0,
            "block_rthroughput": 1.0,
            "cycles_per_iteration": 1.0,
        },
        "target": {
            "instruction_count": 2,
            "uops_per_iteration": 2.0,
            "block_rthroughput": 2.0,
            "cycles_per_iteration": 2.0,
        },
        "delta": {
            "instruction_count": 1,
            "uops_per_iteration": 1.0,
            "block_rthroughput": 1.0,
            "cycles_per_iteration": 1.0,
        },
        "ratio": {
            "instruction_count": 2.0,
            "uops_per_iteration": 2.0,
            "block_rthroughput": 2.0,
            "cycles_per_iteration": 2.0,
        },
    }
    report = {
        "function": "foo",
        "source": {"path": "source.o", "arch": "aarch64"},
        "target": {"path": "target.o", "arch": "aarch64"},
        "mca": {
            "source_mcpu": "generic",
            "target_mcpu": "generic",
            "source_mattr": "",
            "target_mattr": "",
            "iterations": 100,
            "comparison_status": "comparable",
        },
        "fragments": fragments + [{
            "status": "rejected",
            "reason": "target_id_missing",
            "arm_inst_ids": [1],
            "source": {"address_range": ["0x4", "0x4"]},
        }],
    }

    markdown = fm.render_markdown_report(report)

    assert "# Provenance Fragment MCA: `foo`" in markdown
    assert "## Accepted Fragments" in markdown
    assert "sub" not in markdown
    assert "add w0, w0, #1" in markdown
    assert "| Block throughput | 1.00 | 2.00 | +1.00 | 2.00 |" in markdown
    assert "`target_id_missing`" in markdown
