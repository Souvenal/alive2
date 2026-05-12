# arm-lifter: ELF → Re-assemblable Assembly 功能完备性评估

> 本文档评估 `arm-lifter` 从 ELF `.o` 文件逆向生成 re-assemblable GAS 汇编的当前能力、缺失项，以及缺失信息能否由 `--src-bc` 补足。

## 背景

arm-lifter 的核心路径是：

```
ELF .o → generateFullAsm() → re-assemblable .s → MCAsmParser → MCStreamerWrapper → lift
```

`generateFullAsm()` 需要逆向 `addPassesToEmitFile()` 的输出格式——从已编码的 binary 中重建符号、relocation、段属性等信息，组装成 `MCAsmParser` 能正确解析的 GAS 汇编。这是目前唯一能产出 AArch64 re-assemblable assembly 的方式（无现成 LLVM API 或第三方工具可替代）。

---

## 1. .text Section（指令反汇编）

| 能力 | 状态 | 说明 | src-bc 能否补足 |
|------|------|------|----------------|
| 函数入口标签 | ✅ 已实现 | 从 symMap 识别函数符号 | — |
| `.globl` / `.type @function` | ✅ 已实现 | 手工生成 | — |
| `.cfi_startproc` / `.cfi_endproc` | ✅ 已实现 | 手工生成 | — |
| `.p2align` 对齐 | ✅ 已实现 | 固定 `.p2align 2` | ✅ src-bc 的 `Function::getAlignment()` 可获取精确对齐，但通常 4 字节对齐已够 |
| 指令反汇编 (MCInst → 文本) | ✅ 已实现 | `MCDisassembler` + `MCInstPrinter` | — |
| Branch 目标符号化 (b.eq, cbz) | ✅ 已实现 | `ELFSymbolizer` 优先查 relocMap → symMap → `.L_<hex>` | — |
| ADRP/LDR/ADD relocation 符号化 | ✅ 已实现 | `ELFSymbolizer` + `MCSpecifierExpr` 生成 `:abs_g0:` / `:lo12:` 等修饰符 | — |
| GOT 页/偏移修饰符 | ✅ 已实现 | `R_AARCH64_ADR_GOT_PAGE` → `:got:` | — |
| `.loc` 行号指令 | ✅ 已实现 | 手工生成 `instCount` 作为行号 | ⚠️ src-bc 的 `!dbg` metadata 可映射回源码行号，但 arm-lifter 不做 refinement check，行号精度不重要 |
| 地址标签 (`.L_<hex>:`) | ✅ 已实现 | 为每条指令生成 | — |
| BL 外部函数调用符号化 | ✅ 已实现 | `R_AARCH64_CALL26` → 符号名 | — |
| Section symbol + addend → 合成标签 | ✅ 已实现 | `buildSectionOffsetLabels()` | — |
| `.size` 指令 (函数) | ✅ 已实现 | 手工 `.size name, .-name` | — |
| **非 `.text` 可执行段** | ❌ 未实现 | 只处理 `secName == ".text"`，忽略 `.text.hot`、`.text.cold`、`.init`、`.fini` 等 | ❌ src-bc 无 PGO 分区信息；需扩展 section 过滤逻辑 |
| **TLS 描述符重定位** | ❌ 未实现 | `R_AARCH64_TLSDESC_*` 系列未处理 | ❌ src-bc 有 `thread_local` 属性但 TLS relocation 修饰符未在 ELFSymbolizer 中处理 |
| GOT 间接访问 | ⚠️ 部分实现 | 已处理 ADRP+LDR 修饰符，但 GOT 表本身的内容未生成 | ❌ GOT 表内容在链接时填充，.o 中为空；src-bc 无法补足 |
| **函数内 `.cfi_*` 中间指令** | ❌ 未实现 | 只生成 `.cfi_startproc/endproc`，中间的 `.cfi_def_cfa` 等缺失 | ❌ 信息在 .eh_frame 中但未解析；src-bc 无此信息 |
| 逐指令反汇编失败处理 | ⚠️ 基本实现 | 跳过 4 字节，无错误诊断 | — |

### 非 `.text` 可执行段详细分析

当前 `disassembleTextSection()` 只处理 `secName == ".text"`。需要扩展为以下之一：
- `secName.starts_with(".text")`（覆盖 `.text.hot`、`.text.cold`、`.text.unlikely`）
- 所有 `SHF_EXECINSTR` section（覆盖 `.init`、`.fini`、`.plt` 等）

`.plt` / `.plt.got` 是动态链接桩代码，不需要 lift。`.init` / `.fini` 是 CRT 启动代码，通常也不需要。

**真正重要的是 PGO 分区**：编译器用 `-fprofile-use` 可将热函数放在 `.text.hot`、冷函数放在 `.text.cold`，这些函数需要被 lift。

---

## 2. 数据段（.rodata / .data）

