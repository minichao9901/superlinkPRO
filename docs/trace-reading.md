# Trace 阅读折叠与源码上下文

阅读模式用于学习已采集的执行过程。它在原生 TraceStore 上产生独立的 `etm-reading/1`
投影，不修改解码、执行三态、原始波形或质量结论。原有 `simplified/source/html/CSV`
导出仍保留；CLI 默认 `--readability original`。

## 使用

```powershell
python -X utf8 etm/trace.py export --record E:/trace-runs/record-001 `
  --format simplified --readability learning --out E:/exports/reading.c

python -X utf8 etm/trace.py export --record E:/trace-runs/record-001 `
  --format html --readability learning --out E:/exports/reading.html
```

每次生成三个新文件：正文、`<out>.projection.jsonl` 序号映射、`<out>.manifest.json` 清单。
三者均拒绝覆盖。`--projection-manifest` 可指定独立清单路径。可同时提供
`--from-seq/--to-seq` 限定原始执行序号范围；序号不是时间。

网页“导出格式”增加“阅读折叠 C + 映射 .zip”和“阅读折叠 HTML + 映射 .zip”。
ZIP 内含上述三个文件。只有本地服务声明阅读能力时才开放这两个选项；需使用包含
`reading-v1` 功能的原生导出器。旧 EXE 不会因源码更新自动获得该功能。

## 折叠规则

- 默认总重复次数**严格超过10次**才折叠：10次原样保留，11次开始折叠。
- 保留前2次和最后1次。通过普通代码注释表达，没有生成假的 `for` 循环。
- 比较整个可观察指令PC/执行状态序列、源码位置、函数和调用上下文；不按显示文本全局去重。
- 重复起点必须处在同一动态调用帧，期间调用/返回闭合。显式分支尾调用保留原帧返回地址，
  不伪造新的调用。递归层级和不同调用实例不会混合。
- 有完整已观察调用及返回、无未知状态、路径在预算内的无源码库调用可以参与外层重复匹配。
  其他无源码片段保留为独立摘要，标注入口/返回是否已证实。
- 中断进入/退出、Trace缺口、不同步等事件始终单独保留并切断重复。
  unknown 不与纯已知执行重复块合并。同一IT块的相邻未知指令可合为一条提示，指令数和PC映射保留。
- 折叠证明的是相同的**可观察路径**，不表示每次寄存器、变量、外设取值或业务作用相同。

选项：

| CLI 参数 | 默认值/含义 |
|---|---|
| `--repeat-threshold` | 10，严格大于此次数才折叠 |
| `--repeat-keep-first` / `--repeat-keep-last` | 2 / 1，均为正且合计不超过阈值 |
| `--repeat-max-block` | 128个源码节点，允许1～512 |
| `--no-fold-repeats` | 保留完整动态源码节点，仅关闭重复折叠 |
| `--no-fold-libraries` | 保留逐源码/无源码片段，不聚合库内部 |
| `--no-group-unknown` | 不归组合并未知指令 |
| `--no-statements` | 回退源码行上下文 |
| `--macro-view original` | 不加宏/枚举注释 |
| `--macro-view annotate` | 默认；已证实的整数宏/枚举紧随名称标注，如 `MASK/**0x32*/` |
| `--macro-view expanded` | 保留兼容参数；替换表达式保存在序号映射中，C/HTML仍保持紧凑展示 |

实现使用有限缓冲，最多65536个节点、1048576个路径字、128层调用；当前折叠深度为1。
较短内层重复即使不足阈值，也不会阻止外层重复识别。已折叠的长内层块暂不递归参与更高层压缩。
超过块长/缓冲预算的模式保留更多原文，不猜测、截断或删除事实。无源码单片段最多保留
4096条指令的直接路径；超出部分仍以完整序号范围指向原记录。未知位置另外保留，达到预算会拆段。

## 源码与宏的含义

多行语句通过C词法token、括号和结构边界确定上下文；字符串、注释和 `for` 头内的分号
不会被当成普通语句结束。单行多语句、超预算/不能唯一定位的结构回退行级展示。
PC跳回、调用/返回或分支变化会切开动态实例，不按同一行号消除真实重复。
显示完整语句**不是每行或每个token均独立执行的证明**。

