// Test program that generates PGO-style .text.hot / .text.cold / .text.unlikely
// sections when compiled with profile-guided optimization or function
// partitioning attributes.
//
// Compile with:
//   zig cc -target aarch64-linux -fno-sanitize=all \
//     -freorder-blocks-and-partition -c pgo_sections.c -o pgo_sections.o
//   zig cc -target aarch64-linux -fno-sanitize=all \
//     -emit-llvm -c pgo_sections.c -o pgo_sections.bc
//
// Verify sections:
//   llvm-readelf -S pgo_sections.o | grep -E '\.text'

#include <stdint.h>

// __attribute__((hot)) tells the compiler this function is frequently executed.
// With -freorder-blocks-and-partition, it may be placed in .text.hot.
int hot_path(int x) {
    return x * x + x;
}

// __attribute__((cold)) tells the compiler this function is rarely executed.
// It may be placed in .text.cold or .text.unlikely.
int cold_path(int x) {
    if (x < 0)
        return -x;
    return x + 1;
}

// __attribute__((unlikely)) is another way to hint cold code.
int error_handler(int code) {
    // Simulate error handling
    volatile int dummy = code;
    return dummy;
}

// Main function that calls both hot and cold paths, making the branch
// profile obvious for PGO.
int compute(int n) {
    int sum = 0;
    for (int i = 0; i < n; i++) {
        if (i % 100 == 0) {
            sum += cold_path(i);    // 1% of iterations
        } else {
            sum += hot_path(i);     // 99% of iterations
        }

        if (sum < 0) {
            sum = error_handler(sum); // extremely rare
        }
    }
    return sum;
}
