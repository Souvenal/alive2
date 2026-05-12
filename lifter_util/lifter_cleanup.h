#pragma once

// Copyright (c) 2026-present Souvenal
// Distributed under the MIT license that can be found in the LICENSE file.
//
// Safe cleanup passes for lifted IR. Runs only function-local passes
// (no inlining, no IPO, no function-attrs inference) to preserve the
// original call structure and semantic equivalence with the ARM object.

namespace llvm {
class Module;
}

namespace lifter {
void cleanup_module(llvm::Module &M);
}
