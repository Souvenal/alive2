#!/usr/bin/env bash
# Instruction count comparison: source-compiled ARM64 vs lifted ARM64
#
# Compares:
#   1. C source → clang → ARM64 .o        (reference, 1 pass)
#   2. C source → clang → .bc + .o → arm-lifter → .ll → llc → ARM64 .s  (2 passes)
#
# Usage:
#   ./run.sh [case_name]
#
# Default case: compiler_rt_int128.
# On Linux, requires LLVM_VERSION or LLVM_BIN for LLVM 20 or newer.
# ARM_LIFTER env var overrides the arm-lifter path (default: repo build/).

set -e -o pipefail

cd "$(dirname "$0")"

REPO="$(cd ../../../.. && pwd)"
ARM_LIFTER="${ARM_LIFTER:-${REPO}/build/Release/arm-lifter}"
if [[ "$(uname -s)" == Linux
      && -z "${LLVM_BIN:-}" && -z "${LLVM_VERSION:-}" ]]; then
  echo "ERROR: set LLVM_VERSION (for example, 20) or LLVM_BIN" >&2
  exit 1
fi

LLVM_SUFFIX=""
if [[ -n "${LLVM_VERSION:-}" ]]; then
  if [[ ! "${LLVM_VERSION}" =~ ^[0-9]+$ ]]; then
    echo "ERROR: LLVM_VERSION must be a numeric major version" >&2
    exit 1
  fi
  LLVM_SUFFIX="-${LLVM_VERSION}"
fi

if [[ -n "${LLVM_BIN:-}" ]]; then
  for tool in clang llvm-config llvm-objdump llvm-dis llc opt; do
    if [[ ! -x "${LLVM_BIN}/${tool}${LLVM_SUFFIX}" ]]; then
      echo "ERROR: '${LLVM_BIN}/${tool}${LLVM_SUFFIX}' is not executable" >&2
      exit 1
    fi
  done
  CLANG="${LLVM_BIN}/clang${LLVM_SUFFIX}"
  LLVM_OBJDUMP="${LLVM_BIN}/llvm-objdump${LLVM_SUFFIX}"
  LLVM_DIS="${LLVM_BIN}/llvm-dis${LLVM_SUFFIX}"
  LLC="${LLVM_BIN}/llc${LLVM_SUFFIX}"
  OPT="${LLVM_BIN}/opt${LLVM_SUFFIX}"
  SELECTED_LLVM_VERSION=$("${LLVM_BIN}/llvm-config${LLVM_SUFFIX}" --version)
elif [[ -n "${LLVM_VERSION:-}" ]]; then
  for tool in clang llvm-config llvm-objdump llvm-dis llc opt; do
    if ! command -v "${tool}${LLVM_SUFFIX}" &>/dev/null; then
      echo "ERROR: '${tool}${LLVM_SUFFIX}' not found on PATH" >&2
      exit 1
    fi
  done
  CLANG="clang${LLVM_SUFFIX}"
  LLVM_OBJDUMP="llvm-objdump${LLVM_SUFFIX}"
  LLVM_DIS="llvm-dis${LLVM_SUFFIX}"
  LLC="llc${LLVM_SUFFIX}"
  OPT="opt${LLVM_SUFFIX}"
  SELECTED_LLVM_VERSION=$("llvm-config${LLVM_SUFFIX}" --version)
else
  for tool in clang llvm-objdump llvm-dis llc opt; do
    if ! command -v "$tool" &>/dev/null; then
      echo "ERROR: '$tool' not found on PATH" >&2
      exit 1
    fi
  done
  CLANG=clang
  LLVM_OBJDUMP=llvm-objdump
  LLVM_DIS=llvm-dis
  LLC=llc
  OPT=opt
  SELECTED_LLVM_VERSION=""
fi

if [[ -n "${SELECTED_LLVM_VERSION}" ]]; then
  if [[ ! "${SELECTED_LLVM_VERSION%%.*}" =~ ^[0-9]+$
        || "${SELECTED_LLVM_VERSION%%.*}" -lt 20 ]]; then
    echo "ERROR: LLVM toolchain must be at least LLVM 20.x, found ${SELECTED_LLVM_VERSION}" >&2
    exit 1
  fi
  if [[ -n "${LLVM_VERSION:-}"
        && "${SELECTED_LLVM_VERSION%%.*}" != "${LLVM_VERSION}" ]]; then
    echo "ERROR: LLVM_VERSION=${LLVM_VERSION} selected LLVM ${SELECTED_LLVM_VERSION}" >&2
    exit 1
  fi
fi

CASE="${1:-compiler_rt_int128}"
OUTDIR="out_${CASE}"
mkdir -p "${OUTDIR}"

# ── Step 1: compile C → .bc + .o ────────────────────────────────
echo "== Step 1: compile C to .bc and .o"
"${CLANG}" -target aarch64-linux -fno-sanitize=all -O0 \
  -c "${CASE}.c" -o "${OUTDIR}/${CASE}.o" 2>&1
"${CLANG}" -target aarch64-linux -fno-sanitize=all -O0 \
  -emit-llvm -c "${CASE}.c" -o "${OUTDIR}/${CASE}.bc" 2>&1

