# 消除剩余 IR 膨胀：向量规约是主要矛盾

状态：`ready-for-agent`

## 问题描述

Issue 21（寄存器 alloca mem2reg 修复）将 IR 膨胀从 ~6× 降至 ~5×。Issue 23（加入 `instsimplify`）进一步降至 3.27×。

`matmul` 案例实测数据（2026-05-28，instsimplify 已加入）：

| 指标 | nodbg | lifted（当前） | 比值 |
|------|-------|--------------|------|
| 总行数 | 195 | 428 | 2.19× |
| 指令数（`^  `） | 106 | 329 | **3.10×** |
| x86 指令数 | 333 | 861 | **2.59×** |

### 变化历程

| 阶段 | lifted IR 指令数 | 比值 | 说明 |
|------|-----------------|------|------|
| Issue 21 前 | ~650+ | ~6× | 寄存器 alloca 未优化 |
| Issue 21 后 (mem2reg) | 532 | 5.02× | 寄存器 promoted to SSA |
| Issue 23 后 (+instsimplify) | 347 | 3.27× | 常量折叠、`or 0,0` 消除 |
| **修复 ADDV/UADDLV/SADDLV** | **329** | **3.10×** | 发射 `vector.reduce.add` |
| **目标** | **~150** | **~1.4×** | 接近 nodbg 基线 |

## 核心发现

参见 `docs/design/bloat-analysis.md` 完整分析。核心结论：

**在"汇编级忠实"前提下（保留 ptrtoint、i128 bitcast、freeze poison），唯一真正需要修复的是向量规约指令的处理。**

ARM 汇编中已经包含 `addv s0, v0.4s` 等向量水平加指令。但 lifter 没有发射 `llvm.vector.reduce.add`，而是手动拆成 extractelement + add + insertelement 的标量循环。这是 `backend_tv/arm2llvm_int_vec.cpp` 中 `lift_unary_vec` 的历史实现缺陷（从 backend-tv Alive2 流程遗留的），不是汇编级忠实的代价。

### 影响的 opcode

| 函数 | ARM 指令 | 当前 IR | 应该生成 |
|------|---------|--------|---------|
| `lift_unary_vec` | `ADDVv8i8v/v8i16v/v4i32v/v16i8v` | 手动 extractelement + add + insertelement loop | `llvm.vector.reduce.add.vNty` |
| `lift_unary_vec` | `UADDLVv8i16v/v4i32v/v8i8v/v4i16v/v16i8v` | 手动 zext + extractelement + add loop | `zext` + `llvm.vector.reduce.add` |
| `lift_unary_vec` | `SADDLVv8i8v/v16i8v/v4i16v/v8i16v/v4i32v` | 手动 sext + extractelement + add loop | `sext` + `llvm.vector.reduce.add` |

### 实际效果

| 函数 | 修复前 | 修复后 | 节省 | 说明 |
|------|-------|-------|------|------|
| `checksum` | 73 | 67 | 6 | 手动 reduce loop 已替换，剩余 i128 bitcast |
| `matmul` | 79 | 73 | 6 | 同上 |
| `main` | 195 | 189 | 6 | 同上 |
| **总计** | **347** | **329** | **18（5%）** | |

IR 比值从 3.27× 降至 3.10×。节省低于最初估计（~150），因为大部分 bloat 是 i128 bitcast 链（NEON Q 寄存器建模），非向量规约循环。

**核心教训**：Issue 24 的主要矛盾是 NEON Q 寄存器用 i128 模拟的开销（~30-50% 总 bloat），不是向量规约的标量分解。`vector.reduce.add` 替换是正确的改进但力度有限。

## 函数级明细

| 函数 | nodbg 指令数 | 修复前 | 修复后 | 差距源 |
|------|-------------|-------|-------|--------|
| `matmul` | 41 | 79 | 73 | ptrtoint(10) + i128 bitcast(20) + 其他(2) |
| `checksum` | 3 | 73 | 67 | i128 bitcast + 向量操作(60) + 其他(4) |
| `main` | 62 | 195 | 189 | ptrtoint + freeze poison + print_int 内联展开 |

## 残留的膨胀（当前 ~223 条，比值 3.10×）

### 1. ptrtoint/inttoptr 往返（~45 条）——保留

所有指针参数在函数入口处转 `i64`，通过 phi 节点传播。每次访存前 inttoptr。

这是在"汇编级忠实"原则下保留的——ARM 的 `add x0, x1, x2` 是整数加法。x86 后端每条 store 多出 1 条 `add`/`lea`，是合理代价。

```llvm
entry:
  %3 = ptrtoint ptr %0 to i64
  ...
.L_14:
  %X12.0 = phi i64 [ %a5_2, %.L_8 ], ...
  %6 = inttoptr i64 %X12.0 to ptr
  ...
```

