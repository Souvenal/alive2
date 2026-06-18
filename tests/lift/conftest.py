import glob
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

# --- Paths ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CASES_DIR = Path(__file__).resolve().parent / "cases"
BUILD_DIR = Path(__file__).resolve().parent / "build"

# --- Platform detection ---
IS_DARWIN = sys.platform == "darwin"
IS_LINUX = sys.platform == "linux"
IS_WINDOWS = sys.platform == "win32"

# --- VM dispatcher ---
# All compilation and QEMU execution happens inside a Linux VM.
# On macOS the VM is Lima; on Windows it's WSL; on Linux we run directly.

def _vm_prefix() -> list[str]:
    """Return the prefix needed to run commands inside the Linux VM."""
    if IS_LINUX:
        return []
    elif IS_DARWIN:
        return ["lima", "--"]
    else:
        return ["wsl", "--"]


def _to_vm_path(p: Path) -> str:
    """Convert a host path to a path accessible inside the Linux VM.

    On macOS the host filesystem is shared into Lima verbatim (same path).
    On Windows the path must be translated to the WSL /mnt/ layout.
    """
    if IS_WINDOWS:
        drive = p.drive[0].lower()
        return f"/mnt/{drive}/{p.relative_to(p.anchor).as_posix()}"
    return str(p)


# --- Tool configuration ---
# arm-lifter runs natively on the host (even on macOS, where it is the only
# component that does not go through the Linux VM).
ARM_LIFTER = os.environ.get(
    "ARM_LIFTER", str(PROJECT_ROOT / "build/Release/arm-lifter")
)

# Everything else (compilation, disassembly, QEMU) runs inside a Linux VM
# where clang and the rest of the LLVM toolchain are installed natively.
# Strip directory components — commands resolve through the VM's own PATH.
CLANG = os.path.basename(os.environ.get("CC", "clang"))
# Linux: os.environ.get("CC", "clang-20")
LLVM_DIS = os.path.basename(os.environ.get("LLVM_DIS", "llvm-dis"))
# Linux: os.environ.get("LLVM_DIS", "llvm-dis-20")
LLC = os.path.basename(os.environ.get("LLC", "llc"))
# Linux: os.environ.get("LLC", "llc-20")

# # Linux gcc-cross toolchain auto-detection (uncomment on Linux)
# # gcc_cross_dirs = sorted(glob.glob('/usr/lib/gcc-cross/aarch64-linux-gnu/*/'))
# # gcc_cross_path = gcc_cross_dirs[-1] if gcc_cross_dirs else "/usr/lib/gcc-cross/aarch64-linux-gnu/11/"
# # CFLAGS = [
# #     "-target", "aarch64-linux-gnu",
# #     "-fuse-ld=/usr/bin/ld.lld-20",
# #     "-B", gcc_cross_path,
# #     "-L", gcc_cross_path,
# #     "-I", "/usr/aarch64-linux-gnu/include",
# #     "-L", "/usr/aarch64-linux-gnu/lib",
# #     "-fno-sanitize=all",
# #     "-O2"
# # ]

CFLAGS = os.environ.get(
    "CFLAGS", "-target aarch64-linux-gnu -fno-sanitize=all -O2"
).split()

# Timeouts
RUN_TIMEOUT = 60  # seconds


# --- Helpers (module-level, importable outside pytest) ---

def run_command(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 120,
    stdin_text: str = "",
) -> subprocess.CompletedProcess:
    """Run a command with timeout, capturing stdout+stderr. Raises on error."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            input=stdin_text if stdin_text else None,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"Command timed out after {timeout}s:\n  {' '.join(cmd)}"
        ) from e


def run_in_vm(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 120,
    stdin_text: str = "",
) -> subprocess.CompletedProcess:
    """Run a command inside the Linux VM (platform-appropriate prefix)."""
    return run_command(
        _vm_prefix() + cmd, cwd=cwd, timeout=timeout, stdin_text=stdin_text
    )


# --- Cross-platform binary runners ---
# All test binaries are Linux ELF. Both the AArch64 reference and the x86_64
# subject always run under QEMU inside the Linux VM:

def _to_wsl_path(p: Path) -> str:
    """Convert a Windows path to a WSL path."""
    drive = p.drive[0].lower()
    return f"/mnt/{drive}/{p.relative_to(p.anchor).as_posix()}"


def run_aarch64_linux_binary(
    binary: Path,
    *,
    stdin_text: str = "",
    timeout: int = RUN_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run an AArch64 Linux binary under QEMU (inside VM)."""
    if IS_DARWIN:
        return run_command(
            ["lima", "--", "qemu-aarch64", str(binary)],
            stdin_text=stdin_text, timeout=timeout,
        )
    elif IS_LINUX:
        return run_command(
            ["qemu-aarch64", str(binary)], stdin_text=stdin_text, timeout=timeout
        )
    else:
        return run_command(
            ["wsl", "--", "qemu-aarch64", _to_wsl_path(binary)],
            stdin_text=stdin_text, timeout=timeout,
        )


