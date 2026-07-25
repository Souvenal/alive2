#!/usr/bin/env bash

set -euo pipefail

usage() {
  echo "Usage: $0 <case> <function>" >&2
  echo "Environment: MCPU=generic MATTR='' MCA_ITERATIONS=100 OUTPUT_DIR=<path>" >&2
}

if [[ $# -ne 2 ]]; then
  usage
  exit 2
fi

CASE=$1
FUNCTION=$2

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
OUTPUT_DIR=${OUTPUT_DIR:-"$SCRIPT_DIR/output"}
MCPU=${MCPU:-generic}
MATTR=${MATTR:-}
MCA_ITERATIONS=${MCA_ITERATIONS:-100}

if [[ "$OUTPUT_DIR" != /* ]]; then
  OUTPUT_DIR="$PWD/$OUTPUT_DIR"
fi

ARM_LIFTER=${ARM_LIFTER:-"$PROJECT_ROOT/build/Release/arm-lifter"}
MACHINE_CFG_DUMP=${MACHINE_CFG_DUMP:-"$PROJECT_ROOT/build/Release/machine-cfg-dump"}

if [[ ! -x "$ARM_LIFTER" || ! -x "$MACHINE_CFG_DUMP" ]]; then
  if [[ -d "$PROJECT_ROOT/build" ]]; then
    cmake --build "$PROJECT_ROOT/build" \
      --config Release \
      --target arm-lifter machine-cfg-dump
  else
    "$PROJECT_ROOT/build.sh"
  fi
fi

export ARM_LIFTER
export MACHINE_CFG_DUMP

mkdir -p "$OUTPUT_DIR"
cd "$SCRIPT_DIR"

uv run python "$SCRIPT_DIR/dev.py" \
  --outdir "$OUTPUT_DIR" \
  full "$CASE"

REPORT="$OUTPUT_DIR/$CASE.$FUNCTION.block-comparison.json"

uv run python "$SCRIPT_DIR/cfg.py" \
  "$OUTPUT_DIR/$CASE.lifted.arm64.o" \
  --source-object "$OUTPUT_DIR/$CASE.o" \
  --asm-map "$OUTPUT_DIR/$CASE.asm-map.json" \
  --function "$FUNCTION" \
  --block-match \
  --mca \
  --mcpu "$MCPU" \
  --mattr "$MATTR" \
  --mca-iterations "$MCA_ITERATIONS" \
  --output-dir "$OUTPUT_DIR/cfg" \
  --relations-json "$REPORT"

echo
echo "Block comparison JSON: $REPORT"
