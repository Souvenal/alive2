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

# --- Tool configuration ---
# The tests cross-compile to AArch64 ELF (Linux). Only Linux has a native
# cross-compiler toolchain available; all other platforms (macOS, Windows, etc.)
# must use zig cc which bundles its own Linux sysroot.
ARM_LIFTER = os.environ.get(
    "ARM_LIFTER", str(PROJECT_ROOT / "build/Release/arm-lifter")
)
if IS_LINUX:
    CC = os.environ.get("CC", "gcc")
else:
    CC = "zig cc"
CC_ARGS = CC.split()
CFLAGS = os.environ.get(
    "CFLAGS", "-target aarch64-linux -fno-sanitize=all -O2"
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


# --- Cross-platform binary runners ---
# All test binaries are Linux ELF. Both the AArch64 reference and the x86_64
# subject always run under QEMU to guarantee identical execution environment
# regardless of the host's native architecture:
#
#   Platform    | AArch64 binary                     | x86_64 binary
#   ------------|------------------------------------|----------------------------------
#   macOS       | lima -- qemu-aarch64 <binary>      | lima -- qemu-x86_64 <binary>
#   Linux       | qemu-aarch64 <binary>              | qemu-x86_64 <binary>
#   Windows     | wsl -- qemu-aarch64 <wsl-path>     | wsl -- qemu-x86_64 <wsl-path>


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
    """Run an AArch64 Linux binary under QEMU."""
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
    """Run an x86_64 Linux binary under QEMU."""
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

def compile_bc_and_o(src: Path, workdir: Path) -> tuple[Path, Path]:
    """Compile <src> to .bc and .o in <workdir>. Returns (bc_path, o_path)."""
    base = workdir / src.stem
    bc = base.with_suffix(".bc")
    o = base.with_suffix(".o")
    cmd = CC_ARGS + CFLAGS

    r = run_command(cmd + ["-emit-llvm", "-c", str(src), "-o", str(bc)])
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to compile .bc for {src.name}:\n{r.stderr}\n{r.stdout}"
        )

    r = run_command(cmd + ["-c", str(src), "-o", str(o)])
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to compile .o for {src.name}:\n{r.stderr}\n{r.stdout}"
        )
    return bc, o


def run_arm_lifter(o: Path, bc: Path, workdir: Path) -> Path:
    """Run arm-lifter on <o> with --src-bc=<bc>. Returns path to lifted .ll."""
    output = workdir / f"{o.stem}.lifted.ll"
    r = run_command(
        [ARM_LIFTER, str(o), "--src-bc", str(bc), "-o", str(output)]
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"arm-lifter failed on {o.name}:\n{r.stderr}\n{r.stdout}"
        )
    return output


def compile_arm64_binary(src: Path, workdir: Path) -> Path:
    """Compile src to a native ARM64 static binary."""
    output = workdir / f"{src.stem}_arm64"
    r = run_command(
        CC_ARGS + CFLAGS + ["-static", str(src), "-o", str(output)]
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to compile ARM64 binary for {src.name}:\n{r.stderr}\n{r.stdout}"
        )
    return output


def recompile_x86_64(ll_path: Path, workdir: Path) -> Path:
    """Recompile lifted .ll to an x86_64 static binary."""
    o_path = workdir / f"{ll_path.stem}.o"
    output = workdir / f"{ll_path.stem}_x86_64"

    r = run_command(
        CC_ARGS + ["-target", "x86_64-linux-musl", "-c",
                    str(ll_path), "-o", str(o_path)]
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to compile lifted .ll to .o:\n{r.stderr}\n{r.stdout}"
        )

    r = run_command(
        CC_ARGS + ["-target", "x86_64-linux-musl", "-static",
                    str(o_path), "-o", str(output)]
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


def _in_vm(vm: str, cmd: list[str]) -> bool:
    """Return True if <cmd> succeeds inside the VM (lima or wsl)."""
    try:
        return run_command([vm, "--"] + cmd, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def check_prerequisites() -> list[str]:
    """Return list of skip reasons, or empty list if all OK."""
    reasons = []

    if not Path(ARM_LIFTER).exists():
        reasons.append(
            f"arm-lifter not found at {ARM_LIFTER} "
            "(set ARM_LIFTER env var or run ./build.sh)"
        )

    # Compiler
    compiler = CC.split()[0]
    if shutil.which(compiler) is None:
        reasons.append(
            f"Compiler '{compiler}' not found on PATH"
        )

    # Platform-specific runtime (both binaries always run under QEMU)
    if IS_DARWIN:
        if shutil.which("lima") is None:
            reasons.append("lima not found on PATH")
        else:
            for qemu in ("qemu-aarch64", "qemu-x86_64"):
                if not _in_vm("lima", ["which", qemu]):
                    reasons.append(
                        f"{qemu} not found in Lima VM "
                        "(install: lima -- sudo apt install qemu-user)"
                    )

    elif IS_LINUX:
        for qemu in ("qemu-aarch64", "qemu-x86_64"):
            if shutil.which(qemu) is None:
                reasons.append(f"{qemu} not found on PATH (install qemu-user)")

    else:  # Windows
        if shutil.which("wsl") is None:
            reasons.append("wsl not found (install WSL)")
        else:
            for qemu in ("qemu-aarch64", "qemu-x86_64"):
                if not _in_vm("wsl", ["which", qemu]):
                    reasons.append(
                        f"{qemu} not found in WSL "
                        "(install: wsl -- sudo apt install qemu-user)"
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