### 2. i128 ↔ `<4 x i32>` bitcast 往返（~30 条）——保留

NEON Q 寄存器用 `i128` 模拟。bitcast 是后端零成本操作（寄存器重命名），不影响 x86 效率。

```llvm
%a45_4 = bitcast i128 %a21_3 to <4 x i32>
%a45_5 = mul <4 x i32> %a37_6, %a45_4
```

### 3. freeze poison / 常量 / trunc/zext（~20 条）——保留

- `%a0_38 = freeze i64 poison`：未初始化寄存器的忠实表达
- trunc/zext：`instsimplify` 已覆盖大部分，剩余零成本

### 4. 额外基本块与 phi 节点（~20 条）——保留

逐指令映射为基本块，简化后仍有残留。

## 优化方向（按优先级排序）

### 1. [唯一行动] 修复 ADDV/UADDLV/SADDLV 的 IR 发射

**位置：** `backend_tv/arm2llvm_int_vec.cpp` 中 `lift_unary_vec` 函数

**当前代码（`arm2llvm_int_vec.cpp:220-231`）：**
```cpp
case AArch64::ADDVv4i32v: {
    auto src_vector = createBitCast(src, vTy);
    Value *sum = getUnsignedIntConst(0, eltSize);
    for (unsigned i = 0; i < numElts; ++i) {
      auto elt = createExtractElement(src_vector, i);
      sum = createAdd(sum, elt);
    }
    auto zero = getZeroIntVec(numElts, eltSize);
    auto res = createInsertElement(zero, sum, 0);
    updateOutputReg(res);
    break;
}
```

**UADDLV/SADDLV 同样问题（`arm2llvm_int_vec.cpp:290-313`）：**
```cpp
case AArch64::UADDLVv4i32v: {
    bool isSigned = ...;
    auto src_vector = createBitCast(src, vTy);
    Value *sum = getUnsignedIntConst(0, 2 * eltSize);
    for (unsigned i = 0; i < numElts; ++i) {
      auto elt = createExtractElement(src_vector, i);
      auto ext = isSigned ? createSExt(elt, bigTy) : createZExt(elt, bigTy);
      sum = createAdd(sum, ext);
    }
    updateOutputReg(sum);
    break;
}
```

**修复方案：**
- ADDV：发射 `llvm.vector.reduce.add.vNty(<N x i32> %v)`
- UADDLV：先 `zext` 到宽类型，再 `llvm.vector.reduce.add`
- SADDLV：先 `sext` 到宽类型，再 `llvm.vector.reduce.add`

### 2. 不采取的行动

- 不做指针 provenance recovery pass（与"汇编级忠实"冲突）
- 不合并 i128 bitcast 链（x86 后端零成本）
- 不优化 freeze poison（忠实表达）
- 保留 `instsimplify`（零风险）
- `tryReplaceRoundTrip` 保留为可选项，默认关闭

## 度量方式

```bash
cd tests/lift
for case in matmul maze_novarargs minirepro struct_test; do
  echo "--- $case ---"
  echo "nodbg insts: $(grep -c '^  ' output/${case}.nodbg.ll)"
  echo "lifted insts: $(grep -c '^  ' output/${case}.lifted.ll)"
  python3 -c "print(f'ratio: {round($(grep -c '^  ' output/${case}.lifted.ll) / $(grep -c '^  ' output/${case}.nodbg.ll), 2)}x')"
done
```

最终目标：IR 比值 1.5× 以内，x86 指令比值 1.5× 以内。

## 当前状态总结

| 措施 | 指令数 | 比值 | 状态 |
|------|--------|------|------|
| 基准（mem2reg+dce+simplifycfg） | 532 | 5.02× | ✅ 已完成 |
| + instsimplify（Issue 23） | 347 | 3.27× | ✅ 已完成 |
| **+ 修复 ADDV/UADDLV/SADDLV** | **329** | **3.10×** | ✅ **已完成** |
| + 消除 i128 NEON Q 寄存器开销 | ~200 | ~1.9× | ❌ 待研究 |
| + 后续 peephole | ~150 | ~1.4× | ❌ 可选 |

## 相关文件

- `backend_tv/arm2llvm_int_vec.cpp` — **修复目标**（`lift_unary_vec` 中 ADDV/UADDLV/SADDLV 处理）
- `docs/design/bloat-analysis.md` — 完整分析文档
- `lifter_util/lifter_cleanup.cpp` — 清理 pass 管线
- `tools/arm-lifter.cpp` — `tryReplaceRoundTrip`（默认应关闭）
- `tests/lift/cases/matmul.c` — 主要 benchmark 案例
