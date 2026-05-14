#include <stdio.h>

int main(void) {
  // Trigger __udivti3: AArch64 has no native 128-bit divide.
  // The backend lowers this to a __udivti3 call that only appears
  // in the .o, never in the .bc — exactly the issue 11 scenario.
  unsigned __int128 a =
      ((unsigned __int128)0x1234567890ABCDEFULL << 64) | 0xFEDCBA9876543210ULL;
  unsigned __int128 b = 0x1000000000000000ULL;
  unsigned __int128 div = a / b;

  // Also trigger __umodti3
  unsigned __int128 mod = a % b;

  // Print as two 64-bit halves so output is deterministic
  printf("%llx %llx %llx %llx\n",
         (unsigned long long)(div >> 64), (unsigned long long)div,
         (unsigned long long)(mod >> 64), (unsigned long long)mod);
  return 0;
}
