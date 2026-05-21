# 类型不匹配导致 SP/FP/Q0 逃逸 mem2reg，残存 ~3× 指令膨胀

Status: `ready-for-agent`

## 问题

Lifted IR 将 AArch64 物理寄存器建模为 `alloca`，并已在 `lifter_cleanup.cpp`
中跑了 `mem2reg`。X0–X30 全部成功提升为 SSA 值，但 SP、FP、Q0 因类型混用
逃逸了 mem2reg，残存的 load/store 导致汇编膨胀 ~3×。

## 现状：已跑的优化

`lifter_util/lifter_cleanup.cpp` 的 pass pipeline：

```
function(mem2reg, dce, simplifycfg), globaldce
```

效果：X0–X30（31 个 `i64 alloca`）全部被 mem2reg 消除。否则膨胀会更严重。

## 膨胀的根源：3 个逃逸的 alloca

lift 后的 IR 中残存 4 个 alloca：

| alloca | 类型 | 逃逸原因 |
|--------|------|---------|
| `%stack1 = alloca i8, i64 1280` | 模拟栈 | GEP 基地址，mem2reg 不处理这种 |
| **`%SP = alloca i64`** | SP 寄存器 | 混合 `i64` / `ptr` 类型存取 |
| **`%FP = alloca i64`** | FP 寄存器 | 混合 `i64` / `ptr` 类型存取 |
| **`%Q0 = alloca i128`** | Q0 寄存器 | 混合 `i128` / `<2 x i64>` 类型存取 |

### 具体例：SP 的混合类型访问

```llvm
%SP = alloca i64                       ; 声明为 i64 a
store i64 %initial_val, ptr %SP         ; 写 i64   ✓
store ptr %stack_top, ptr %SP           ; 写 ptr   ✗ 类型不同
%val = load i64, ptr %SP               ; 读 i64   ✓
%ptr_val = load ptr, ptr %SP           ; 读 ptr   ✗ 类型不同
```

`mem2reg` 要求同个 alloca 的所有 `load`/`store` 类型一致才能提升。
SP 在 AArch64 中同时承担整数加减（`sub sp, sp, #N`）和指针运算
（`str x0, [sp, #0x10]`），lifter 忠实地保留了这种双角色，反而阻塞了优化。

### FP 同理

```llvm
%FP = alloca i64
store ptr %sp_plus_offset, ptr %FP     ; 写 ptr
%val = load i64, ptr %FP               ; 读 i64
```

### Q0 的向量/标量混用

```llvm
%Q0 = alloca i128
store i128 %result, ptr %Q0            ; 写完整 128 位
%vec = load <2 x i64>, ptr %Q0        ; 读为向量（提取 lane）
```

## 量化数据

### Maze_novarargs（6 函数）

| 阶段 | 指令数 | 膨胀比 |
|------|--------|--------|
| 原始 ARM64 `.o` | 428 | 1× |
| 当前 lifted → ARM64 | 1325 | **3.1×** |
| `opt -O2` 后 | 1277 | 3.0× |

### matmul（3 函数：main, matmul, checksum）

新增 compute-intensive benchmark，验证 cleanup pipeline 增强的效果：

| 阶段 | 指令数 | 膨胀比 |
|------|--------|--------|
| 原始 ARM64 `.o` | 255 | 1× |
| 当前 lifted → ARM64 | 536 | **2.1×** |
| cleanup + `instcombine` | 536 | 2.1× |

**关键发现**：在 `lifter_cleanup.cpp` 的 pipeline 中增补 `instcombine` 对 matmul 的指令数**零改善**（536 → 536）。这与 Maze 数据一致 —— `opt -O2` 对 lifted IR 几乎无效。根因是 SP/FP/Q0 的类型混用 alloca 在 `mem2reg` 阶段就逃逸了，后续 passes 无法触及残存的 load/store 序列。

1280 字节的模拟栈（`stack1`）每次函数调用都分配，即使实际栈帧很小。
SP/FP/Q0 的冗余 load/store 构成剩余膨胀的主体。

## 建议修复

有两种思路：

### 方案 A：在 lifter 端保证类型统一

修改 `arm2llvm.cpp` 中 SP/FP/Q0 的存取，强制所有指针操作走
`ptrtoint`/`inttoptr` 包装：

```diff
-  store ptr %stack_top, ptr %SP
+  %sp_int = ptrtoint i8* %stack_top to i64
+  store i64 %sp_int, ptr %SP
```

Q0 的 `<2 x i64>` 存取改为 `i128` + `shl`/`or`/`trunc`。

这样 mem2reg 就能提升 SP/FP/Q0，消除冗余 load/store。

### 方案 B：在 cleanup 中加 extra  passes

相比于改动 lifter 生成逻辑，在 cleanup pipeline 增补 passes：

```
function(mem2reg, instcombine, dce, simplifycfg), globaldce
```

`instcombine` 可以消除一部分 `ptrtoint`/`load`/`store` 序列，
但无法根治类型混用的 alloca——因为 mem2reg 在第一关就跳过了它们。

**2026-05-21 验证**：新增 `matmul` benchmark 并实测在 cleanup pipeline 中加入 `instcombine`，指令数 536 → 536（零改善）。Maze 数据（`opt -O2` 仅减少 3.6%）与 matmul 数据一致证明：**方案 B 治标不治本，且事实上几乎无效。推荐方案 A。**

## 相关文件

- `lifter_util/lifter_cleanup.cpp` — 现有 cleanup pipeline（注释含 `instcombine`，实际 pipeline 字符串遗漏）
- `backend_tv/arm2llvm.cpp` — SP/FP/Q0 的寄存器读写代码
- `backend_tv/mc2llvm.h/cpp` — 寄存器 alloca 的创建
- `tests/lift/experiments/instruction-count-comparison/` — 复现脚本和数据
- `tests/lift/cases/matmul.c` — 新增 compute-intensive benchmark
- `tests/lift/test_lift.py` — test case 注册
