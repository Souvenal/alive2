# 指令数量对比：源码 ARM64 vs lifted ARM64

`run.sh` 对比同一份 C 程序通过两种路径生成的汇编规模：

1. **参考路径**：C → `clang` → AArch64 `.o`（标准一次编译）
2. **Lift 路径**：C → `clang` → `.bc` + `.o` → `arm-lifter` → `.ll` → `llc` → ARM64 `.o`（经过 lifter 二次编译）

测试用例 `compiler_rt_int128.c`（本目录内）使用 `unsigned __int128` 算术运算。
AArch64 没有原生的 128 位整数除法指令，编译器后端会将 `udiv i128` 降级为对
`__udivti3` 的调用——这个符号出现在 `.o` 中但不存在于 `.bc` 中，正是 issue 11
要解决的问题。

## 快速开始

```bash
./run.sh [case_name]      # 默认 compiler_rt_int128
```

Linux 环境要求：设置 `LLVM_VERSION=20` 使用版本化工具，或设置 `LLVM_BIN` 到
包含 LLVM 20+ 工具的 `bin` 目录。
可通过 `ARM_LIFTER` 环境变量指定自定义的 arm-lifter 路径。

## 输出文件

| 文件 | 说明 |
|------|------|
| `out_<case>/<case>.ll` | 源码 IR（`.bc` → `llvm-dis`） |
| `out_<case>/<case>.lifted.ll` | Lift 结果（`.o` + `.bc` → `arm-lifter`） |
| `out_<case>/<case>_arm64.s` | 参考 ARM64 反汇编（仅代码段） |
| `out_<case>/<case>_lifted_to_arm64.s` | Lifted IR → `llc` → ARM64 `.o` → 反汇编（仅代码段） |
| `out_<case>/<case>_opt_to_arm64.s` | `opt -O2` lifted IR → ARM64 `.o` → 反汇编（仅代码段） |
| `out_<case>/<case>.lifted.opt.ll` | `opt -O2` 后的 lifted IR |
| `out_<case>/<case>.lift.log` | arm-lifter 调试日志 |

`.bc` 和 `.o` 是编译器产物（LLVM 20+ `clang` 生成，lifter 变化不影响）。
重新运行 `./run.sh` 即可用当前 arm-lifter 刷新所有结果。

## 为什么指令更多？

### 根本原因

源码 IR 工作在**语义层面**：`udiv i128` 直接表达 128 位无符号除法。
Lifted IR 从**机器码重建**，编译器已完成：

1. **指令选择**：`udiv i128` → `bl __udivti3`（AArch64 无原生 128 位除法）
2. **寄存器分配**：中间值分配到物理寄存器（X0–X30、Q0–Q31）
3. **寄存器视为 alloca**：每次读写变成 `load`/`store`
4. **栈帧显式化**：SP/FP 调整变成显式的 GEP 操作

### 指令类别变化

| 类别 | 参考 ARM64 | Lifted → ARM64 |
|------|-----------|----------------|
| `ldr`/`str`（访存） | 11 | 95 |
| `mov`/`fmov`（寄存器传值） | 36 | 62 |
| `orr`/`and`/`sub`（ALU） | 0 | 19 |
| `bl`（函数调用） | 3 | 4 |
| 总计 | 68 | 205 |

`ldr`/`str` 从 11 暴涨到 95，因为 lifted IR 中的每个"寄存器"都是内存中的
`alloca`。原始代码的一条 `fmov x0, d3` 在 lifted IR 中变成
`load Q0 → bitcast → insertelement → bitcast → store Q0 → load X0`。
即使经过 `opt -O2`（189 条，从 205 降下来），`llc` 也无法区分"模拟寄存器"
的 alloca 和真正的内存访问。

### 影响语义吗？

不影响。测试（pytest）已验证重编译后的 x86-64 二进制与 ARM64 参考二进制的
stdout 和 exit code 完全一致。多余的 load/store 只是让二进制膨胀，
行为不变。

### 能优化吗？

部分可以。`opt -O2` 能消除一部分 alloca 访问（205 → 189），但剩余差距
（68 → ~189）是根本性的：lifter 看到的是 `bl __udivti3` 这样的函数边界，
而原始编译器看到的是 `udiv i128`、`lshr i128` 等高层操作。从机器码逆向
回高层 IR 是一个开放研究问题。
