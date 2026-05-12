# 为什么把 ptrtoint/add/inttoptr 还原为 GEP

## 背景

arm-lifter 的目标：ARM64 汇编 → LLVM IR → x86_64 汇编，追求汇编层面的语义等价。

ARM 的指针运算是一个普通的整数寄存器操作：

```asm
add x0, x1, x2      ; 32/64 bit 整数加法
ldr x3, [x0]        ; 从 x0 指向的地址加载
```

ARM 硬件不区分"整数加法"和"指针加法"——都是寄存器上的 `ADD`。但 lifter 在把这条 `ADD` 转回 LLVM IR 时面临选择：它操作的两个寄存器，在 IR 层面是 `ptr` 还是 `i64`？

lifter 保守地选择 `i64`（int），结果生成了：

```llvm
%p = ptrtoint ptr %base to i64
%o = add i64 %p, %idx
%r = inttoptr i64 %o to ptr
```

这是个 round-trip：指针→整数→指针。

## 问题

`ptrtoint`/`inttoptr` 在 LLVM 中有严格语义约束：

1. **损失 pointer provenance** — 一旦指针变成整数，LLVM 无法追踪它的来源。`inttoptr` 返回的指针不是 `base` 的派生指针，而是"凭空出现"的新指针。别名分析被迫保守处理。

2. **阻碍优化** — x86_64 后端看到 GEP 可以直接编码为 `[base+idx]` 寻址。看到 `inttoptr` 则必须单独生成 `add` + `load`。指令更多。

3. **语义漂移** — ARM 的 `add x0, x1, x2` 本质是指针运算。lifter 把它表示为 int 运算是 lifter 的表示偏差，不是 ARM 语义的忠实反映。

## 替换

```llvm
%o = add i64 (ptrtoint ptr %base to i64), %idx
%r = inttoptr i64 %o to ptr

→

%r = getelementptr i8, ptr %base, i64 %idx
```

GEP 保留了 `%base` 的 pointer provenance，告诉 LLVM `%r` 是 `%base` 的派生指针。在 x86_64 后端，这直接映射到 `[base+idx]` 寻址。

**GEP 更接近 ARM 的原始语义**——不是"先转整数再加再转回"，而是"基址 + 偏移"。

## 对 asm-to-asm 准确性的影响

替换 ptrtoint round-trip → GEP **提升**准确性：

- ARM `add + ldr` → IR `GEP + load` → x86_64 `[base+idx] + mov`，三层之间的指针偏移关系被保留
- 如果保留 `ptrtoint/inttoptr`，LLVM 可能丢失 base/offset 关系，x86_64 输出会多出不必要的 `mov`/`add`

`--run-replace-ptrtoint` 是对 lifter 内部表示偏差的纠正，不是对 ARM 语义的篡改。

## 参考

- `tools/arm-lifter.cpp:tryReplaceRoundTrip()` — 实现
- LLVM Language Reference: [Pointer Aliasing Rules](https://llvm.org/docs/LangRef.html#pointer-aliasing-rules)
- Alive2 paper: §3 — provenance tracking in refinement checking
