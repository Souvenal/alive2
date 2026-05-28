# 字符串常量被展开为 548 行 .byte 指令

Status: `need-triage`
Resolution: Resolved by issue 20 (symbol-level string globals via offset-based byte matching)

## 问题描述

使用 `dev.py full` 生成 ARM 汇编后对比，lifted 的汇编比 source 大 3 倍（1001 行 vs 361 行）。主要原因是字符串常量在 lifted IR 中被表示为一个单一的 opaque struct，llc 无法识别为字符串，只能逐字节展开成 `.byte` 指令。

### 对比（修复前）

| 指标 | nodbg.s | lifted.s |
|------|---------|----------|
| 总行数 | 361 | 1001 |
| `.asciz` 行数 | 12 | 0 |
| `.byte` 行数 | 0 | 548 |
| `.ascii` 行数 | 7 | 7 |

### 正常情况（nodbg.s）

```asm
.asciz  "Maze dimensions: %dx%d\n"
.asciz  "Player position: %dx%d\n"
.asciz  "Iteration no. %d\n"
...
```

C 编译器将每个字符串常量作为独立的 `.asciz` 放入 `.rodata`，每个字符串一行，简洁可读。

### 异常情况（修复前）

```asm
.byte   77                              // 0x4d  ('M')
.byte   97                              // 0x61  ('a')
.byte   122                             // 0x7a  ('z')
.byte   101                             // 0x65  ('e')
.byte   32                              // 0x20  (' ')
...
```

全部字符串展开为 548 行 `.byte`，不可读且浪费行数。

### 例外：maze 变量正常

```asm
maze:
    .ascii  "+-+---+---+"
    .ascii  "| |     |#|"
    ...
```

`maze` 是 `[7 x [11 x i8]]` 数组类型，保留了原始类型信息，所以 llc 能输出 `.ascii`。

## 根因

与 issue 20 相同。`__sec_11` 全局变量在 lifted IR 中被定义为 `StructType { i8, i8, ... }`，丢失了每个字符串的独立身份和长度信息。llc 看到的是一个 packed struct of bytes，无法恢复为 `.asciz`。

## 修复

Issue 20 将 lifted IR 中的字符串恢复为独立的 `@.str.N = constant [N x i8] c"..."` 格式。llc 因此可以正确识别字符串常量并输出 `.asciz` 指令。

### 修复后效果

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 总行数 | 1001 | 746 |
| `.asciz` 行数 | 0 | 22 |
| `.byte` 行数（字符串数据） | 548 | 0* |
| `.long` 行数 | ~60 | 60 |

\* 剩余的 `.byte` 指令全部来自 DWARF debug metadata，与字符串数据无关。

## 文件

无代码变更 — 由 issue 20 间接修复。
