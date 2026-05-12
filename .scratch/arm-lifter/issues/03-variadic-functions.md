# Variadic functions not supported

Status: `needs-triage`

## Problem

The lifter cannot handle variadic calls (e.g., `printf`, `sprintf`). Variadic calling conventions pass arguments on the stack in a way the lifter does not model, leading to incorrect or missing arguments.

## Impact

Any program using variadic libc calls will fail to lift correctly. Non-variadic libc calls (e.g., `strlen`, `puts`) work when `--src-bc` is provided.

## Files involved

- `backend_tv/arm2llvm.cpp` — call instruction handling
- `backend_tv/mc2llvm.cpp` — argument marshalling
