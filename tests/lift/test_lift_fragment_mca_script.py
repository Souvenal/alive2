import importlib.util
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "lift_fragment_mca.py"
)
SPEC = importlib.util.spec_from_file_location("lift_fragment_mca", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def _mock_pipeline_dependencies(monkeypatch, tmp_path, functions):
    monkeypatch.setattr(tool.sys, "platform", "linux")
    source = tmp_path / "input.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    arm_lifter = tmp_path / "arm-lifter"
    arm_lifter.write_text("", encoding="utf-8")
    output_dir = tmp_path / "output"

    def compile_bc_and_o(source_path, workdir):
        bc = workdir / f"{source_path.stem}.bc"
        obj = workdir / f"{source_path.stem}.o"
        bc.write_text("", encoding="utf-8")
        obj.write_text("", encoding="utf-8")
        return bc, obj

    def compile_source_ir(source_path, source_bc, source_ll, nodbg_ll):
        source_ll.write_text("", encoding="utf-8")
        nodbg_ll.write_text("", encoding="utf-8")

    def run_lifter(source_object, source_bc, lifted_ll, log, asm_map, binary):
        lifted_ll.write_text("", encoding="utf-8")
        log.write_text("", encoding="utf-8")
        asm_map.write_text("", encoding="utf-8")

    def recompile_x86_64(lifted_ll, workdir):
        target = workdir / f"{lifted_ll.stem}_x86_64"
        target.write_text("", encoding="utf-8")
        return target

    def recompile_arm64(lifted_ll, workdir):
        obj = workdir / f"{lifted_ll.stem}.arm64.o"
        obj.write_text("", encoding="utf-8")
        binary = workdir / f"{lifted_ll.stem}_arm64"
        binary.write_text("", encoding="utf-8")
        return binary

    def compile_arm64_binary(source_path, workdir):
        binary = workdir / f"{source_path.stem}_arm64"
        binary.write_text("", encoding="utf-8")
        return binary

    monkeypatch.setattr(tool, "compile_bc_and_o", compile_bc_and_o)
    monkeypatch.setattr(tool, "_compile_source_ir", compile_source_ir)
    monkeypatch.setattr(tool, "_run_lifter", run_lifter)
    monkeypatch.setattr(tool, "recompile_x86_64", recompile_x86_64)
    monkeypatch.setattr(tool, "recompile_arm64", recompile_arm64)
    monkeypatch.setattr(tool, "compile_arm64_binary", compile_arm64_binary)
    monkeypatch.setattr(
        tool,
        "_compile_arm_assembly",
        lambda *args: None,
    )
    monkeypatch.setattr(
        tool,
        "parse_instruction_map",
        lambda path: ("arm_asm.s", {name: {} for name in functions}),
    )
    monkeypatch.setattr(
        tool,
        "build_fragment_report",
        lambda source, target, asm_map, function, *args: _fake_report(
            source,
            target,
            function,
        ),
    )
    monkeypatch.setattr(
        tool,
        "_json_report",
        lambda report: report,
    )
    monkeypatch.setattr(
        tool,
        "render_markdown_report",
        lambda report: f"# {report['function']}\n",
    )
    return source, arm_lifter, output_dir


def _fake_report(source, target, function):
    target_path = Path(target)
    target_arch = (
        "aarch64" if target_path.name.endswith(".arm64.o") else "x86-64"
    )
    return {
        "function": function,
        "source": {"path": source, "arch": "aarch64"},
        "target": {"path": target, "arch": target_arch},
        "mca": {
            "comparison_status": (
                "comparable" if target_arch == "aarch64" else "cross_isa"
            )
        },
        "fragments": [
            _fragment("improved", 5.0, 4.0),
            _fragment("unchanged", 5.0, 5.0),
            _fragment("regressed", 5.0, 6.0),
            _fragment("mca_error", 5.0, 4.0, target_status="error"),
            {"id": "rejected", "status": "rejected"},
            {"id": "excluded", "status": "excluded"},
        ],
    }


def _fragment(
    fragment_id,
    source_cycles,
    target_cycles,
    *,
    source_status="ok",
    target_status="ok",
):
    return {
        "id": fragment_id,
        "status": "accepted",
        "mca": {
            "status": "ok",
            "source": {
                "status": source_status,
                "cycles_per_iteration": source_cycles,
            },
            "target": {
                "status": target_status,
                "cycles_per_iteration": target_cycles,
            },
        },
    }


def test_run_pipeline_writes_both_reports_for_each_function(
    monkeypatch, tmp_path
):
    source, arm_lifter, output_dir = _mock_pipeline_dependencies(
        monkeypatch,
        tmp_path,
        ["helper", "main"],
    )

    reports = tool.run_pipeline(
        source,
        output_dir,
        arm_lifter=arm_lifter,
        machine_cfg_dump=None,
        mcpu="generic",
        mattr="",
        iterations=100,
        min_source_instructions=2,
        min_target_instructions=2,
    )

    assert [label for label, _, _ in reports] == [
        "helper ARM64 all",
        "helper ARM64 improved",
        "helper x86_64 all",
        "helper x86_64 improved",
        "main ARM64 all",
        "main ARM64 improved",
        "main x86_64 all",
        "main x86_64 improved",
    ]
    assert all(
        markdown_path is None
        for _, _, markdown_path in reports
    )
    build_dir = output_dir / "build"
    report_dir = output_dir / "report"
    assert (report_dir / "input.helper.fragment-mca.json").is_file()
    assert not (report_dir / "input.helper.fragment-mca.md").exists()
    assert (
        report_dir / "input.helper.fragment-mca-arm-improved.json"
    ).is_file()
    assert (
        report_dir / "input.helper.fragment-mca-arm-improved.md"
    ).exists() is False
    assert (report_dir / "input.helper.fragment-mca-x64.json").is_file()
    assert not (report_dir / "input.helper.fragment-mca-x64.md").exists()
    assert (
        report_dir / "input.helper.fragment-mca-x64-improved.json"
    ).is_file()
    assert (
        report_dir / "input.helper.fragment-mca-x64-improved.md"
    ).exists() is False
    assert (report_dir / "input.main.fragment-mca.json").is_file()
    assert not (report_dir / "input.main.fragment-mca-x64.md").exists()
    assert (build_dir / "input_arm64").is_file()
    assert not list(output_dir.glob("input*"))
    summary = (
        report_dir / "input.improved-summary.txt"
    ).read_text(encoding="utf-8")
    assert "helper ARM64 improved:" in summary
    assert "main x86_64 improved:" in summary
    assert "  - improved" in summary
    arm_report = (
        report_dir / "input.main.fragment-mca.json"
    ).read_text(encoding="utf-8")
    assert "input.lifted.arm64.o" in arm_report
    assert "input.lifted_arm64\"" not in arm_report


def test_main_highlights_improved_fragments(monkeypatch, tmp_path, capsys):
    improved_report = tmp_path / "main.fragment-mca-improved.json"
    improved_report.write_text(
        (
            '{"fragments": [{"id": "fragment_2", "mca": {"source": '
            '{"cycles_per_iteration": 5.0}, "target": '
            '{"cycles_per_iteration": 3.5}}}]}'
        ),
        encoding="utf-8",
    )
    all_report = tmp_path / "main.fragment-mca.json"
    all_report.write_text('{"fragments": []}', encoding="utf-8")
    improved_markdown = improved_report.with_suffix(".md")
    all_markdown = all_report.with_suffix(".md")
    monkeypatch.setattr(
        tool,
        "run_pipeline",
        lambda *args, **kwargs: [
            ("main ARM64 all", all_report, all_markdown),
            ("main ARM64 improved", improved_report, improved_markdown),
        ],
    )

    assert tool.main(["input.c"]) == 0

    output = capsys.readouterr().out
    assert ">>> IMPROVED: 1 fragment(s) found" in output
    assert (
        "fragment_2: cycles/iteration 5.00 -> 3.50 (-1.50)"
        in output
    )


def test_main_reports_empty_improved_result(monkeypatch, tmp_path, capsys):
    improved_report = tmp_path / "main.fragment-mca-improved.json"
    improved_report.write_text('{"fragments": []}', encoding="utf-8")
    improved_markdown = improved_report.with_suffix(".md")
    monkeypatch.setattr(
        tool,
        "run_pipeline",
        lambda *args, **kwargs: [
            ("main ARM64 improved", improved_report, improved_markdown),
        ],
    )

    assert tool.main(["input.c"]) == 0

    assert ">>> IMPROVED: none (0 fragments)" in capsys.readouterr().out


def test_write_improved_summary_lists_only_nonempty_reports(tmp_path):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    improved = report_dir / "input.main.fragment-mca-arm-improved.json"
    improved.write_text(
        '{"fragments": [{"id": "fragment_3"}]}',
        encoding="utf-8",
    )
    empty = report_dir / "input.main.fragment-mca-x64-improved.json"
    empty.write_text('{"fragments": []}', encoding="utf-8")

    summary_path = tool._write_improved_summary(
        "input",
        report_dir,
        [
            ("main ARM64 improved", improved, None),
            ("main x86_64 improved", empty, None),
        ],
    )

    assert summary_path.read_text(encoding="utf-8") == (
        "Improved fragments\n"
        "==================\n"
        "\n"
        "main ARM64 improved:\n"
        "  - fragment_3\n"
    )


def test_write_improved_summary_reports_no_improvements(tmp_path):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    empty = report_dir / "input.main.fragment-mca-arm-improved.json"
    empty.write_text('{"fragments": []}', encoding="utf-8")

    summary_path = tool._write_improved_summary(
        "input",
        report_dir,
        [("main ARM64 improved", empty, None)],
    )

    assert summary_path.read_text(encoding="utf-8") == (
        "Improved fragments\n"
        "==================\n"
        "\n"
        "No improved fragments found.\n"
    )


def test_write_fragment_reports_writes_markdown_only_when_requested(
    monkeypatch, tmp_path
):
    report = _fake_report("source.o", "target.arm64.o", "main")
    monkeypatch.setattr(tool, "build_fragment_report", lambda *args: report)
    monkeypatch.setattr(tool, "_json_report", lambda candidate: candidate)
    monkeypatch.setattr(
        tool,
        "render_markdown_report",
        lambda candidate: f"# {candidate['function']}\n",
    )

    json_only = tool._write_fragment_reports(
        tmp_path / "input.o",
        tmp_path / "target.arm64.o",
        tmp_path / "input.asm-map.json",
        "main",
        tmp_path,
        report_suffix="",
        improved_suffix="-arm-improved",
        write_md=False,
        mcpu="generic",
        mattr="",
        iterations=100,
        min_source_instructions=2,
        min_target_instructions=2,
    )
    assert json_only[0].is_file()
    assert json_only[1] is None
    assert json_only[2].is_file()
    assert json_only[3] is None

    with_markdown = tool._write_fragment_reports(
        tmp_path / "input.o",
        tmp_path / "target.arm64.o",
        tmp_path / "input.asm-map.json",
        "main",
        tmp_path,
        report_suffix="",
        improved_suffix="-arm-improved",
        write_md=True,
        mcpu="generic",
        mattr="",
        iterations=100,
        min_source_instructions=2,
        min_target_instructions=2,
    )
    assert all(path.is_file() for path in with_markdown)


def test_improved_report_only_contains_lower_cycle_fragments():
    report = _fake_report("source.o", "target.arm64.o", "main")

    improved = tool._improved_report(report)

    assert [fragment["id"] for fragment in improved["fragments"]] == [
        "improved"
    ]
    assert improved["fragment_filter"] == {
        "kind": "improved_cycles_per_iteration",
        "condition": (
            "target.cycles_per_iteration < source.cycles_per_iteration"
        ),
        "cross_isa": False,
    }
    assert len(report["fragments"]) == 6


def test_improved_report_handles_missing_and_invalid_cycle_values():
    report = _fake_report("source.o", "target.arm64.o", "main")
    report["fragments"] = [
        _fragment("missing_source", None, 1.0),
        _fragment("missing_target", 2.0, None),
        _fragment("boolean", True, False),
        {"id": "missing_mca", "status": "accepted"},
    ]

    improved = tool._improved_report(report)

    assert improved["fragments"] == []


def test_improved_cross_isa_report_keeps_warning_metadata(monkeypatch):
    report = _fake_report("source.o", "target_x86_64", "main")
    monkeypatch.setattr(
        tool,
        "render_markdown_report",
        lambda candidate: f"# {candidate['function']}\n",
    )

    improved = tool._improved_report(report)

    assert improved["fragment_filter"]["cross_isa"] is True
    markdown = tool._render_improved_markdown(improved)
    assert "not directly comparable" in markdown


def test_write_fragment_reports_writes_empty_improved_report(
    monkeypatch, tmp_path
):
    report = _fake_report("source.o", "target.arm64.o", "main")
    report["fragments"] = [_fragment("unchanged", 5.0, 5.0)]
    build_calls = []
    monkeypatch.setattr(
        tool,
        "build_fragment_report",
        lambda *args: build_calls.append(args) or report,
    )
    monkeypatch.setattr(tool, "_json_report", lambda candidate: candidate)
    monkeypatch.setattr(
        tool,
        "render_markdown_report",
        lambda candidate: f"# {candidate['function']}\n",
    )

    paths = tool._write_fragment_reports(
        tmp_path / "input.o",
        tmp_path / "target.arm64.o",
        tmp_path / "input.asm-map.json",
        "main",
        tmp_path,
        report_suffix="",
        improved_suffix="-arm-improved",
        write_md=False,
        mcpu="generic",
        mattr="",
        iterations=100,
        min_source_instructions=2,
        min_target_instructions=2,
    )

    assert len(build_calls) == 1
    assert paths[0].is_file()
    assert paths[1] is None
    assert paths[2].is_file()
    assert paths[3] is None
    improved_json = paths[2].read_text(encoding="utf-8")
    assert '"fragments": []' in improved_json


def test_run_pipeline_preserves_files_that_only_share_the_stem(
    monkeypatch, tmp_path
):
    source, arm_lifter, output_dir = _mock_pipeline_dependencies(
        monkeypatch,
        tmp_path,
        ["main"],
    )
    output_dir.mkdir()
    unrelated = output_dir / "input-not-generated.txt"
    unrelated.write_text("keep\n", encoding="utf-8")
    (output_dir / "build").mkdir()
    unrelated_build = output_dir / "build" / "input-not-generated.txt"
    unrelated_build.write_text("keep build\n", encoding="utf-8")
    (output_dir / "report").mkdir()
    unrelated_report = output_dir / "report" / "input-not-generated.txt"
    unrelated_report.write_text("keep report\n", encoding="utf-8")

    tool.run_pipeline(
        source,
        output_dir,
        arm_lifter=arm_lifter,
        machine_cfg_dump=None,
        mcpu="generic",
        mattr="",
        iterations=100,
        min_source_instructions=2,
        min_target_instructions=2,
    )

    assert unrelated.read_text(encoding="utf-8") == "keep\n"
    assert unrelated_build.read_text(encoding="utf-8") == "keep build\n"
    assert unrelated_report.read_text(encoding="utf-8") == "keep report\n"


def test_run_pipeline_function_filter_only_writes_selected_reports(
    monkeypatch, tmp_path
):
    source, arm_lifter, output_dir = _mock_pipeline_dependencies(
        monkeypatch,
        tmp_path,
        ["helper", "main"],
    )

    reports = tool.run_pipeline(
        source,
        output_dir,
        arm_lifter=arm_lifter,
        machine_cfg_dump=None,
        mcpu="generic",
        mattr="",
        iterations=100,
        min_source_instructions=2,
        min_target_instructions=2,
        function="main",
    )

    assert [label for label, _, _ in reports] == [
        "main ARM64 all",
        "main ARM64 improved",
        "main x86_64 all",
        "main x86_64 improved",
    ]
    report_dir = output_dir / "report"
    assert (report_dir / "input.main.fragment-mca.json").is_file()
    assert (
        report_dir / "input.main.fragment-mca-arm-improved.json"
    ).is_file()
    assert not (report_dir / "input.helper.fragment-mca.json").exists()
    assert not (
        report_dir / "input.helper.fragment-mca-arm-improved.json"
    ).exists()


def test_run_pipeline_rejects_unknown_function(monkeypatch, tmp_path):
    source, arm_lifter, output_dir = _mock_pipeline_dependencies(
        monkeypatch,
        tmp_path,
        ["main"],
    )

    try:
        tool.run_pipeline(
            source,
            output_dir,
            arm_lifter=arm_lifter,
            machine_cfg_dump=None,
            mcpu="generic",
            mattr="",
            iterations=100,
            min_source_instructions=2,
            min_target_instructions=2,
            function="missing",
        )
    except ValueError as error:
        assert "function 'missing' not found" in str(error)
    else:
        raise AssertionError("unknown function should be rejected")


def test_require_vm_shared_path_rejects_non_users_path_on_macos(
    monkeypatch,
):
    monkeypatch.setattr(tool.sys, "platform", "darwin")

    try:
        tool._require_vm_shared_path(Path("/private/tmp/output"), "output")
    except ValueError as error:
        assert "must be under /Users on macOS" in str(error)
    else:
        raise AssertionError("non-shared macOS path should be rejected")
