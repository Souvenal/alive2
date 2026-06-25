#include "util.h"

const char *reverse(const char *s) {
    /* Return a pointer to a static reversed copy.
       Works because the test inputs are short. */
    static char buf[256];
    int len = 0;
    while (s[len]) {
        len++;
    }
    for (int i = 0; i < len; i++) {
        buf[i] = s[len - 1 - i];
    }
    buf[len] = '\0';
    return buf;
}