def run_x86_64_linux_binary(
    binary: Path,
    *,
    stdin_text: str = "",
    timeout: int = RUN_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run an x86_64 Linux binary under QEMU (inside VM)."""
    if IS_DARWIN:
        return run_command(
            ["lima", "--", "qemu-x86_64", str(binary)],
            stdin_text=stdin_text, timeout=timeout,
        )
    elif IS_LINUX:
        return run_command(
            ["qemu-x86_64", str(binary)], stdin_text=stdin_text, timeout=timeout
        )
    else:
        return run_command(
            ["wsl", "--", "qemu-x86_64", _to_wsl_path(binary)],
            stdin_text=stdin_text, timeout=timeout,
        )


# --- Pipeline steps ---

def compile_bc_and_o(
    src: Path, workdir: Path, extra_cflags: list[str] | None = None
) -> tuple[Path, Path]:
    """Compile <src> to .bc and .o in <workdir>. Returns (bc_path, o_path).

    Runs inside the Linux VM where clang and the LLVM toolchain live.
    """
    base = workdir / src.stem
    bc = base.with_suffix(".bc")
    o = base.with_suffix(".o")

    vm_src = _to_vm_path(src)
    vm_bc = _to_vm_path(bc)
    vm_o = _to_vm_path(o)

    cmd = [CLANG] + CFLAGS + (extra_cflags or [])

    r = run_in_vm(cmd + ["-emit-llvm", "-c", vm_src, "-o", vm_bc])
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to compile .bc for {src.name}:\n{r.stderr}\n{r.stdout}"
        )

    r = run_in_vm(cmd + ["-c", vm_src, "-o", vm_o])
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to compile .o for {src.name}:\n{r.stderr}\n{r.stdout}"
        )
    return bc, o


def run_arm_lifter(o: Path, bc: Path, workdir: Path) -> Path:
    """Run arm-lifter on <o> with --src-bc=<bc>. Returns path to lifted .ll.

    arm-lifter runs natively on the host (it does NOT go through the VM).
    """
    output = workdir / f"{o.stem}.lifted.ll"
    r = run_command(
        [ARM_LIFTER, str(o), "--src-bc", str(bc), "-o", str(output)]
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"arm-lifter failed on {o.name}:\n{r.stderr}\n{r.stdout}"
        )
    return output


def compile_arm64_binary(
    src: Path, workdir: Path, extra_cflags: list[str] | None = None
) -> Path:
    """Compile src to a static ARM64 Linux binary (inside VM)."""
    output = workdir / f"{src.stem}_arm64"
    vm_src = _to_vm_path(src)
    vm_output = _to_vm_path(output)

    r = run_in_vm(
        [CLANG] + CFLAGS + (extra_cflags or [])
        + ["-static", vm_src, "-o", vm_output]
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to compile ARM64 binary for {src.name}:\n{r.stderr}\n{r.stdout}"
        )
    return output


def recompile_x86_64(ll_path: Path, workdir: Path) -> Path:
    """Recompile lifted .ll to a static x86_64 Linux binary (inside VM)."""
    o_path = workdir / f"{ll_path.stem}.o"
    output = workdir / f"{ll_path.stem}_x86_64"

    vm_ll = _to_vm_path(ll_path)
    vm_o = _to_vm_path(o_path)
    vm_output = _to_vm_path(output)

    r = run_in_vm(
        [CLANG, "-target", "x86_64-linux-gnu", "-c",
         vm_ll, "-o", vm_o]
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to compile x86_64 .o from {ll_path.name}:\n{r.stderr}\n{r.stdout}"
        )

    r = run_in_vm(
        [CLANG, "-target", "x86_64-linux-gnu", "-static",
         vm_o, "-o", vm_output, "-lm"]
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to link x86_64 binary:\n{r.stderr}\n{r.stdout}"
        )
    return output


def get_stdin(name: str) -> str:
    """Read stdin sidecar file if it exists, else return empty string."""
    stdin_file = CASES_DIR / f"{name}.stdin"
    if stdin_file.exists():
        return stdin_file.read_text()
    return ""


# --- Prerequisites ---

def _check_tool_in_vm(tool: str) -> bool:
    """Return True if <tool> is available inside the Linux VM."""
    if IS_LINUX:
        return shutil.which(tool) is not None
    try:
        return run_in_vm(["which", tool], timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def check_prerequisites() -> list[str]:
    """Return list of skip reasons, or empty list if all OK."""
    reasons = []

    # arm-lifter runs natively — check on the host
    if not Path(ARM_LIFTER).exists():
        reasons.append(
            f"arm-lifter not found at {ARM_LIFTER} "
            "(set ARM_LIFTER env var or run ./build.sh)"
        )

    # VM infrastructure
    if IS_DARWIN:
        if shutil.which("lima") is None:
            reasons.append("lima not found on PATH")
            return reasons  # cannot check further
    elif IS_WINDOWS:
        if shutil.which("wsl") is None:
            reasons.append("wsl not found (install WSL)")
            return reasons  # cannot check further

    # Tools that must be available inside the Linux VM
    vm_tools = [CLANG, LLVM_DIS, LLC, "qemu-aarch64", "qemu-x86_64"]
    for tool in vm_tools:
        if not _check_tool_in_vm(tool):
            reasons.append(
                f"{tool} not found in Linux VM "
                "(install: sudo apt install clang llvm qemu-user)"
            )

    return reasons


# --- Pytest fixtures (only loaded when running tests) ---

try:
    import pytest
except ImportError:
    pass
else:

    @pytest.fixture
    def workdir():
        """Per-test working directory under project tree (visible to all runners)."""
        d = BUILD_DIR / f"test-{uuid.uuid4().hex[:12]}"
        d.mkdir(parents=True, exist_ok=True)
        yield d
        shutil.rmtree(d)

    def pytest_configure(config):
        """Check prerequisites once at session start."""
        config._arm_lifter_skip_reasons = check_prerequisites()

    def pytest_sessionstart(session):
        """Abort with a clear message if prerequisites are not met."""
        reasons = getattr(session.config, "_arm_lifter_skip_reasons", [])
        if reasons:
            msg = "; ".join(reasons)
            pytest.exit(f"\n!!! {msg}\n")