宏值来自记录自带 ELF 的 `.debug_macro`，枚举来自 DWARF。按CU、各宏单元自己的行表、
include/import、define/undef与重定义顺序确定使用位置的定义，不读取当前同名头文件猜值。
重复include的定义不一致、CU重叠、枚举同名或可能被变量遮蔽时保守保留原名，未解析原因写入配套映射。

整数常量求值使用ARM32目标语义：int/long 32位，long long 64位；保留有符号/无符号运算，
无符号回绕、整数提升、除法取整及位运算。带未定义/实现定义语义、不支持的类型/表达式、
运行时变量等不猜值。不会调用宿主 `eval` 或执行宏。

函数宏支持词法实参替换和有预算的嵌套展开，保留运行时变量名。
字符串化、token拼接、可变参数和递归等暂未支持的情况在配套映射中写“未解析”；页面保留原调用。
展开的是编译时替换表达式，不是现场寄存器数值、参数或返回值。
本功能不生成 `.i`、不重编固件、不替换 ELF，也不重新采集。

## 映射、HTML和复核

JSONL第一行为 `header`，包含版本、选项、Store代/文件指纹、ELF与源码指纹及静态PC字典；
之后是连续覆盖整个选择范围的节点。`code/not-taken/unknown` 保存按序静态ID/状态路径。
`library` 保存指令计数、内部函数、条件未知位置及入口/返回标记。
`event` 保存原始边界。`repeat` 保存第一轮模板、原始序号步长和总次数：
第 `i` 轮的原始序号为模板序号加 `i * stride`。因此全部隐藏迭代可定位，而不用复制隐藏事件。

清单单列事件数、指令数、投影节点数、迭代数、未知数和隐藏指令数；它们不能互相替代。
清单和序号映射保留整份记录的质量及有预算的包诊断，C头注释保留质量计数。即使选择区间
没有覆盖记录末端，也不会把末尾不完整包悄悄变成“无问题”。诊断表超过预算时明确标为延后读取，
原始表保留。缺少可核实的关键质量字段时拒绝阅读导出，不假定为零。
宿主在发布文件之前另查连续覆盖、重复模板长度/计数和输出SHA256。
`reading_reference.py` 是仅用于小样本差分的Python参考，生产大记录仍直接由C++读取RUN。

HTML沿用原来的紧凑双栏：左侧按执行顺序显示源码，右侧点击定位原文；保留搜索、分页和序号定位。
可证实的数值宏/枚举在原名右侧标为 `MASK/**0x32*/`，不改动字符串、注释或其他标识符。
函数宏没有独立整数值时保留原调用。来源和替换表达式留在映射，不增加卡片、宏详情或质量面板。
重复块直接显示前2次和最后1次，中间只有一条 `/* 此执行块共重复 N 次；省略中间 M 次 */` 注释。
定位隐藏序号仍会按模板计算右侧源码位置。相同路径不表示每次运行时取值相同；完整明细请查配套记录：

```powershell
python -X utf8 etm/trace.py query --record E:/trace-runs/record-001 --view event --seq 1000000
python -X utf8 etm/trace.py query --record E:/trace-runs/record-001 --view page --offset 999950 --limit 150
```

HTML/JSON转义保留，显示使用文本节点。C对说明文字转义注释终止符；完整源码导出仍可用于旧格式对照。

## AI只读接口

```powershell
python -X utf8 etm/trace.py capabilities
python -X utf8 etm/trace.py query --record E:/trace-runs/record-001 --view summary --compact
python -X utf8 etm/trace.py query --record E:/trace-runs/record-001 --view summary `
  --fields summary.instructions,quality.unknownExecution,captureExtent.actualSeconds
```

能力查询包含当前原生工具实际指纹/自描述协议、阅读能力和分页限制。软件支持1s不等于所有
硬件条件都已录满1s或通过验收。`--compact` 只精简摘要大表；`--fields` 是字典字段的点分路径选择，
不存在字段报错，不补造默认值。stdout仍为单个JSON，日志走stderr。

本轮第5类建议选做能力查询、紧凑/字段选择和导出审阅统计；没有引入recipe调度、任意外部hook、
TCP/UDP业务参数、自动函数实例切段或未经扫描的导出体积预测。
