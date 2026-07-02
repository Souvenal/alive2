---
name: build
description: Build arm-lifter and related targets using CMake + Ninja. Invoked automatically when you detect the build is needed or when a build-related command fails.
---

# /build — Build arm-lifter

## Prerequisite: Local LLVM build

arm-lifter requires a local `llvm-project` checkout with `release/22.x`, built with RTTI enabled. The repo expects it as a **sibling directory** to `alive2/`.

If not present:
```bash
cd ..  # sibling of alive2/
git clone --branch release/22.x --depth 1 https://github.com/llvm/llvm-project.git
cd llvm-project
cmake -B build -S llvm -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_RTTI=ON \
  -DBUILD_SHARED_LIBS=ON \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_ENABLE_PROJECTS="llvm"
cmake --build build
```

## Build arm-lifter

```bash
./build.sh
```

`build.sh` uses `LOCAL_LLVM` (default: `./llvm-project`) to set `CMAKE_PREFIX_PATH`. Pass extra CMake args via `$@`.

## Manual CMake configure (alternative)

```bash
LOCAL_LLVM=${LOCAL_LLVM:-../llvm-project}
cmake -B build -S . \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.12 \
  -DCMAKE_PREFIX_PATH=$LOCAL_LLVM/build \
  -DBUILD_TV=1 \
  -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --target arm-lifter
```

## Environment

- `LOCAL_LLVM` — path to shared LLVM checkout (default: `<workspace>/../llvm-project`)

## Build target

The primary target is `arm-lifter`. The CMake project also includes other tools (`alive`, `alive-tv`, `backend-tv`) if built without target restriction.

## Selective rebuild

After initial build, just re-run ninja on the target:
```bash
cmake --build build --target arm-lifter
```