# ── Step 2: .bc → .ll  (source IR for side-by-side) ────────────
echo "== Step 2: bitcode to readable IR"
"${LLVM_DIS}" "${OUTDIR}/${CASE}.bc" -o "${OUTDIR}/${CASE}.ll" 2>/dev/null || true
if [ ! -s "${OUTDIR}/${CASE}.ll" ]; then
  "${LLC}" -O0 -march=aarch64 "${OUTDIR}/${CASE}.bc" \
    -stop-after=ir -o "${OUTDIR}/${CASE}.ll" 2>/dev/null || true
fi

# ── Step 3: arm-lifter: .o + .bc → lifted.ll ────────────────────
echo "== Step 3: arm-lifter"
${ARM_LIFTER} "${OUTDIR}/${CASE}.o" \
  --src-bc="${OUTDIR}/${CASE}.bc" \
  -o "${OUTDIR}/${CASE}.lifted.ll" 2>&1 | \
  grep -v '^warning:' > "${OUTDIR}/${CASE}.lift.log" || true

# ── Step 4: disassemble original ARM64 .o ────────────────────────
echo "== Step 4: disassemble original ARM64 .o"
"${LLVM_OBJDUMP}" -d "${OUTDIR}/${CASE}.o" > "${OUTDIR}/${CASE}_arm64.s" 2>&1

# ── Step 5: lifted.ll → ARM64 .o → disassemble ──────────────────
echo "== Step 5: lifted IR -> ARM64 .o -> disassemble"
"${LLC}" -O0 -march=aarch64 -filetype=obj "${OUTDIR}/${CASE}.lifted.ll" \
  -o "${OUTDIR}/${CASE}_lifted_to_arm64.o" 2>/dev/null
"${LLVM_OBJDUMP}" -d "${OUTDIR}/${CASE}_lifted_to_arm64.o" \
  > "${OUTDIR}/${CASE}_lifted_to_arm64.s" 2>&1
rm -f "${OUTDIR}/${CASE}_lifted_to_arm64.o"

# ── Step 6: opt -O2 lifted.ll → ARM64 .o → disassemble ──────────
echo "== Step 6: opt -O2 lifted IR -> ARM64 .o -> disassemble"
"${OPT}" -O2 "${OUTDIR}/${CASE}.lifted.ll" -S \
  -o "${OUTDIR}/${CASE}.lifted.opt.ll" 2>/dev/null || true
"${LLC}" -O0 -march=aarch64 -filetype=obj "${OUTDIR}/${CASE}.lifted.opt.ll" \
  -o "${OUTDIR}/${CASE}_opt_to_arm64.o" 2>/dev/null
"${LLVM_OBJDUMP}" -d "${OUTDIR}/${CASE}_opt_to_arm64.o" \
  > "${OUTDIR}/${CASE}_opt_to_arm64.s" 2>&1
rm -f "${OUTDIR}/${CASE}_opt_to_arm64.o"

# ── Counts ───────────────────────────────────────────────────────
COUNT='^\s+[0-9a-f]+:\s+[0-9a-f]+\s'
SRC_INSNS=$(grep -cE "${COUNT}" "${OUTDIR}/${CASE}_arm64.s" 2>/dev/null || echo 0)
LIFT_INSNS=$(grep -cE "${COUNT}" "${OUTDIR}/${CASE}_lifted_to_arm64.s" 2>/dev/null || echo 0)
OPT_INSNS=$(grep -cE "${COUNT}" "${OUTDIR}/${CASE}_opt_to_arm64.s" 2>/dev/null || echo 0)
SRC_IR_LINES=$(wc -l < "${OUTDIR}/${CASE}.ll" 2>/dev/null || echo 0)
LIFT_IR_LINES=$(wc -l < "${OUTDIR}/${CASE}.lifted.ll" 2>/dev/null || echo 0)

# ── Summary ──────────────────────────────────────────────────────
cat << SUMMARY

============================================
  Instruction Count Comparison: ${CASE}
============================================

  Source IR (.ll) lines                  ${SRC_IR_LINES}
  Lifted IR (.ll) lines                  ${LIFT_IR_LINES}

  Original ARM64 .o (insns)              ${SRC_INSNS}
  Lifted IR -> ARM64 (insns)             ${LIFT_INSNS}
  opt -O2 lifted -> ARM64 (insns)        ${OPT_INSNS}

SUMMARY

if command -v bc &>/dev/null; then
  RATIO=$(echo "scale=1; ${LIFT_INSNS} / ${SRC_INSNS}" | bc)
  echo "  Ratio (lifted/original)              ${RATIO}x"
  echo ""
fi

echo "--- Original ARM64 instruction categories ---"
"${LLVM_OBJDUMP}" -d "${OUTDIR}/${CASE}.o" 2>/dev/null | \
  grep -E '^\s+[0-9a-f]+:\s+[0-9a-f]+\s' | \
  awk '{print $3}' | sort | uniq -c | sort -rn
echo ""
echo "--- Lifted -> ARM64 instruction categories ---"
grep -E "${COUNT}" "${OUTDIR}/${CASE}_lifted_to_arm64.s" | \
  awk '{print $3}' | sort | uniq -c | sort -rn
echo ""

echo "============================================"
echo "  Output files in ${OUTDIR}/"
echo "============================================"
for f in "${OUTDIR}"/*; do
  echo "  $(basename "${f}")"
done
echo ""
echo "To view the disassembly side-by-side:"
echo "  open ${OUTDIR}/${CASE}_arm64.s"
echo "  open ${OUTDIR}/${CASE}_lifted_to_arm64.s"
