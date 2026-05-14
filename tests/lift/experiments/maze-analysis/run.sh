#!/usr/bin/env bash
# Maze instruction count analysis
#
# Compares source-compiled ARM64 vs lifted ARM64 for the Maze game cases.
# Maze is a non-trivial real-world program (maze game with backtracking),
# giving a representative picture of the lifter's assembly bloat.
#
# Usage:
#   ./run.sh [Maze | Maze_novarargs]

set -e -o pipefail

cd "$(dirname "$0")"

REPO="$(cd ../../../.. && pwd)"
ARM_LIFTER="${ARM_LIFTER:-${REPO}/build/Release/arm-lifter}"
ZIG="${ZIG:-zig}"

for tool in llvm-objdump llvm-dis llc opt; do
  if ! command -v "$tool" &>/dev/null; then
    echo "ERROR: '$tool' not found on PATH" >&2
    exit 1
  fi
done

CASE="${1:-Maze_novarargs}"
OUTDIR="out_${CASE}"
mkdir -p "${OUTDIR}"

echo "== Step 1: compile C -> .bc and .o"
${ZIG} cc -target aarch64-linux -fno-sanitize=all -O0 \
  -c "${CASE}.c" -o "${OUTDIR}/${CASE}.o" 2>&1
${ZIG} cc -target aarch64-linux -fno-sanitize=all -O0 \
  -emit-llvm -c "${CASE}.c" -o "${OUTDIR}/${CASE}.bc" 2>&1

echo "== Step 2: bitcode -> readable IR"
llvm-dis "${OUTDIR}/${CASE}.bc" -o "${OUTDIR}/${CASE}.ll" 2>/dev/null || true
if [ ! -s "${OUTDIR}/${CASE}.ll" ]; then
  llc -O0 -march=aarch64 "${OUTDIR}/${CASE}.bc" \
    -stop-after=ir -o "${OUTDIR}/${CASE}.ll" 2>/dev/null || true
fi

echo "== Step 3: arm-lifter"
${ARM_LIFTER} "${OUTDIR}/${CASE}.o" \
  --src-bc="${OUTDIR}/${CASE}.bc" \
  -o "${OUTDIR}/${CASE}.lifted.ll" 2>&1 | \
  grep -v '^warning:' > "${OUTDIR}/${CASE}.lift.log" || true

echo "== Step 4: disassemble original ARM64 .o"
llvm-objdump -d "${OUTDIR}/${CASE}.o" > "${OUTDIR}/${CASE}_arm64.s" 2>&1

echo "== Step 5: lifted IR -> ARM64 .o -> disassemble"
llc -O0 -march=aarch64 -filetype=obj "${OUTDIR}/${CASE}.lifted.ll" \
  -o "${OUTDIR}/${CASE}_lifted_to_arm64.o" 2>/dev/null
llvm-objdump -d "${OUTDIR}/${CASE}_lifted_to_arm64.o" \
  > "${OUTDIR}/${CASE}_lifted_to_arm64.s" 2>&1
rm -f "${OUTDIR}/${CASE}_lifted_to_arm64.o"

echo "== Step 6: opt -O2 lifted IR -> ARM64 .o -> disassemble"
opt -O2 "${OUTDIR}/${CASE}.lifted.ll" -S \
  -o "${OUTDIR}/${CASE}.lifted.opt.ll" 2>/dev/null || true
llc -O0 -march=aarch64 -filetype=obj "${OUTDIR}/${CASE}.lifted.opt.ll" \
  -o "${OUTDIR}/${CASE}_opt_to_arm64.o" 2>/dev/null
llvm-objdump -d "${OUTDIR}/${CASE}_opt_to_arm64.o" \
  > "${OUTDIR}/${CASE}_opt_to_arm64.s" 2>&1
rm -f "${OUTDIR}/${CASE}_opt_to_arm64.o"

# ── Counts ───────────────────────────────────────────────────────
COUNT_RE='^\s+[0-9a-f]+:\s+[0-9a-f]+\s'
SRC_INSNS=$(grep -cE "${COUNT_RE}" "${OUTDIR}/${CASE}_arm64.s" 2>/dev/null || echo 0)
LIFT_INSNS=$(grep -cE "${COUNT_RE}" "${OUTDIR}/${CASE}_lifted_to_arm64.s" 2>/dev/null || echo 0)
OPT_INSNS=$(grep -cE "${COUNT_RE}" "${OUTDIR}/${CASE}_opt_to_arm64.s" 2>/dev/null || echo 0)
SRC_IR_LINES=$(wc -l < "${OUTDIR}/${CASE}.ll" 2>/dev/null || echo 0)
LIFT_IR_LINES=$(wc -l < "${OUTDIR}/${CASE}.lifted.ll" 2>/dev/null || echo 0)

# ── Per-function breakdown ───────────────────────────────────────
echo ""
echo "--- Per-function instruction breakdown ---"
for func in $(grep '^[0-9a-f]\{16\} <.*>:' "${OUTDIR}/${CASE}_arm64.s" | \
              sed 's/.*<\(.*\)>:/\1/'); do
  src=$(awk "/^[0-9a-f]+ <${func}>:/" ORS='//' RS='\n\n' \
    "${OUTDIR}/${CASE}_arm64.s" 2>/dev/null | \
    grep -cE "${COUNT_RE}" 2>/dev/null || echo 0)
  # for lifted, find any .text section
  lifted=$(awk "/^[0-9a-f]+ <${func}>:/" ORS='//' RS='\n\n' \
    "${OUTDIR}/${CASE}_lifted_to_arm64.s" 2>/dev/null | \
    grep -cE "${COUNT_RE}" 2>/dev/null || echo 0)
  if [ "$src" -gt 0 ] || [ "$lifted" -gt 0 ]; then
    printf "  %-30s  src=%4d  lifted=%4d\n" "${func}" "${src}" "${lifted}"
  fi
done

# ── Summary ──────────────────────────────────────────────────────
cat << SUMMARY

============================================
  Maze Analysis: ${CASE}
============================================

  Source IR (.ll) lines                  ${SRC_IR_LINES}
  Lifted IR (.ll) lines                  ${LIFT_IR_LINES}

  Original ARM64 .o (insns)              ${SRC_INSNS}
  Lifted IR -> ARM64 (insns)             ${LIFT_INSNS}
  opt -O2 lifted -> ARM64 (insns)        ${OPT_INSNS}

SUMMARY

if command -v bc &>/dev/null; then
  RATIO=$(echo "scale=2; ${LIFT_INSNS} / ${SRC_INSNS}" | bc)
  echo "  Overall ratio (lifted/original)        ${RATIO}x"
  echo ""
fi

echo "--- Original ARM64 instruction categories ---"
llvm-objdump -d "${OUTDIR}/${CASE}.o" 2>/dev/null | \
  grep -E "${COUNT_RE}" | \
  awk '{print $3}' | sort | uniq -c | sort -rn
echo ""
echo "--- Lifted -> ARM64 instruction categories ---"
grep -E "${COUNT_RE}" "${OUTDIR}/${CASE}_lifted_to_arm64.s" | \
  awk '{print $3}' | sort | uniq -c | sort -rn
echo ""

echo "--- Summary from lift log ---"
grep -i 'function\|Done\|encoding\|error' "${OUTDIR}/${CASE}.lift.log" || true
echo ""

echo "Output files in ${OUTDIR}/:"
ls "${OUTDIR}"
