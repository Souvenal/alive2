# Backend-TV 数据流详解：MCStreamerWrapper 与 MCGlobals

本文档详细记录 backend-tv 中从汇编解析到 LLVM IR 生成的完整数据流，
重点关注 `MCStreamerWrapper` 的回调机制和 `MCGlobals` → `GlobalVariable` 的转化路径。

## 1. 核心组件

### 1.1 MCStreamer（LLVM 接口）

`llvm::MCStreamer`（`llvm/MC/MCStreamer.h`）是 LLVM MC 层的核心流式接口。
MCAsmParser 在解析汇编文本时，通过 MCStreamer 的虚函数回调来报告解析事件。
这是一个纯接口类，backend-tv 通过子类化来拦截这些回调。

### 1.2 MCStreamerWrapper（backend-tv 封装）

定义：`backend_tv/streamerwrapper.h`

继承自 `llvm::MCStreamer`，拦截以下关键回调：

| 回调方法 | 触发时机 | 数据去向 | 汇编中对应 |
|----------|----------|----------|------------|
| `emitLabel(Symbol)` | 遇到标签 `foo:` | 创建新 `MCBasicBlock` | 函数入口/跳转目标标签 |
| `emitInstruction(Inst)` | 遇到指令 | 添加到当前 `MCBasicBlock` | 代码段指令 |
| `emitBytes(Data)` | 遇到原始字节数据 | 追加到 `curROData` | `.rodata`/`.data` 中的 `.byte`/`.ascii` 等 |
| `emitFill(NumBytes, FillValue)` | 遇到填充 | 追加到 `curROData` | `.space`/`.zero N` |
| `emitValueImpl(Value, Size)` | 遇到值表达式 | 追加到 `curROData` | `.quad sym`/`.long sym+offset` |
| `emitCommonSymbol(Symbol, Size, Align)` | 遇到 COMMON 符号 | 直接添加到 `MF.MCglobals` | `.comm sym, size, align` |
| `emitZerofill(Section, Symbol, Size, Align)` | 遇到 BSS 段 | 仅打印日志（当前未存储） | `.zerofill` |
| `emitValueToAlignment(Align)` | 遇到对齐指令 | 更新 `curAlign` | `.p2align N` |
| `emitSymbolAttribute(Symbol, Attr)` | 遇到符号属性 | 仅打印日志 | `.globl` |
| `emitELFSize(Symbol, Value)` | 遇到 ELF size 指令 | 调用 `addConstant()` | `.size sym, .-sym` |
| `emitDwarfLocDirective(...)` | 遇到 DWARF loc 指令 | 更新 `curDebugLine` | `.loc N L C` |
| `emitAssignment(Symbol, Value)` | 遇到符号赋值 | 仅打印日志 | `sym = expr` |

### 1.3 MCGlobal（backend-tv 自定义）

定义：`backend_tv/streamerwrapper.h`

```cpp
struct MCGlobal {
  std::string name;           // 全局符号名
  llvm::Align align;          // 对齐要求
  std::string section;        // 所在段名 (".rodata", ".data", "common" 等)
  std::vector<RODataItem> data;  // 数据内容
};
```

其中 `RODataItem` 是 `std::variant<OffsetSym, char>`：
- `char`：原始字节数据
- `OffsetSym {sym, offset}`：符号引用 + 偏移（如 `.quad _mydata + 8`）

`MCFunction::MCglobals` 是 `std::vector<MCGlobal>`，存储从汇编中提取的所有全局数据。

### 1.4 MCFunction / MCBasicBlock

- `MCBasicBlock`：包含指令列表 + 后继基本块列表
- `MCFunction`：包含基本块列表 `BBs` + 全局数据列表 `MCglobals`

## 2. 完整数据流

### 2.1 汇编解析阶段（MCStreamerWrapper 收集数据）

