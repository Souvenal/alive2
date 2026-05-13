// Indirect call via function pointer (blr xN) — tests arm-lifter's indirect
// call handling. Calls add/sub through a function pointer via dispatcher.
// No variadic functions (printf) — writes raw bytes to stdout so the lifter
// only needs to handle blr xN, not varargs.

#include <unistd.h>

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

static void print_int(int n) {
    char buf[16];
    int len = 0;
    if (n < 0) { buf[len++] = '-'; n = -n; }
    if (n == 0) { buf[len++] = '0'; }
    else {
        char rev[16];
        int rlen = 0;
        while (n > 0) { rev[rlen++] = '0' + (n % 10); n /= 10; }
        while (rlen > 0) buf[len++] = rev[--rlen];
    }
    write(1, buf, len);
}

int main(void) {
    int r1 = dispatcher(1, 10, 20);  // add -> 30
    int r2 = dispatcher(0, 50, 3);   // sub -> 47
    print_int(r1);
    write(1, " ", 1);
    print_int(r2);
    write(1, "\n", 1);
    return r1 + r2;  // 77
}
