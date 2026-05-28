# instcombine 导致 Maze_novarargs 行为回归

状态：`resolved`

## 解决方案

在清理管线中将 `instcombine` 替换为 `instsimplify`：

```
-function(mem2reg,instcombine,dce,simplifycfg),globaldce
+function(mem2reg,instsimplify,dce,simplifycfg),globaldce
```

`instsimplify` 是 `instcombine` 的一个子集，专注于纯表达式化简（常量折叠、代数化简等），**不包含** load/store 优化（DSE）、GEP/ptrtoint/inttoptr 折叠等可能导致内存语义错误的变换。

### 根因分析

通过详尽实验排除了多个假说，最终确定根因在 `instcombine` 内部：

1. `freeze(poison) → 0`（非根本原因）——即使完全去除 IR 中所有 freeze/poison，回归依然存在
2. **DSE / GEP/"undef is poison" 链**（根本原因）：
   - `instcombine` 内部的 DSE 删除了入口块中对 `stack12` 的 store（因为 store 在同一块内没有被读取，且跨块别名的分析不完整的 intraprocedural DSE 无法确认 `.L_280` 中的 load 与这些 store 别名）
   - 后续的 `simplifycfg` 或 `instcombine` 展开 GEP/ptrtoint/inttoptr 链时，产生了带 `inbounds` 的 GEP
   - 当 GEP 索引来自未初始化内存的 load 时，LLVM 22 的 `undef→poison` 优化使其变成 `poison`
   - `br i1 poison` → `unreachable` → 整个 main 函数体被删除

### 优化效果

| 测试用例 | 无优化 (基准) | instsimplify | instcombine |
|----------|--------------|-------------|-------------|
| Maze_novarargs | 1079 | 678 (✓) | 334 (✗) |
| matmul | 532 | 347 (✓) | 275 (✗) |
| struct_test | — | 424 (✓) | — |
| 全部 must_pass | 9 passed | 9 passed | 8 passed |

`instsimplify` 提供了约 37% 的指令数缩减（vs instcombine 的约 65%），但**所有 9 个 must_pass 测试全部通过**。

## 问题描述

Issue 21 修复了寄存器 alloca 类型不匹配的问题，使 mem2reg 能够提升 SP/FP/Q0。在清理管线中添加 `instcombine`（`function(mem2reg,instcombine,dce,simplifycfg),globaldce`）后，lifted IR 进一步减少了约 50%：

| 测试用例 | 无 instcombine | 有 instcombine |
|----------|---------------|----------------|
| matmul   | 532           | 275            |
| maze_novarargs | 1079     | 334            |

然而，`instcombine` 在 Maze_novarargs 中引入了行为回归：`print_int` 函数输出为空，导致 stdout 不匹配：

```
--- REF ---
Maze dimensions: 11x7
Player position: 1x1
...

--- SUB ---
Maze dimensions: 
Player position: 
...
```

数值（11、7、1、1）和分隔符字符均丢失。

不开启 `instcombine` 时，全部 9 个 must_pass 测试均通过。该回归问题特定于 instcombine 与类型统一后的寄存器 IR 之间的交互（仅 lifter 本身的变更是正确的）。

## 待调查

instcombine 是一个大型 pass，包含多个子变换。可能的原因包括：

1. **`freeze poison → 0` 规范化**——寄存器初始化使用了 `freeze poison`；instcombine 将其替换为 `0`。可能影响合并了已初始化和未初始化寄存器值的 phi 节点。
2. **inttoptr/ptrtoint 折叠**——`inttoptr(ptrtoint(x)) → x` 可能错误地折叠栈指针计算，如果中间的算术操作混淆了折叠逻辑。
3. **循环迭代次数分析**——内联后的 `print_int` 包含一个数字打印循环（`while n > 0 { n /= 10 }`）。当 `n` 经过 `readFromRegTyped` 引入的 `trunc`/`zext` 链时，instcombine 可能错误计算了迭代次数。
4. **load/store 转发**——instcombine 可能错误地将 store 通过模拟栈（`stack12` alloca）转发。

## 建议排查方向

1. 转储并对比有无 instcombine 时 `print_2d_int` 的 lifted IR，重点关注数字打印循环
2. 尝试 instcombine 的子选项：`instcombine<no-ldst-opt>`、`instcombine<max-iterations=1>` 以缩小范围
3. 检查回归是否仅影响内联后的 `print_int`，还是影响所有整数运算
4. 如果定位到特定变换，在 lifter 中规避（如避免生成触发错误折叠的模式），或添加一个只做安全子集的针对性 pass

## 相关文件

- `lifter_util/lifter_cleanup.cpp`——清理 pass 管线
- `backend_tv/arm2llvm.cpp`——寄存器读写（issue 21 修复）
- `tests/lift/cases/Maze_novarargs.c`——复现测试用例