```
输入: 标准汇编源码 (MemoryBuffer)
     ┌──────────────────────────────────────────────────────────────────┐
     │                     MCAsmParser::Run()                          │
     │                                                                  │
     │  逐行解析汇编文本，触发 MCStreamerWrapper 回调:                   │
     │                                                                  │
     │  遇到 .text 段:                                                  │
     │    .globl _foo        → emitSymbolAttribute(_foo, Global)        │
     │    _foo:              → emitLabel(_foo)                          │
     │                          → addConstant() [保存之前累积的 ROData] │
     │                          → 创建 MCBasicBlock("_foo")            │
     │    .cfi_startproc     → emitInstruction(CFI_NOP)                │
     │    mov x0, x1        → emitInstruction(MOV)                     │
     │    ret               → emitInstruction(RET)                     │
     │    .Lfunc_end0:       → emitLabel(.Lfunc_end0)                  │
     │                          → FunctionEnded = true                 │
     │                                                                  │
     │  遇到 .rodata 段:                                                │
     │    .section .rodata   → 切换当前 section                        │
     │    .p2align 3         → emitValueToAlignment(Align(8))          │
     │    _mydata:           → emitLabel(_mydata)                      │
     │                          → addConstant() [保存之前累积的 ROData]│
     │                          → curSym = "_mydata", curSec = ".rodata"│
     │    .quad 42           → emitValueImpl(ConstantExpr(42), 8)      │
     │                          → curROData.push_back(char(42))        │
     │    .quad _other_sym   → emitValueImpl(SymbolRefExpr, 8)        │
     │                          → curROData.push_back(OffsetSym{"_other_sym", 0})│
     │    .quad _sym+8       → emitValueImpl(BinaryExpr, 8)           │
     │                          → curROData.push_back(OffsetSym{"_sym", 8})│
     │    .size _mydata, .-_mydata → emitELFSize()                    │
     │                          → addConstant()                        │
     │                          → 创建 MCGlobal{name="_mydata",        │
     │                              section=".rodata", align=8,         │
     │                              data=[42, OffsetSym("_other_sym",0),│
     │                                    OffsetSym("_sym",8)]}        │
     │                          → MF.MCglobals.push_back(g)            │
     │                                                                  │
     │  遇到 BSS 段:                                                    │
     │    .comm _bss_var, 16, 8 → emitCommonSymbol(_bss_var, 16, 8)   │
     │                          → 创建 MCGlobal{name="_bss_var",       │
     │                              section="common", align=8,          │
     │                              data=['0'×16]}                     │
     │                          → MF.MCglobals.push_back(g)            │
     └──────────────────────────────────────────────────────────────────┘
```

### 2.2 addConstant() 机制

`addConstant()` 是将累积的 `curROData` 归档到 `MF.MCglobals` 的关键方法：

```cpp
void MCStreamerWrapper::addConstant() {
  if (curROData.empty()) return;
  MCGlobal g{
    .name = curSym,        // 上一个 emitLabel 设置的符号名
    .align = curAlign,     // 上一个 emitValueToAlignment 设置的对齐
    .section = curSec,     // 当前 section 名
    .data = curROData,     // 累积的数据
  };
  MF.MCglobals.emplace_back(g);
  curROData.clear();
}
```

调用时机：
1. `emitLabel()` — 新符号出现，保存之前累积的数据
2. `emitELFSize()` — `.size` 指令标志数据定义结束

### 2.3 指令提升阶段（MCBasicBlock → LLVM IR）

```
mc2llvm::run() 中:
  1. Parser->Run(true)           → MCStreamerWrapper 收集 MF.BBs + MF.MCglobals
  2. Str->removeEmptyBlocks()    → 清理空 BB
  3. Str->checkEntryBlock()      → 插入 entry BB（如果入口是跳转目标）
  4. Str->generateSuccessors()   → 构建 CFG
  5. 创建 liftedFn + LLVM BasicBlocks（一一对应 MCFunction.BBs）
  6. platformInit()              → 初始化寄存器、栈帧
  7. 逐 BB 逐指令: liftInst() → lift() → 生成 LLVM IR 指令
```

### 2.4 全局变量生成阶段（MCGlobals → LLVM GlobalVariable）

当指令提升过程中遇到 ADRP/LDR 等引用全局符号时，触发 `lazyAddGlobal()`：

```
arm 指令: ADRP x0, _mydata
  → liftInst() → lift() → arm2llvm 处理 ADRP
  → lookupExprVar(expr) → lookupGlobal("_mydata")
  → lazyAddGlobal("_mydata")
```

