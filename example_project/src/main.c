#include <stdio.h>
#include "calc.h"
#include "util.h"

int main(void) {
    int a = 42, b = 8;

    printf("add(%d, %d) = %d\n", a, b, add(a, b));
    printf("sub(%d, %d) = %d\n", a, b, sub(a, b));
    printf("mul(%d, %d) = %d\n", a, b, mul(a, b));
    printf("div(%d, %d) = %d\n", a, b, divide(a, b));

    printf("factorial(5) = %d\n", factorial(5));
    printf("gcd(%d, %d) = %d\n", a, b, gcd(a, b));

    const char *msg = "hello world";
    printf("reverse(\"%s\") = \"%s\"\n", msg, reverse(msg));

    return 0;
}
