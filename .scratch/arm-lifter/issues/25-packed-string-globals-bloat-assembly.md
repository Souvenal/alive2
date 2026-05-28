# 字符串常量被展开为 548 行 .byte 指令

状态：`needs-investigation`

## 问题描述

使用 `dev.py full` 生成 ARM 汇编后对比，lifted 的汇编比 source 大 3 倍（1001 行 vs 361 行）。主要原因是字符串常量在 lifted IR 中被表示为一个单一的 opaque struct，llc 无法识别为字符串，只能逐字节展开成 `.byte` 指令。

### 对比

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

### 异常情况（lifted.s）

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
	.ascii	"+-+---+---+"
	.ascii	"| |     |#|"
	...
```

`maze` 是 `[7 x [11 x i8]]` 数组类型，保留了原始类型信息，所以 llc 能输出 `.ascii`。

## 根因

`__sec_11` 全局变量在 lifted IR 中定义为：

```llvm
%0 = type { i8, i8, i8, ... }   ; 数百个 i8
@__sec_11 = weak constant %0 { i8 89, i8 111, ... }
```

lifter 的 `obj2asm` 将 ELF 数据节中所有常量打包进单个 struct，丢失了每个字符串的独立身份和长度信息。llc 看到的是一个 packed struct of bytes，无法恢复为 `.asciz`。

这与 Issue 19（Lifted globals lose source-level names and types）和 Issue 20（Symbol-level compatibility for partial lifting）相关，但问题 25 聚焦于它对汇编输出的具体影响。

## 影响

- **可读性**：汇编输出中无法分辨字符串内容
- **行数膨胀**：~550 行额外 `.byte` 指令（约占 lifted.s 的 55%）
- **大小**：汇编文件大小增加约 3 倍，间接影响编译时间
- **排查难度**：难以将 lifted 汇编与 source 汇编做逐行对比

## 可能的修复方向

1. 为每个字符串常量生成独立的 `@.str.N` 全局变量（与问题 20 一致）
2. 在 obj2asm 中识别 ELF 字符串（`SHF_STRINGS` flag），保留字符串类型
3. 将 packed struct 拆分为命名数组类型（如 `@.str.0 = constant [28 x i8] c"..."`）