`lazyAddGlobal()` 按以下优先级查找全局符号：

```
lazyAddGlobal(newGlobal):
  1. intrinsic_names 映射？→ 重命名
  2. "__stack_chk_fail"？  → 创建 ExternalLinkage GlobalVariable
  3. 是 liftedFn 自身？   → 返回 liftedFn
  4. srcFn->getParent() 中有同名函数？ → copyFunctionToTarget()
  5. Str->MF.MCglobals 中有同名定义？ → ★ 从 MCGlobal 创建 GlobalVariable
  6. srcFn->getParent()->globals() 中有声明？ → 创建 ExternalLinkage 声明
  7. implicit_intrinsics 中有？ → 创建函数声明
  8. 以上都不匹配 → ERROR: global symbol not found
```

**步骤 5 是关键**——从 `MCGlobal` 创建 `GlobalVariable` 的详细逻辑：

```cpp
for (const auto &g : Str->MF.MCglobals) {
  if (demangle(g.name) != newGlobal) continue;

  // 遍历 g.data，构建 LLVM 类型 + 常量值
  for (auto &item : g.data) {
    if (holds_alternative<char>(item)) {
      // 原始字节 → i8 常量
      tys.push_back(getIntTy(8));
      vals.push_back(ConstantInt::get(getIntTy(8), get<char>(item)));
    } else if (holds_alternative<OffsetSym>(item)) {
      // 符号引用 → 指针常量 (可能递归触发 lazyAddGlobal)
      auto s = get<OffsetSym>(item);
      Constant *con = lookupGlobal(s.sym);  // 可能创建 dummy 或递归
      Constant *offset = getUnsignedIntConst(s.offset, 64);
      Constant *ptr = ConstantExpr::getGetElementPtr(getIntTy(8), con, offset);
      tys.push_back(PointerType::get(Ctx, 0));
      vals.push_back(ptr);
    }
  }

  // 创建 StructType 作为 GlobalVariable 的类型
  auto *ty = StructType::create(tys);
  auto initializer = ConstantStruct::get(ty, vals);
  bool isConstant = g.section.starts_with(".rodata") || g.section == ".text";

  // 创建 GlobalVariable
  auto *glob = new GlobalVariable(*LiftedModule, ty, isConstant,
                                  ExternalLinkage, initializer, name);
  glob->setAlignment(g.align);
  return glob;
}
```

### 2.5 递归引用处理（deferredGlobs 机制）

当 `MCGlobal` 的数据引用了另一个尚未创建的全局符号时：

```
例: _mydata 包含 .quad _other_data，但 _other_data 尚未被处理

1. lazyAddGlobal("_mydata") 处理 _mydata
2. 遇到 OffsetSym{"_other_data", 0}
3. lookupGlobal("_other_data") → LLVMglobals 中没有
4. 创建 dummy GlobalVariable ("_other_data_tmp")
5. 加入 deferredGlobs 队列
6. 用 dummy 完成当前 _mydata 的初始化

7. lookupGlobal() 返回后，处理 deferredGlobs:
   while (!deferredGlobs.empty()) {
     auto def = deferredGlobs.pop_front();
     auto g2 = lazyAddGlobal(def.name);   // 递归创建真正的全局变量
     def.val->replaceAllUsesWith(g2);      // 替换 dummy
     def.val->eraseFromParent();           // 删除 dummy
   }
```

## 3. generateAsm() 输出的汇编格式

`generateAsm()` 使用 LLVM 后端的 `MCAsmPrinter` 生成**完整标准汇编**，
包含所有段的内容。典型输出格式：

```asm
        .text
        .globl  _foo
        .p2align  2
        .type   _foo,@function
_foo:
        .cfi_startproc
        .loc    1 0 0
        mov     x0, x1
        .loc    1 1 0
        ret
        .Lfunc_end0:
        .size   _foo, .Lfunc_end0-_foo
        .cfi_endproc

        .section        .rodata,"a",@progbits
        .p2align  3
_mydata:
        .quad   42
        .quad   _other_data
        .size   _mydata, 16

        .comm   _bss_var,16,8
```

