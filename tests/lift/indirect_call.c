// Minimal repro: indirect call (blr xN) — arm-lifter fails with
// "OOPS: no debuginfo mapping exists" / "Can't process BR/BLR instruction"
//
// Compile: zig cc -target aarch64-linux -fno-sanitize=all -O0 -c indirect_call.c -o indirect_call.o

typedef int (*fn_ptr_t)(int, int);

int add(int a, int b) { return a + b; }
int sub(int a, int b) { return a - b; }

int call_via_ptr(fn_ptr_t fn, int x, int y) {
    return fn(x, y);  // → blr xN on AArch64
}

int dispatcher(int op, int x, int y) {
    fn_ptr_t f = (op == 1) ? add : sub;
    return call_via_ptr(f, x, y);
}
