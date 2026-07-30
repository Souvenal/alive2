import pytest

import toolchain


def test_linux_llvm_tool_requires_explicit_configuration(monkeypatch) -> None:
    monkeypatch.setattr(toolchain.sys, "platform", "linux")
    monkeypatch.delenv("LLVM_BIN", raising=False)
    monkeypatch.delenv("LLVM_VERSION", raising=False)

    with pytest.raises(
        toolchain.ToolchainError,
        match="LLVM_BIN or LLVM_VERSION is required",
    ):
        toolchain.llvm_tool("clang")


def test_linux_llvm_tool_uses_configured_directory(
    monkeypatch, tmp_path
) -> None:
    clang = tmp_path / "clang"
    clang.write_text("#!/bin/sh\n", encoding="utf-8")
    clang.chmod(0o755)
    monkeypatch.setattr(toolchain.sys, "platform", "linux")
    monkeypatch.setenv("LLVM_BIN", str(tmp_path))

    assert toolchain.llvm_tool("clang") == str(clang)


def test_linux_llvm_tool_does_not_fall_back_to_path(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(toolchain.sys, "platform", "linux")
    monkeypatch.setenv("LLVM_BIN", str(tmp_path))

    with pytest.raises(
        toolchain.ToolchainError,
        match="LLVM tool 'llvm-dis' is not executable",
    ):
        toolchain.llvm_tool("llvm-dis")


def test_linux_llvm_tool_uses_versioned_path_tool(monkeypatch) -> None:
    monkeypatch.setattr(toolchain.sys, "platform", "linux")
    monkeypatch.delenv("LLVM_BIN", raising=False)
    monkeypatch.setenv("LLVM_VERSION", "20")
    monkeypatch.setattr(
        toolchain.shutil,
        "which",
        lambda command: "/usr/bin/clang-20" if command == "clang-20" else None,
    )

    assert toolchain.llvm_tool("clang") == "/usr/bin/clang-20"


def test_non_linux_llvm_tool_uses_vm_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(toolchain.sys, "platform", "darwin")

    assert toolchain.llvm_tool("clang") == "clang"


def test_non_linux_host_tool_requires_path_entry(monkeypatch) -> None:
    monkeypatch.setattr(toolchain.sys, "platform", "darwin")
    monkeypatch.setattr(toolchain.shutil, "which", lambda _: None)

    with pytest.raises(
        toolchain.ToolchainError,
        match="LLVM tool 'llvm-mc' not found in PATH",
    ):
        toolchain.find_host_llvm_tool("llvm-mc")


def test_linux_llvm_version_requires_at_least_major_20(
    monkeypatch, tmp_path
) -> None:
    llvm_config = tmp_path / "llvm-config"
    llvm_config.write_text("#!/bin/sh\n", encoding="utf-8")
    llvm_config.chmod(0o755)
    monkeypatch.setattr(toolchain.sys, "platform", "linux")
    monkeypatch.setenv("LLVM_BIN", str(tmp_path))
    monkeypatch.setattr(
        toolchain.subprocess,
        "run",
        lambda *args, **kwargs: toolchain.subprocess.CompletedProcess(
            args[0], 0, stdout="19.1.0\n", stderr=""
        ),
    )

    with pytest.raises(
        toolchain.ToolchainError,
        match="LLVM toolchain must be at least LLVM 20.x, found 19.1.0",
    ):
        toolchain.check_linux_llvm_version()


def test_linux_llvm_version_matches_requested_suffix(
    monkeypatch, tmp_path
) -> None:
    llvm_config = tmp_path / "llvm-config-20"
    llvm_config.write_text("#!/bin/sh\n", encoding="utf-8")
    llvm_config.chmod(0o755)
    monkeypatch.setattr(toolchain.sys, "platform", "linux")
    monkeypatch.setenv("LLVM_BIN", str(tmp_path))
    monkeypatch.setenv("LLVM_VERSION", "20")
    monkeypatch.setattr(
        toolchain.subprocess,
        "run",
        lambda *args, **kwargs: toolchain.subprocess.CompletedProcess(
            args[0], 0, stdout="21.1.0\n", stderr=""
        ),
    )

    with pytest.raises(
        toolchain.ToolchainError,
        match="LLVM_VERSION=20 selected LLVM 21.1.0",
    ):
        toolchain.check_linux_llvm_version()
