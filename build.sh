#!/bin/bash

set -e -o pipefail

cd "$(dirname "$0")"

# ── Local LLVM path ──────────────────────────────────────
# Default: llvm-project/ as a sibling directory (shared across repos).
# Override with LOCAL_LLVM if cloned elsewhere.
LOCAL_LLVM="${LOCAL_LLVM:-$(pwd)/../llvm-project}"
CMAKE_PREFIX_PATH="$LOCAL_LLVM/build"

# ── CMake Configure ──────────────────────────────────────
cmake -B build -S . \
  -DCMAKE_PREFIX_PATH="$CMAKE_PREFIX_PATH" \
  -DBUILD_TV=1 \
  -DCMAKE_BUILD_TYPE=Release \
  "$@"

# ── Build ────────────────────────────────────────────────
cmake --build build \
  --config Release \
  --target arm-lifter machine-cfg-dump \
  -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu)"
