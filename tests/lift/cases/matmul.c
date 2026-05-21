/*
 * Matrix multiplication benchmark for arm-lifter instruction bloat analysis.
 *
 * Computes C = A * B for 8x8 integer matrices, then prints a checksum.
 * Uses only integer arithmetic and write() — no variadic functions.
 */

#include <unistd.h>
#include <string.h>

#define N 8

static void print_char(char c) {
  write(STDOUT_FILENO, &c, 1);
}

static void print_int(int n) {
  char buf[16];
  int i = 0;
  int neg = 0;

  if (n < 0) {
    neg = 1;
    n = -n;
  }

  if (n == 0) {
    print_char('0');
    return;
  }

  while (n > 0) {
    buf[i++] = '0' + (n % 10);
    n /= 10;
  }

  if (neg)
    print_char('-');

  while (i > 0)
    print_char(buf[--i]);
}

static void print_str(const char *s) {
  write(STDOUT_FILENO, s, strlen(s));
}

int matmul(int a[N][N], int b[N][N], int c[N][N]) {
  int i, j, k;
  for (i = 0; i < N; i++) {
    for (j = 0; j < N; j++) {
      int sum = 0;
      for (k = 0; k < N; k++) {
        sum += a[i][k] * b[k][j];
      }
      c[i][j] = sum;
    }
  }
  return 0;
}

int checksum(int m[N][N]) {
  int sum = 0;
  int i, j;
  for (i = 0; i < N; i++) {
    for (j = 0; j < N; j++) {
      sum += m[i][j];
    }
  }
  return sum;
}

int main(int argc, char *argv[]) {
  int a[N][N] = {
    {1, 2, 3, 4, 5, 6, 7, 8},
    {8, 7, 6, 5, 4, 3, 2, 1},
    {1, 0, 1, 0, 1, 0, 1, 0},
    {0, 1, 0, 1, 0, 1, 0, 1},
    {2, 2, 2, 2, 2, 2, 2, 2},
    {3, 3, 3, 3, 3, 3, 3, 3},
    {4, 4, 4, 4, 4, 4, 4, 4},
    {5, 5, 5, 5, 5, 5, 5, 5},
  };

  int b[N][N] = {
    {1, 0, 0, 0, 0, 0, 0, 0},
    {0, 1, 0, 0, 0, 0, 0, 0},
    {0, 0, 1, 0, 0, 0, 0, 0},
    {0, 0, 0, 1, 0, 0, 0, 0},
    {0, 0, 0, 0, 1, 0, 0, 0},
    {0, 0, 0, 0, 0, 1, 0, 0},
    {0, 0, 0, 0, 0, 0, 1, 0},
    {0, 0, 0, 0, 0, 0, 0, 1},
  };

  int c[N][N];

  (void)argc;
  (void)argv;

  matmul(a, b, c);

  print_str("checksum=");
  print_int(checksum(c));
  print_char('\n');

  return 0;
}
