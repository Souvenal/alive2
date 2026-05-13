#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* ---- Small struct: fits in registers (AAPCS64: <=16 bytes) ---- */

typedef struct {
  int x;
  int y;
} Point;

/* ---- Medium struct: three 64-bit fields = 24 bytes, passed on stack ---- */

typedef struct {
  long a;
  long b;
  long c;
} Triple;

/* ---- Mixed struct: int + pointer ---- */

typedef struct {
  int count;
  char *name;
} NamedCount;

/* ---- Struct with array ---- */

typedef struct {
  int values[4];
  int sum;
} IntVec;

/* ---- Struct returned by value ---- */

Point make_point(int x, int y) {
  Point p;
  p.x = x;
  p.y = y;
  return p;
}

Point add_points(Point a, Point b) {
  Point r;
  r.x = a.x + b.x;
  r.y = a.y + b.y;
  return r;
}

/* ---- Triple: returned via sret pointer (AAPCS >16 bytes) ---- */

Triple make_triple(long a, long b, long c) {
  Triple t;
  t.a = a;
  t.b = b;
  t.c = c;
  return t;
}

Triple scale_triple(Triple t, long factor) {
  Triple r;
  r.a = t.a * factor;
  r.b = t.b * factor;
  r.c = t.c * factor;
  return r;
}

/* ---- Struct parameter with pointer member ---- */

int get_count(NamedCount nc) {
  return nc.count;
}

int name_length(NamedCount nc) {
  if (nc.name == 0)
    return 0;
  return (int)strlen(nc.name);
}

/* ---- Struct containing array ---- */

IntVec make_vec(int a, int b, int c, int d) {
  IntVec v;
  v.values[0] = a;
  v.values[1] = b;
  v.values[2] = c;
  v.values[3] = d;
  v.sum = a + b + c + d;
  return v;
}

int vec_total(IntVec v) {
  return v.sum;
}

/* ---- Simple I/O without variadic functions ---- */

static void print_char(char c) {
  write(STDOUT_FILENO, &c, 1);
}

static void print_int(int n) {
  char buf[16];
  int i = 0;

  if (n < 0) {
    print_char('-');
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

  while (i > 0)
    print_char(buf[--i]);
}

static void print_str(const char *s) {
  write(STDOUT_FILENO, s, strlen(s));
}

static void print_long(long n) {
  char buf[32];
  int i = 0;

  if (n < 0) {
    print_char('-');
    n = -n;
  }

  if (n == 0) {
    print_char('0');
    return;
  }

  while (n > 0) {
    buf[i++] = '0' + (int)(n % 10);
    n /= 10;
  }

  while (i > 0)
    print_char(buf[--i]);
}

/* ---- Main: exercise struct param / return ---- */

int main(void) {
  Point p1, p2, psum;
  Triple t1, t2;
  NamedCount nc;
  IntVec v;

  /* Test small struct return */
  p1 = make_point(10, 20);
  p2 = make_point(30, 40);
  print_str("p1: ");
  print_int(p1.x);
  print_char(',');
  print_int(p1.y);
  print_char('\n');
  print_str("p2: ");
  print_int(p2.x);
  print_char(',');
  print_int(p2.y);
  print_char('\n');

  /* Test small struct param + return */
  psum = add_points(p1, p2);
  print_str("psum: ");
  print_int(psum.x);
  print_char(',');
  print_int(psum.y);
  print_char('\n');

  /* Test medium struct return (sret) */
  t1 = make_triple(1, 2, 3);
  print_str("t1: ");
  print_long(t1.a);
  print_char(',');
  print_long(t1.b);
  print_char(',');
  print_long(t1.c);
  print_char('\n');

  /* Test medium struct param + return */
  t2 = scale_triple(t1, 5);
  print_str("t2 (t1 * 5): ");
  print_long(t2.a);
  print_char(',');
  print_long(t2.b);
  print_char(',');
  print_long(t2.c);
  print_char('\n');

  /* Test mixed struct */
  nc.count = 42;
  nc.name = (char*)"hello_struct_test";
  print_str("nc.count: ");
  print_int(get_count(nc));
  print_char('\n');
  print_str("nc.namelen: ");
  print_int(name_length(nc));
  print_char('\n');

  /* Test struct with array */
  v = make_vec(1, 2, 3, 4);
  print_str("vec_total: ");
  print_int(vec_total(v));
  print_char('\n');
  print_str("v.values[0]: ");
  print_int(v.values[0]);
  print_char('\n');
  print_str("v.values[3]: ");
  print_int(v.values[3]);
  print_char('\n');

  return psum.x + psum.y;  /* 10+30 + 20+40 = 100 */
}