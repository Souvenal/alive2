# Maze 指令膨胀分析

对真实的 Maze 游戏程序进行指令数量对比，评估 arm-lifter 的寄存器-alloca 建模
对汇编规模的影响。

## 快速开始

```bash
./run.sh Maze              # 使用 printf 的变体
./run.sh Maze_novarargs    # 不使用变参函数
```

## 与 `instruction-count-comparison` 的区别

`instruction-count-comparison/` 用简单的 `compiler_rt_int128.c`（单函数）做对比，
这里用 Maze（多函数、完整迷宫游戏）评估实际场景。

## 已知结果

| 指标 | Maze_novarargs |
|------|---------------|
| 原始 ARM64 指令数 | 428 |
| Lifted ARM64 指令数 | 1325 |
| opt -O2 后 | 1277 |
| 膨胀比 | ~3.0× |
