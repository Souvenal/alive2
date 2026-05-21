# 修复寄存器 alloca 类型不匹配，使 mem2reg 能处理 SP/FP/Q0

**日期**: 2026-05-21
**Issue**: 21

## 概要

修复了 `readPtrFromReg`、`readFromRegTyped` 以及 `arm2llvm.cpp` 中五处直接调用 `createStore` 的地方，使所有 load/store 类型与对应 alloca 的 `getAllocatedType()` 一致。这样 LLVM 的 `mem2reg` 就能把 SP、FP、Q0 这些寄存器提升为 SSA 值，消除冗余的 load/store 序列。

`instcombine` 曾试用过但已回退——它进一步将 IR 减少约 50%（maze 从 1079 降至 334），但在 Maze_novarargs 中引入了行为回归。根因分析推迟到后续跟进。

## 根因

`createRegStorage` 创建的所有寄存器 alloca 都使用整数类型（SP/FP = `i64`，Q0-Q31 = `i128`）。LLVM 的 `isAllocaPromotable` 要求每个 load/store 的类型与 `AllocaInst::getAllocatedType()` 一致。以下三种模式违反了这一要求：

1. `readPtrFromReg` 从 `i64` alloca 中加载 `ptr`
2. `readFromRegTyped` 从 `i128` alloca 中加载向量类型
3. 直接 store（paramBase、initFP、向量参数）向整数 alloca 存 `ptr`/向量

这对于上游 Alive2 来说并无问题（SMT 比较，无需重新编译），但导致 arm-lifter 重新编译管线的 IR 膨胀约 3 倍。

## 改动

- `readPtrFromReg`：按寄存器位宽加载整数，再用 `inttoptr` 转为 `ptr`
- `readFromRegTyped`：对于非整数的目标类型，先加载整数再转换（inttoptr/bitcast）
- paramBase、sret、标量/向量参数、initFP 处的直接 store：在 store 前添加 `ptrtoint`/`bitcast`
- `lifter_cleanup.cpp`：**保持不动**——添加 `instcombine` 虽能将 lifted IR 再减少约 50%（如 maze 从 1079 降至 334），但在 Maze_novarargs 中引入了行为回归（stdout 不一致）。instcombine 交互问题的根因分析推迟到后续跟进。
