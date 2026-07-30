"""LLVM tool resolution for the arm-lifter test suite."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

LLVM_MIN_MAJOR_VERSION = 20


class ToolchainError(RuntimeError):
    """Raised when the configured LLVM toolchain cannot provide a tool."""


def llvm_tool(name: str) -> str:
    """Return the LLVM tool command for the current platform.

    Linux requires LLVM_BIN or LLVM_VERSION so the test suite cannot silently
    select a distribution-provided LLVM version. macOS and Windows retain the
    existing VM-side PATH lookup behavior.
    """
    if sys.platform != "linux":
        return name

    llvm_bin = os.environ.get("LLVM_BIN")
    llvm_version = os.environ.get("LLVM_VERSION")
    if not llvm_bin and not llvm_version:
        raise ToolchainError(
            "LLVM_BIN or LLVM_VERSION is required on Linux; set LLVM_VERSION "
            "to a versioned toolchain such as 20, or LLVM_BIN to a directory "
            "containing unversioned LLVM tools"
        )

    if llvm_version and not llvm_version.isdecimal():
        raise ToolchainError(
            f"LLVM_VERSION must be a numeric major version, got '{llvm_version}'"
        )

    command = f"{name}-{llvm_version}" if llvm_version else name
    if llvm_bin:
        tool = Path(llvm_bin).expanduser() / command
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise ToolchainError(
                f"LLVM tool '{command}' is not executable at {tool}"
            )
        return str(tool)

    tool = shutil.which(command)
    if not tool:
        raise ToolchainError(
            f"LLVM tool '{command}' not found in PATH"
        )
    return tool


def find_host_llvm_tool(name: str) -> str:
    """Return an executable LLVM tool for a host-side analysis command."""
    tool = llvm_tool(name)
    if sys.platform == "linux":
        return tool

    resolved = shutil.which(tool)
    if not resolved:
        raise ToolchainError(f"LLVM tool '{name}' not found in PATH")
    return resolved


def check_linux_llvm_version() -> None:
    """Require the LLVM major version used to build arm-lifter on Linux."""
    if sys.platform != "linux":
        return

    llvm_config = llvm_tool("llvm-config")
    result = subprocess.run(
        [llvm_config, "--version"],
        capture_output=True,
        text=True,
    )
    version = result.stdout.strip()
    if result.returncode != 0 or not version:
        raise ToolchainError(
            f"Could not determine LLVM version with {llvm_config}"
        )

    major = version.split(".", 1)[0]
    if not major.isdecimal() or int(major) < LLVM_MIN_MAJOR_VERSION:
        raise ToolchainError(
            f"LLVM toolchain must be at least LLVM {LLVM_MIN_MAJOR_VERSION}.x, "
            f"found {version}"
        )
    requested_version = os.environ.get("LLVM_VERSION")
    if requested_version and major != requested_version:
        raise ToolchainError(
            f"LLVM_VERSION={requested_version} selected LLVM {version}"
        )
