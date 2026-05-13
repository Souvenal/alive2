/*
 * Copyright (c) 2018 Trail of Bits, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/*
 * It's a maze!
 * Use a,s,d,w to move "through" it.
 *
 * This version avoids all variadic functions (printf, sprintf, etc.)
 * to be compatible with the mc2llvm lifter.
 */

#include <string.h>
#include <stdlib.h>
#include <unistd.h>

/* Dimensions of the Maze */
enum {
  kWidth = 11,
  kHeight = 7
};

/* Hard-coded maze */
char maze[kHeight][kWidth] = {
    {'+', '-', '+', '-', '-', '-', '+', '-', '-', '-', '+'},
    {'|', ' ', '|', ' ', ' ', ' ', ' ', ' ', '|', '#', '|'},
    {'|', ' ', '|', ' ', '-', '-', '+', ' ', '|', ' ', '|'},
    {'|', ' ', '|', ' ', ' ', ' ', '|', ' ', '|', ' ', '|'},
    {'|', ' ', '+', '-', '-', ' ', '|', ' ', '|', ' ', '|'},
    {'|', ' ', ' ', ' ', ' ', ' ', '|', ' ', ' ', ' ', '|'},
    {'+', '-', '-', '-', '-', '-', '+', '-', '-', '-', '+'},
};

/* ---- Non-variadic I/O helpers (using write only) ---- */

static void print_char(char c) {
  write(STDOUT_FILENO, &c, 1);
}

static void print_str(const char *s) {
  write(STDOUT_FILENO, s, strlen(s));
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

/* Print "NdM\n" */
static void print_2d_int(int a, int b) {
  print_int(a);
  print_char('x');
  print_int(b);
  print_char('\n');
}

/**
 * Draw the maze state in the screen!
 */
void draw(void) {
  int i, j;
  for (i = 0; i < kHeight; i++) {
    for (j = 0; j < kWidth; j++) {
      print_char(maze[i][j]);
    }
    print_char('\n');
  }
  print_char('\n');
}

enum {
  kMaxNumPlayerMoves = 28
};

/**
 * The main function
 */
int main(int argc, char *argv[]) {
  int x, y;     /* Player position */
  int ox, oy;   /* Old player position */
  int i = 0;    /* Iteration number */

  char program[kMaxNumPlayerMoves];

  /* Initial position */
  x = 1;
  y = 1;
  maze[y][x] = 'X';

  /* Print some info. */
  print_str("Maze dimensions: ");
  print_2d_int(kWidth, kHeight);
  print_str("Player position: ");
  print_2d_int(x, y);
  print_str("Iteration no. ");
  print_int(i);
  print_char('\n');
  print_str("Program the player moves with a sequence of 'w', 's', 'a' and 'd'\n");
  print_str("Try to reach the price(#)!\n");

  /* Draw the maze */
  draw();

  /* Read the directions 'program' to execute... */
  read(STDIN_FILENO, program, kMaxNumPlayerMoves);

  /* Iterate and run 'program'. */
  while (i < kMaxNumPlayerMoves) {
    /* Save old player position */
    ox = x;
    oy = y;

    /* Move player position depending on the actual command */
    switch (program[i]) {
      case 'w':
        y--;
        break;
      case 's':
        y++;
        break;
      case 'a':
        x--;
        break;
      case 'd':
        x++;
        break;
      default:
        print_str("Wrong command, only w,s,a,d are accepted!)\n");
        print_str("You lose!\n");
        exit(EXIT_FAILURE);
    }

    /* If hit the price, You Win!! */
    if (maze[y][x] == '#') {
      print_str("You win!\n");
      print_str("Your solution <");
      print_str(program);
      print_str(">\n");
      exit(EXIT_SUCCESS);
    }

    /* If something is wrong do not advance. */
    if (maze[y][x] != ' '
        && !((y == 2 && maze[y][x] == '|' && x > 0 && x < kWidth))) {
      x = ox;
      y = oy;
    }

    /* Print new maze state and info... */
    print_str("Player position: ");
    print_2d_int(x, y);
    print_str("Iteration no. ");
    print_int(i);
    print_str(". Action: ");
    print_char(program[i]);
    print_str(". ");
    print_str((ox == x && oy == y) ? "Blocked!" : "");
    print_char('\n');

    /* If crashed to a wall! Exit, you lose */
    if (ox == x && oy == y) {
      print_str("You lose\n");
      exit(EXIT_FAILURE);
    }

    /* put the player on the maze... */
    maze[y][x] = 'X';

    /* draw it */
    draw();

    /* increment iteration */
    i++;

    /* me wait to human */
    sleep(1);
  }

  /* You couldn't make it! You lose! */
  print_str("You lose\n");
  return EXIT_FAILURE;
}
