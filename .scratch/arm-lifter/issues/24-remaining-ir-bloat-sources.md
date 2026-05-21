# 消除剩余 IR 膨胀：ptrtoint/inttoptr 往返与向量操作碎片化

状态：`needs-triage`

## 问题描述

Issue 21（寄存器 alloca mem2reg 修复）已将 IR 膨胀从 ~6× 降至 ~3×。当前 `matmul` 案例对比：

| IR 文件 | 行数 | 与 nodbg 比值 |
|---------|------|--------------|
| `matmul.nodbg.ll` | 196 | 1.0×（基线） |
| `matmul.lifted.ll` | 614 | 3.1× |

目标是趋近 1.0×。剩余差距来自两大块：

## 差距来源分析

### 1. `ptrtoint`/`inttoptr` 往返（约占总膨胀 35%）

lifted IR 中每个内存访问都走这个模式：

```llvm
%addr = ptrtoint ptr %some_ptr to i64
%new_addr = add i64 %addr, <offset>
%ptr = inttoptr i64 %new_addr to ptr
%val = load i32, ptr %ptr
```

而 nodbg 直接是：

```llvm
%val = load i32, ptr %some_ptr, align 4
```

这是因为汇编层所有地址都存在整数寄存器（`xN`）里，lifter 无法区分"这个 x 寄存器存的是地址还是整数"。如果 lifter 能在某些点恢复指针类型（例如推导出某个寄存器始终指向栈或全局变量），就能避免每次访问都做 ptrtoint/inttoptr 往返。

**影响**：每一处 load/store 多出 2–4 条指令（ptrtoint + 偏移计算 + inttoptr + 有时冗余的 ptrtoint(inttoptr(x))）。

### 2. 向量手动拆解 i128 ↔ `<4 x i32>`（约占总膨胀 30%）

AArch64 NEON 指令以 128-bit 向量（`Q` 寄存器）为基本操作单元，lifter 用 `i128` 模拟。每次向量操作都需要：

```llvm
%v = bitcast i128 %container to <4 x i32>
%v2 = insertelement <4 x i32> %v, i32 %new_elem, i32 1
%container2 = bitcast <4 x i32> %v2 to i128
```

而在 nodbg 中，编译器直接使用高级向量 IR（`<8 x i32>`、`llvm.vector.reduce.add.v64i32`）。

**影响**：每个向量操作膨胀 3–5×，求和规约部分最严重（checksum 函数从 4 行 IR 变成 121 行）。理想情况下 lifter 应能识别出多个 i128 操作构成一个高阶向量操作并合并。

### 3. 寄存器初始化与冗余类型转换（约占总膨胀 20%）

常见模式：

- `freeze i64 poison` 作为未初始化的寄存器值（每次函数调用重复出现）
- `trunc i64 %x to i32` 紧接着 `zext i32 %x to i64`（只需一次 `and`）
- `or i64 0, 0` 代替 `i64 0`
- `trunc i128 %x to i32` / `zext i32 %x to i128` 往返

### 4. 额外基本块与 phi 节点（约占总膨胀 10%）

汇编指令一一映射为基本块（每条指令一个标签），使得 CFG 更碎片化。这本身不一定是问题，但导致更多 phi 节点和分支。

### 5. 内联函数展开形式差异（约占总膨胀 5%）

`print_int` 等内联函数在 lifted IR 中展开得更冗长。部分原因是额外的指针计算，部分原因是循环结构不同的表达方式。

## 建议的优化方向（优先级排序）

1. **指针类型恢复**：在 `arm2llvm.cpp` 中，当能推导出寄存器指向已知对象（栈 alloca、全局变量、或函数参数）时，直接生成 `ptr` 类型的 SSA 值而非 `i64`，消除 `inttoptr`/`ptrtoint` 往返。

2. **向量操作合并**：在 `lifter_cleanup.cpp` 中添加 LLVM pass，识别 `bitcast(i128)→<4xi32>→操作→bitcast→<4xi32>` 序列并折叠为单个高阶向量操作。

3. **寄存器初始化优化**：对于 `freeze poison` 后从未被 `store` 的寄存器，直接替换为 `undef` 或 `0`（使后续 DCE 可以消除相关操作）。

4. **简化类型转换**：在 cleanup pass 中消除连续的 `trunc`/`zext` 往返（在 `i32` 和 `i64` 之间的冗余转换）。

## 度量方式

```bash
cd tests/lift
for case in matmul maze_novarargs minirepro struct_test; do
  echo "--- $case ---"
  wc -l output/${case}.nodbg.ll output/${case}.lifted.ll
  ratio=$(python -c "print(round($(grep -c '^  ' output/${case}.lifted.ll) / $(grep -c '^  ' output/${case}.nodbg.ll), 2))")
  echo "ratio: ${ratio}x"
done
```

以 `matmul` 为标杆，目标 1.5× 以内。

## 相关文件

- `backend_tv/arm2llvm.cpp` — 生成 ptrtoint/inttoptr 和向量操作
- `backend_tv/mc2llvm.cpp` — MC 指令 → LLVM IR 核心引擎
- `lifter_util/lifter_cleanup.cpp` — 清理 pass 管线
- `tests/lift/cases/matmul.c` — 主要 benchmark 案例
