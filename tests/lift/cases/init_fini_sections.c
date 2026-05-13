// Test program that generates .init_array and .fini_array sections
// via __attribute__((constructor)) and __attribute__((destructor)).
//
// Compile with:
//   zig cc -target aarch64-linux -fno-sanitize=all \
//     -c init_fini_sections.c -o init_fini_sections.o
//   zig cc -target aarch64-linux -fno-sanitize=all \
//     -emit-llvm -c init_fini_sections.c -o init_fini_sections.bc
//
// Verify sections:
//   llvm-readelf -S init_fini_sections.o | grep -E 'init_array|fini_array'

#include <stdint.h>

// Global state modified by constructor/destructor
static int32_t initialized_value = 0;
static int32_t cleanup_count = 0;

// Priority 101-65535: lower number = earlier execution
// constructor with priority runs before constructor without priority
__attribute__((constructor(101))) void early_init(void) {
    initialized_value = 42;
}

__attribute__((constructor)) void normal_init(void) {
    initialized_value += 8;
}

__attribute__((destructor(101))) void early_fini(void) {
    cleanup_count = 1;
}

__attribute__((destructor)) void normal_fini(void) {
    cleanup_count = 0;
}

// The actual function to lift — uses state set up by constructors
int get_initialized_value(void) {
    return initialized_value;
}

int get_cleanup_count(void) {
    return cleanup_count;
}

// A computation that depends on constructor-initialized state
int compute_with_init(int x) {
    return initialized_value + x;
}