关键特征：
- `.text` 段：包含 `.cfi_startproc`/`.cfi_endproc`、`.loc` 调试信息
- `.rodata`/`.data` 段：包含 `.quad`/`.long`/`.byte` 等数据声明和符号引用
- `.comm`：BSS 段未初始化数据
- 所有符号都有明确的 `.size` 声明

这种格式能被 `MCAsmParser` 正确解析，触发 `MCStreamerWrapper` 的所有回调。

## 4. llvm-objdump -d 输出格式（对比）

```asm
Disassembly of section __TEXT,__text:

_main:
       0:       aa0103e0        mov     x0, x1
       4:       d65f03c0        ret
```

关键缺失：
- ❌ 无 `.rodata`/`.data`/`.bss` 段内容
- ❌ 无 `.quad`/`.byte` 等数据声明
- ❌ 无 `.globl`/`.type`/`.size` 等符号属性
- ❌ 无 `.cfi_startproc`/`.cfi_endproc`
- ❌ 有地址前缀和十六进制编码（不是合法汇编源码）

## 5. 数据流总图

```
                    ┌─────────────────────────────────────────┐
                    │           标准汇编源码 (MemoryBuffer)      │
                    └────────────────┬────────────────────────┘
                                     │
                                     ▼
                    ┌─────────────────────────────────────────┐
                    │          MCAsmParser::Run()              │
                    │         逐行解析汇编文本                   │
                    └────────────────┬────────────────────────┘
                                     │ MCStreamer 回调
                    ┌────────────────┴────────────────────────┐
                    │                                         │
            ┌───────▼────────┐                      ┌────────▼────────┐
            │  代码段回调      │                      │  数据段回调       │
            │  emitLabel()   │                      │  emitBytes()    │
            │  emitInstruction()│                    │  emitFill()     │
            │                │                      │  emitValueImpl()│
            │                │                      │  emitCommonSymbol()│
            └───────┬────────┘                      │  addConstant()  │
                    │                               └────────┬────────┘
                    ▼                                        ▼
           ┌────────────────┐                      ┌──────────────────┐
           │ MCFunction.BBs │                      │MCFunction.MCglobals│
           │ (MCBasicBlock  │                      │ (MCGlobal:       │
           │  + MCInst)     │                      │  name/align/     │
           └───────┬────────┘                      │  section/data)   │
                   │                               └────────┬──────────┘
                   ▼                                        │
    ┌──────────────────────────┐                           │
    │  arm2llvm::run()         │                           │
    │  逐 BB 逐指令 lift       │                           │
    │  → LLVM IR 指令          │                           │
    │                          │                           │
    │  ADRP/LDR 引用全局符号时  │◄──────────────────────────┘
    │  → lazyAddGlobal()       │     查找 MCGlobal 定义
    │    → MCGlobal → GlobalVariable                    │
    └──────────┬───────────────┘
               ▼
    ┌──────────────────────────┐
    │   LiftedModule           │
    │   ├── liftedFn (IR 函数)  │
    │   └── GlobalVariables    │
    │       (从 MCGlobals 创建) │
    └──────────────────────────┘
```

## 6. 涉及文件索引

| 组件 | 文件 | 行号范围 |
|------|------|----------|
| MCStreamerWrapper 类定义 | `backend_tv/streamerwrapper.h` | 131-284 |
| MCStreamerWrapper 回调实现 | `backend_tv/streamerwrapper.cpp` | 全文 |
| MCGlobal / MCFunction 定义 | `backend_tv/streamerwrapper.h` | 66-123 |
| mc2llvm 类定义 | `backend_tv/mc2llvm.h` | 全文 |
| lazyAddGlobal 实现 | `backend_tv/mc2llvm.cpp` | 36-172 |
| lookupGlobal 实现 | `backend_tv/mc2llvm.cpp` | 174-200 |
| mc2llvm::run 实现 | `backend_tv/mc2llvm.cpp` | 631-782 |
| liftFunc 实现 | `backend_tv/lifter.cpp` | 74-113 |
| generateAsm 声明 | `backend_tv/lifter.h` | 28-30 |
