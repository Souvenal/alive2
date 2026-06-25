# Example Project for arm-lifter

A minimal multi-file C project to demonstrate batch lifting with `lift_compile_commands.py`.

## Files

| File | Description |
|------|-------------|
| `CMakeLists.txt` | CMake build (sets `CMAKE_C_COMPILER=clang`) |
| `src/main.c` | Entry point — calls into calc and util |
| `src/calc.h` / `src/calc.c` | Arithmetic functions (add, sub, mul, div, factorial, gcd) |
| `src/util.h` / `src/util.c` | String reversal (stack buffer, basic pointer ops) |

## Build (native ARM64)

```bash
cd example_project
cmake -S . -B build -DCMAKE_C_COMPILER=clang -DCMAKE_BUILD_TYPE=Release
cmake --build build
./build/example
```

Expected output:
```
add(42, 8) = 50
sub(42, 8) = 34
mul(42, 8) = 336
div(42, 8) = 5
factorial(5) = 120
gcd(42, 8) = 2
reverse("hello world") = "dlrow olleh"
```

## Batch lifting

```bash
cd /path/to/alive2
python3 scripts/lift_compile_commands.py example_project/build/compile_commands.json \
    --arm-lifter=build/Release/arm-lifter \
    --link-output=example_project/example_lifted
```

**Note**: The lifted binary currently crashes (SIGSEGV) due to register-alloca modeling issues in the lifter (issue 21). The lifting pipeline itself succeeds — all 3 functions in all 3 translation units are lifted to valid LLVM IR without errors.