| 能力 | 状态 | 说明 | src-bc 能否补足 |
|------|------|------|----------------|
| Section header 生成 | ✅ 已实现 | `.section name,flags,@progbits` | — |
| 对齐指令 | ✅ 已实现 | `.p2align` | — |
| 符号标签 | ✅ 已实现 | 遍历 secSymMap | — |
| 合成标签 (section+addend) | ✅ 已实现 | `sectionOffsetLabels` — `.rodata.str1.1 + 0x10` → `__sec_N` | — |
| 绝对重定位 `.quad symbol` | ✅ 已实现 | `R_AARCH64_ABS64` / `ABS32` | — |
| Section symbol 替换为合成标签 | ✅ 已实现 | `.rodata.str1.1` → `__sec_N` | — |
| `.asciz` 字符串检测 | ✅ 已实现 | 检测 null-terminated printable 字符串 | — |
| `.byte` 原始字节 | ✅ 已实现 | 每 8 个一行 | — |
| `.size` 符号大小 | ✅ 已实现 | 数据段符号的 `.size` | — |
| 合并段 (.rodata.str1.1, .rodata.cst8) | ✅ 已实现 | 作为普通 section 处理 | — |
| 数据段内指针重定位的精确大小 | ⚠️ 基本实现 | 默认 8 字节，`R_AARCH64_ABS32` 时 4 字节 | — |
| 多重 addend 的 section symbol | ⚠️ 基本实现 | 同一 section 不同 addend 共享合成标签 | — |
| **`.init_array` / `.fini_array`** | ❌ 未实现 | 只处理 `SHT_PROGBITS`，跳过 `SHT_INIT_ARRAY` / `SHT_FINI_ARRAY` | ⚠️ src-bc 有 `@llvm.global_ctors` / `@llvm.global_dtors`，格式不同需转换 |
| **非符号重定位** | ❌ 未实现 | 如 `R_AARCH64_PREL32`（用于跳转表） | ❌ src-bc 无此信息 |
| **Thread-local 数据** (.tdata / .tbss) | ❌ 未实现 | TLS 变量需要 `@TLSDESC` / `@TPOFF` relocation | ❌ src-bc 有 `thread_local` 属性，但 TLS relocation 的 AArch64 修饰符尚未处理 |
| SHT_NOBITS 段 (非 BSS) | ❌ 未实现 | 如 `.tbss` (thread-local BSS) | — |

### `.init_array` / `.fini_array` 详细分析

`.init_array` / `.fini_array` section 包含 constructor/destructor 函数指针。当前 `generateDataSections()` 跳过它们，导致 recompiled binary 不会调用 `__attribute__((constructor))` / `__attribute__((destructor))` 函数。

两种补足路径：
1. **从 ELF 解析**：识别 `SHT_INIT_ARRAY` / `SHT_FINI_ARRAY`，将其中 `.quad` 指针作为 `.quad funcName` 输出
2. **从 src-bc 补足**：`@llvm.global_ctors` / `@llvm.global_dtors` 是常量数组，元素为 `{ i32 priority, void()* func, i8* data }`，可提取函数指针列表

路径 1 更直接（信息已在 ELF 中），路径 2 可获取优先级信息。

---

## 3. BSS Section

| 能力 | 状态 | 说明 | src-bc 能否补足 |
|------|------|------|----------------|
| `.comm` 声明 | ✅ 已实现 | 符号名 + size + align | — |
| **对齐精度** | ⚠️ 硬编码 | 固定 `align = 3`（2³ = 8 字节对齐） | ✅ **src-bc 可补**：`GlobalVariable::getAlignment()` 返回精确对齐 |
| **BSS 段中非 COMMON 符号** | ❌ 未实现 | 只处理 COMMON 符号，遗漏 SHT_NOBITS section 中定义的非 COMMON 全局变量 | ✅ src-bc 的 GlobalVariable 声明可识别，但 section 属性需从 ELF 获取 |

---

## 4. 全局信息（跨 Section）

| 信息 | src-bc 中的来源 | 当前是否使用 | 补足价值 |
|------|----------------|------------|---------|
| 全局变量类型 | `GlobalVariable::getValueType()` | ✅ 已用 | 高 — 让 lifter 知道全局变量是 `i32*` 而非 `i8*` |
| 全局变量对齐 | `GlobalVariable::getAlignment()` | ❌ 未用 | 中 — 可改善 BSS `.comm` 对齐精度 |
| 函数对齐 | `Function::getAlignment()` | ❌ 未用 | 低 — 通常 4 字节对齐已够 |
| 函数属性 (noinline 等) | `Function::getAttributes()` | ❌ 未用 | 低 — 不影响 lifting |
| `@llvm.global_ctors` / `@llvm.global_dtors` | `GlobalVariable` (常量数组) | ❌ 未用 | 高 — 可补足 `.init_array` / `.fini_array` |
| 函数参数名 | `Function::arg_begin()->getName()` | ❌ 未用 | 低 — 不影响 lifting |
| 类型定义 (struct, enum) | `srcModule->getTypeByName()` | ❌ 未用 | 低 — lifter 不需要 struct 布局信息 |
| 函数链接类型 (internal/linkonce 等) | `GlobalValue::Linkage` | ❌ 未用 | 中 — 可影响符号可见性 |
| 目标特性 (target-features) | `Function::getFnAttribute("target-features")` | ❌ 未用 | 低 — 不影响 lifting |
| 全局变量初始化值 | `GlobalVariable::getInitializer()` | ❌ 未用 | 低 — .o 的 data section 已有初始值 |

---

## 5. 总结

**当前完备性约 80%**：

- ✅ **核心路径完整**：.text 反汇编 + relocation 符号化 + 数据段 + BSS
- ❌ **主要缺口**（按优先级排序）：
  1. 非 `.text` 可执行段（PGO 分区）— 需扩展 section 过滤 + buildTextRelocMap
  2. `.init_array` / `.fini_array` — 需扩展 generateDataSections 或从 src-bc 提取
  3. BSS 对齐精度 — src-bc 可直接补足
  4. TLS 支持 — 需扩展 ELFSymbolizer
  5. 函数内 `.cfi_*` 中间指令 — 需解析 .eh_frame

- **src-bc 可补足 3 项**：BSS 对齐、.init_array/.fini_array（优先级 2-3）、链接类型
- **必须从 ELF 扩展的 2 项**：PGO 分区（优先级 1）、TLS relocation
