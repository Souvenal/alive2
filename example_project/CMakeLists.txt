cmake_minimum_required(VERSION 3.16)
project(example C)

set(CMAKE_C_COMPILER clang)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

add_executable(example
    src/main.c
    src/calc.c
    src/util.c
)
