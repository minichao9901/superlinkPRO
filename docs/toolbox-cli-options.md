# CLI 命令与参数完整参考

由 `python tools/build_cli_options.py` 从真实 argparse 解析器生成；`--check` 检查是否过期。

总工作流见 [AI 总指南](toolbox-cli-ai.md)，Trace 详解见 [Trace 指南](trace-cli-ai.md)。

此处逐条覆盖命令行语法。`page invoke` 是高级网页方法适配器：内部方法的签名/源码位置不等于完整参数类型文档，复杂参数须检查源码，不可猜测。

`toolbox.py` 全局选项放在子命令前；`trace` 必须紧接 toolbox.py，其全局选项放在 trace 后、Trace 子命令前。

统一入口除 catalog、trace 外的命令必须提供全局 --runtime；该条件由运行时检查，表中的必填列显示 argparse 层规则。

默认值为解析器默认值；环境变量未设置时的 Trace 默认值在此固定显示。必填互斥组也列出。

## toolbox.py

Unified AI entry: python -X utf8 toolbox.py --help. See docs/toolbox-cli-ai.md.

```text
usage: toolbox.py [-h] [--runtime RUNTIME] [--timeout TIMEOUT] [--out OUT]
                  {catalog,serve,ping,status,job,ack,request,batch,page,trace} ...
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --runtime | serve创建的工作台运行时目录（不是trace native运行时） | str | 否 | 未指定 | — |
| --timeout | 等待结果秒数；超时不取消/不重试 | float | 否 | 60 | — |
| --out | JSON结果文件，拒绝覆盖 | str | 否 | 未指定 | — |

## toolbox.py catalog

离线列出全部页面分类和功能范围

```text
usage: toolbox.py catalog [-h] [--full]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --full | 输出总清单，包括静态控件、选项和方法源码位置 | flag × 0 | 否 | False | — |

## toolbox.py serve

启动本机工作台和CLI队列，前台运行

```text
usage: toolbox.py serve [-h] [--port PORT]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --port | 本机工作台 HTTP 监听端口，1024～65535；只绑定127.0.0.1。 | int | 否 | 8896 | — |

## toolbox.py ping

检查浏览器worker就绪，不访问设备

```text
usage: toolbox.py ping [-h]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |

## toolbox.py status

工作台摘要

```text
usage: toolbox.py status [-h]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |

## toolbox.py job

查询超时/历史操作结果，不重试

```text
usage: toolbox.py job [-h] id
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| id | job/ack 为队列返回的32位十六进制 job ID；download/discard 为 downloads 返回的 Blob ID，二者不能混用。 | str | 是 | 未指定 | — |

## toolbox.py ack

删除已完成job结果，释放队列容量

```text
usage: toolbox.py ack [-h] id
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| id | job/ack 为队列返回的32位十六进制 job ID；download/discard 为 downloads 返回的 Blob ID，二者不能混用。 | str | 是 | 未指定 | — |

## toolbox.py request

发送一条JSON请求文件，支持已有Trace浏览器动作

```text
usage: toolbox.py request [-h] file
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| file | UTF-8 JSON 请求文件；request 为单个对象，batch 为1～1000个对象数组。 | str | 是 | 未指定 | — |

## toolbox.py batch

顺序执行JSON数组；首个失败停止，非事务

```text
usage: toolbox.py batch [-h] file
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| file | UTF-8 JSON 请求文件；request 为单个对象，batch 为1～1000个对象数组。 | str | 是 | 未指定 | — |

## toolbox.py page

按分类操作真实Web页面

```text
usage: toolbox.py page [-h]
                       {serial,terminal,rtt,rttcdc,spicdc,scope,dbg,flash,swo,etm,spi,panel,i2c,analog,gen}
                       {describe,state,show,downloads,download,discard,event,read,click,interrupt,set,upload,wait,invoke} ...
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| page | 网页功能分类，取下面 choices 中的值；先 describe 再操作。 | str | 是 | 未指定 | ['serial', 'terminal', 'rtt', 'rttcdc', 'spicdc', 'scope', 'dbg', 'flash', 'swo', 'etm', 'spi', 'panel', 'i2c', 'analog', 'gen'] |

## toolbox.py page <page> describe

枚举当前控件、可选值、目标子对象、方法签名和停止白名单。

```text
usage: toolbox.py page <page> describe [-h] [--target TARGET]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --target | view/session等，以describe返回的targets为准 | str | 否 | view | — |

## toolbox.py page <page> state

读取页面摘要、文字与近期错误，不代表任务已成功。

```text
usage: toolbox.py page <page> state [-h]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |

## toolbox.py page <page> show

切换当前显示页面，仅投递切换事件。

```text
usage: toolbox.py page <page> show [-h]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |

## toolbox.py page <page> downloads

列出该页面暂存的 Blob 导出。

```text
usage: toolbox.py page <page> downloads [-h]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |

## toolbox.py page <page> download

分块保存暂存的Blob导出；拒绝覆盖

```text
usage: toolbox.py page <page> download [-h] id destination
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| id | job/ack 为队列返回的32位十六进制 job ID；download/discard 为 downloads 返回的 Blob ID，二者不能混用。 | str | 是 | 未指定 | — |
| destination | 下载到本机的新文件完整路径，拒绝覆盖；失败保留 .partial。 | str | 是 | 未指定 | — |

## toolbox.py page <page> discard

释放浏览器暂存的导出

```text
usage: toolbox.py page <page> discard [-h] id
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| id | job/ack 为队列返回的32位十六进制 job ID；download/discard 为 downloads 返回的 Blob ID，二者不能混用。 | str | 是 | 未指定 | — |

## toolbox.py page <page> event

键盘/鼠标/指针/滚轮事件（仍受浏览器用户手势限制）

```text
usage: toolbox.py page <page> event [-h] [--data DATA]
                                    selector
                                    {keydown,keyup,mousedown,mousemove,mouseup,pointerdown,pointermove,pointerup,wheel,dblclick,click,input,change}
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| selector | describe 返回的唯一控件 selector；也支持其返回的 @file: 虚拟文件入口。 | str | 是 | 未指定 | — |
| event | 待分发的浏览器事件类型，参数通过 --data JSON 对象提供。 | str | 是 | 未指定 | ['keydown', 'keyup', 'mousedown', 'mousemove', 'mouseup', 'pointerdown', 'pointermove', 'pointerup', 'wheel', 'dblclick', 'click', 'input', 'change'] |
| --data | JSON事件参数；x/y为相对控件左上角的像素 | str | 否 | {} | — |

## toolbox.py page <page> read

读取唯一控件的值和文字。

```text
usage: toolbox.py page <page> read [-h] selector
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| selector | describe返回的CSS selector，必须唯一匹配 | str | 是 | 未指定 | — |

## toolbox.py page <page> click

点击控件，仅表示投递，需另查业务完成状态。

```text
usage: toolbox.py page <page> click [-h] selector
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| selector | describe返回的CSS selector，必须唯一匹配 | str | 是 | 未指定 | — |

## toolbox.py page <page> interrupt

长任务期间请求停止，只允许停止白名单中的控件。

```text
usage: toolbox.py page <page> interrupt [-h] selector
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| selector | describe返回的CSS selector，必须唯一匹配 | str | 是 | 未指定 | — |

## toolbox.py page <page> set

设置参数并发送 input/change，checkbox 必须为 JSON boolean。

```text
usage: toolbox.py page <page> set [-h] [--value-file] selector value
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| selector | describe返回的CSS selector，必须唯一匹配 | str | 是 | 未指定 | — |
| value | JSON值；字符串需用JSON引号，也可 --value-file | str | 是 | 未指定 | — |
| --value-file | value视为JSON文件路径 | flag × 0 | 否 | False | — |

## toolbox.py page <page> upload

经原网页 file input 导入文件；事件投递不代表解析完成。

```text
usage: toolbox.py page <page> upload [-h] [--directory]
                                     selector files [files ...]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| selector | describe返回的CSS selector，必须唯一匹配 | str | 是 | 未指定 | — |
| files | 本机文件路径，可多文件；--directory 时仅接受一个精简源码目录。 | str × + | 是 | 未指定 | — |
| --directory | 上传一个源码目录，保留相对路径 | flag × 0 | 否 | False | — |

## toolbox.py page <page> wait

轮询控件文字直到匹配或超时，不代替采集质量检查。

```text
usage: toolbox.py page <page> wait [-h] --contains CONTAINS selector
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| selector | describe返回的CSS selector，必须唯一匹配 | str | 是 | 未指定 | — |
| --contains | 等待控件文本包含内容；仍需检查业务质量 | str | 是 | 未指定 | — |

## toolbox.py page <page> invoke

调用describe公开的原方法，等待其Promise返回

```text
usage: toolbox.py page <page> invoke [-h] [--target TARGET] [--args ARGS]
                                     [--args-file ARGS_FILE]
                                     method
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| method | describe.methods 中真实公开的方法名，不支持任意对象路径、构造函数和私有方法。 | str | 是 | 未指定 | — |
| --target | 以describe返回的targets为准 | str | 否 | view | — |
| --args | JSON位置参数数组；大参数可用--args-file | str | 否 | [] | — |
| --args-file | UTF-8 JSON 位置参数数组文件；参数内支持 $file 引用，规则见总指南。 | str | 否 | 未指定 | — |

## toolbox.py trace

AI Trace CLI；硬件复用网页命令与WebUSB；JSON输出，日志写stderr。

```text
usage: toolbox.py trace [-h] [--json-out JSON_OUT] [--web-url WEB_URL]
                        [--token-file TOKEN_FILE] [--runtime RUNTIME]
                        {capabilities,doctor,elf,web,target,capture,decode,query,verify,export,import-record,record} ...
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --json-out | 将本次结果另存为新JSON文件（不能覆盖） | Path | 否 | 未指定 | — |
| --web-url | 启用CLI队列的本地工作台服务 | str | 否 | http://127.0.0.1:8878 | — |
| --token-file | 本机CLI令牌文件；必须与服务启动参数一致 | Path | 否 | 未指定 | — |
| --runtime | 使用web start创建的运行时目录，自动读取服务URL和令牌 | Path | 否 | 未指定 | — |

## toolbox.py trace capabilities

软件能力/分页限制/阅读schema及验收边界；不访问硬件

```text
usage: toolbox.py trace capabilities [-h]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |

## toolbox.py trace doctor

检查依赖；--hardware才枚举硬件

```text
usage: toolbox.py trace doctor [-h] [--hardware] [--la-host LA_HOST]
                               [--la-port LA_PORT]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --hardware | 除软件依赖检查外，还枚举硬件/查询 LA 服务；默认不访问硬件。 | flag × 0 | 否 | False | — |
| --la-host | KingstVIS / LA 控制服务主机地址。 | str | 否 | 127.0.0.1 | — |
| --la-port | KingstVIS / LA 控制服务 TCP 端口。 | int | 否 | 23367 | — |

## toolbox.py trace elf

离线ELF函数/源文件列表与精确断点解析

```text
usage: toolbox.py trace elf [-h] {inspect,resolve} ...
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |

## toolbox.py trace elf inspect

离线查看 ELF 符号/文件，或回读目标当前配置，取决于上一级分类。

```text
usage: toolbox.py trace elf inspect [-h] --elf ELF [--search SEARCH]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --elf | 与采集/目标固件匹配的 ELF 文件路径；仅提供文件不代表烧录。 | Path | 是 | 未指定 | — |
| --search | 搜索文本；elf inspect 筛选符号，query 按所选视图筛选。 | str | 否 |  | — |

## toolbox.py trace elf resolve

从匹配 ELF 解析精确断点候选。

```text
usage: toolbox.py trace elf resolve [-h] --elf ELF --spec SPEC
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --elf | 与采集/目标固件匹配的 ELF 文件路径；仅提供文件不代表烧录。 | Path | 是 | 未指定 | — |
| --spec | 断点表达式：函数名、文件:行号或 *0x地址。 | str | 是 | 未指定 | — |

## toolbox.py trace web

直接使用网页烧录、调试命令和持久会话

```text
usage: toolbox.py trace web [-h]
                            {status,connect,disconnect,load-elf,command,help,flash,wait-halt,cancel,start,stop} ...
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |

## toolbox.py trace web status

查询原生运行时/调试会话状态。

```text
usage: toolbox.py trace web status [-h] [--probe PROBE]
                                   [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                   [--hse HSE] [--swd-hz SWD_HZ]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |

## toolbox.py trace web connect

连接选定探针和目标调试会话，会占用设备。

```text
usage: toolbox.py trace web connect [-h] [--probe PROBE]
                                    [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                    [--hse HSE] [--swd-hz SWD_HZ]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |

## toolbox.py trace web disconnect

正常释放目标和探针会话。

```text
usage: toolbox.py trace web disconnect [-h] [--probe PROBE]
                                       [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                       [--hse HSE] [--swd-hz SWD_HZ]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |

## toolbox.py trace web load-elf

载入目标匹配的 ELF，供符号与断点使用，不执行烧录。

```text
usage: toolbox.py trace web load-elf [-h] [--probe PROBE]
                                     [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                     [--hse HSE] [--swd-hz SWD_HZ] --file FILE
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |
| --file | 本地文件路径；web load-elf/flash 读取镜像，query source 用作源码路径筛选。 | Path | 是 | 未指定 | — |

## toolbox.py trace web command

执行网页调试命令，命令语法以 web help 返回为准。

```text
usage: toolbox.py trace web command [-h] [--probe PROBE]
                                    [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                    [--hse HSE] [--swd-hz SWD_HZ]
                                    [--execute EXECUTE] [--script SCRIPT]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |
| --execute / -e | 原样传给网页调试器；可重复 | str | 否 | 未指定 | — |
| --script | 调试命令脚本文件，每行传给网页原命令解释器。 | Path | 否 | 未指定 | — |

## toolbox.py trace web help

返回网页真实调试命令帮助。

```text
usage: toolbox.py trace web help [-h] [--probe PROBE]
                                 [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                 [--hse HSE] [--swd-hz SWD_HZ]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |

## toolbox.py trace web flash

执行网页原烧录流程，校验后复位运行，会修改 Flash 和目标状态。

```text
usage: toolbox.py trace web flash [-h] [--probe PROBE]
                                  [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                  [--hse HSE] [--swd-hz SWD_HZ] --file FILE
                                  --chip CHIP [--base BASE]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |
| --file | 本地文件路径；web load-elf/flash 读取镜像，query source 用作源码路径筛选。 | Path | 是 | 未指定 | — |
| --chip | 网页烧录器芯片选项值 | str | 是 | 未指定 | — |
| --base | BIN 等镜像基地址；接受十进制或 0x 十六进制，以芯片和实际镜像布局为准。 | <lambda> | 否 | 0 | — |

## toolbox.py trace web wait-halt

等待目标暂停，可要求停在指定 PC；不会擅自复位。

```text
usage: toolbox.py trace web wait-halt [-h] [--probe PROBE]
                                      [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                      [--hse HSE] [--swd-hz SWD_HZ]
                                      [--timeout TIMEOUT] [--pc PC]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |
| --timeout | 等待结果的时间，单位秒；Trace 目标等待要求大于 0 且不超过 180。超时不代表任务已停止。 | float | 否 | 15 | — |
| --pc | 期望/查找的 PC 地址，接受十进制或 0x 十六进制。 | <lambda> | 否 | 未指定 | — |

## toolbox.py trace web cancel

请求取消当前受支持的调试等待；不保证可打断烧录。

```text
usage: toolbox.py trace web cancel [-h] [--probe PROBE]
                                   [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                   [--hse HSE] [--swd-hz SWD_HZ]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |

## toolbox.py trace web start

启动原生 WebUSB worker 运行时，不是浏览器页面运行时。

```text
usage: toolbox.py trace web start [-h] [--probe PROBE]
                                  [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                  [--hse HSE] [--swd-hz SWD_HZ]
                                  --runtime-dir RUNTIME_DIR
                                  [--image-root IMAGE_ROOT]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |
| --runtime-dir | 原生 WebUSB worker 运行时目录；start 要求新目录，stop 使用原目录。 | Path | 是 | 未指定 | — |
| --image-root | 本地load_image/dump_image允许访问的镜像目录 | Path | 否 | 未指定 | — |

## toolbox.py trace web stop

正常停止运行时或 Trace；行为取决于上级 web/target。

```text
usage: toolbox.py trace web stop [-h] [--probe PROBE]
                                 [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                 [--hse HSE] [--swd-hz SWD_HZ]
                                 --runtime-dir RUNTIME_DIR
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |
| --runtime-dir | 原生 WebUSB worker 运行时目录；start 要求新目录，stop 使用原目录。 | Path | 是 | 未指定 | — |

## toolbox.py trace target

目标回读、配置、恢复、停止

```text
usage: toolbox.py trace target [-h]
                               {inspect,configure,restore,stop,halt,run,recover,run-to,reset} ...
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |

## toolbox.py trace target inspect

离线查看 ELF 符号/文件，或回读目标当前配置，取决于上一级分类。

```text
usage: toolbox.py trace target inspect [-h] [--probe PROBE]
                                       [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                       [--hse HSE] [--swd-hz SWD_HZ]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |

## toolbox.py trace target configure

校验 ELF 并配置 Trace，保存可核验的恢复状态。

```text
usage: toolbox.py trace target configure [-h] [--probe PROBE]
                                         [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                         [--hse HSE] [--swd-hz SWD_HZ]
                                         --state STATE --elf ELF [--resume]
                                         [--trace-id {1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63,64,65,66,67,68,69,70,71,72,73,74,75,76,77,78,79,80,81,82,83,84,85,86,87,88,89,90,91,92,93,94,95,96,97,98,99,100,101,102,103,104,105,106,107,108,109,110,111}]
                                         [--pin-speed {1,2,3}]
                                         [--branch-broadcast]
                                         [--return-stack | --no-return-stack]
                                         [--cycles | --no-cycles]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |
| --state | Trace 配置恢复状态 JSON 路径；configure 保存，restore 读取并核验目标身份/状态。 | str | 是 | 未指定 | — |
| --elf | 与采集/目标固件匹配的 ELF 文件路径；仅提供文件不代表烧录。 | Path | 是 | 未指定 | — |
| --resume | 配置成功后继续运行目标。 | flag × 0 | 否 | False | — |
| --trace-id | ETM Trace ID，整数 1～111；必须与采集配置/解码配置一致。 | int | 否 | 2 | [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111] |
| --pin-speed | Trace GPIO 输出速度档 1/2/3；不是 Trace 时钟频率。 | int | 否 | 2 | [1, 2, 3] |
| --branch-broadcast | 启用 ETM 分支广播；默认关闭，会影响数据量。 | flag × 0 | 否 | False | — |
| --return-stack / --no-return-stack | 启用返回栈；--no-return-stack 关闭。 | flag × 0 | 否 | True | — |
| --cycles / --no-cycles | 启用周期计数；--no-cycles 关闭。 | flag × 0 | 否 | True | — |

## toolbox.py trace target restore

核验目标身份和当前配置后恢复保存的 Trace 状态。

```text
usage: toolbox.py trace target restore [-h] [--probe PROBE]
                                       [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                       [--hse HSE] [--swd-hz SWD_HZ]
                                       --state STATE
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |
| --state | Trace 配置恢复状态 JSON 路径；configure 保存，restore 读取并核验目标身份/状态。 | str | 是 | 未指定 | — |

## toolbox.py trace target stop

正常停止运行时或 Trace；行为取决于上级 web/target。

```text
usage: toolbox.py trace target stop [-h] [--probe PROBE]
                                    [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                    [--hse HSE] [--swd-hz SWD_HZ] [--halt]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |
| --halt | 停止 Trace 后保持目标暂停，不自动继续。 | flag × 0 | 否 | False | — |

## toolbox.py trace target halt

暂停目标 CPU。

```text
usage: toolbox.py trace target halt [-h] [--probe PROBE]
                                    [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                    [--hse HSE] [--swd-hz SWD_HZ]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |

## toolbox.py trace target run

继续运行目标 CPU。

```text
usage: toolbox.py trace target run [-h] [--probe PROBE]
                                   [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                   [--hse HSE] [--swd-hz SWD_HZ]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |

## toolbox.py trace target recover

尝试恢复目标调试口并保持暂停。

```text
usage: toolbox.py trace target recover [-h] [--probe PROBE]
                                       [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                       [--hse HSE] [--swd-hz SWD_HZ]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |

## toolbox.py trace target run-to

不复位地运行至指定起点。

```text
usage: toolbox.py trace target run-to [-h] [--probe PROBE]
                                      [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                      [--hse HSE] [--swd-hz SWD_HZ] --elf ELF
                                      --entry ENTRY [--address ADDRESS]
                                      [--timeout TIMEOUT]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |
| --elf | 与采集/目标固件匹配的 ELF 文件路径；仅提供文件不代表烧录。 | Path | 是 | 未指定 | — |
| --entry | 采集/运行起点：函数、文件:行号或 *0x地址，必须能在匹配 ELF 中解析。 | str | 是 | 未指定 | — |
| --address | 精确选择断点地址，接受十进制或 0x 十六进制。 | <lambda> | 否 | 未指定 | — |
| --timeout | 等待结果的时间，单位秒；Trace 目标等待要求大于 0 且不超过 180。超时不代表任务已停止。 | float | 否 | 15 | — |

## toolbox.py trace target reset

复位并运行至指定起点。

```text
usage: toolbox.py trace target reset [-h] [--probe PROBE]
                                     [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                     [--hse HSE] [--swd-hz SWD_HZ] --elf ELF
                                     --entry ENTRY [--address ADDRESS]
                                     [--timeout TIMEOUT]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |
| --elf | 与采集/目标固件匹配的 ELF 文件路径；仅提供文件不代表烧录。 | Path | 是 | 未指定 | — |
| --entry | 采集/运行起点：函数、文件:行号或 *0x地址，必须能在匹配 ELF 中解析。 | str | 是 | 未指定 | — |
| --address | 精确选择断点地址，接受十进制或 0x 十六进制。 | <lambda> | 否 | 未指定 | — |
| --timeout | 等待结果的时间，单位秒；Trace 目标等待要求大于 0 且不超过 180。超时不代表任务已停止。 | float | 否 | 15 | — |

## toolbox.py trace capture

校验ELF→配置→预运行→KVDAT→恢复→原生解码

```text
usage: toolbox.py trace capture [-h] [--probe PROBE]
                                [--profile {auto,stm32h743,stm32h7b0,stm32f103ze,stm32f1,stm32f4}]
                                [--hse HSE] [--swd-hz SWD_HZ]
                                [--la-host LA_HOST] [--la-port LA_PORT]
                                [--trace-id {1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63,64,65,66,67,68,69,70,71,72,73,74,75,76,77,78,79,80,81,82,83,84,85,86,87,88,89,90,91,92,93,94,95,96,97,98,99,100,101,102,103,104,105,106,107,108,109,110,111}]
                                [--pin-speed {1,2,3}] [--branch-broadcast]
                                [--return-stack | --no-return-stack]
                                [--cycles | --no-cycles]
                                [--sources-json SOURCES_JSON]
                                [--source-root SOURCE_ROOT] --elf ELF
                                --out OUT [--seconds SECONDS] [--rate RATE]
                                [--clock CLOCK] [--data DATA DATA DATA DATA]
                                [--fraction FRACTION] [--glitch-ns GLITCH_NS]
                                [--threshold THRESHOLD] [--no-decode]
                                [--echo-port ECHO_PORT]
                                [--echo-interval ECHO_INTERVAL]
                                [--expect-core-hz EXPECT_CORE_HZ]
                                [--expect-trace-hz EXPECT_TRACE_HZ]
                                [--entry ENTRY] [--address ADDRESS]
                                [--reset-to-entry]
                                [--entry-timeout ENTRY_TIMEOUT]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --probe | 精确探针序列号；只连一个时可省略 | str | 否 | 未指定 | — |
| --profile | 目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。 | str | 否 | stm32h743 | ['auto', 'stm32h743', 'stm32h7b0', 'stm32f103ze', 'stm32f1', 'stm32f4'] |
| --hse | HSE MHz，仅用于计算，不修改PLL | float | 否 | 25 | — |
| --swd-hz | SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。 | int | 否 | 1000000 | — |
| --la-host | KingstVIS / LA 控制服务主机地址。 | str | 否 | 127.0.0.1 | — |
| --la-port | KingstVIS / LA 控制服务 TCP 端口。 | int | 否 | 23367 | — |
| --trace-id | ETM Trace ID，整数 1～111；必须与采集配置/解码配置一致。 | int | 否 | 2 | [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111] |
| --pin-speed | Trace GPIO 输出速度档 1/2/3；不是 Trace 时钟频率。 | int | 否 | 2 | [1, 2, 3] |
| --branch-broadcast | 启用 ETM 分支广播；默认关闭，会影响数据量。 | flag × 0 | 否 | False | — |
| --return-stack / --no-return-stack | 启用返回栈；--no-return-stack 关闭。 | flag × 0 | 否 | True | — |
| --cycles / --no-cycles | 启用周期计数；--no-cycles 关闭。 | flag × 0 | 否 | True | — |
| --sources-json | 路径→正文的源码字典 | Path | 否 | 未指定 | — |
| --source-root | 只读取此目录内、ELF记录的实际源码路径；不猜basename | Path | 否 | 未指定 | — |
| --elf | 与采集/目标固件匹配的 ELF 文件路径；仅提供文件不代表烧录。 | Path | 是 | 未指定 | — |
| --out | 新的输出文件或目录（取决于命令）；拒绝覆盖已有结果。 | Path | 是 | 未指定 | — |
| --seconds | 请求采集长度，单位秒，例如 0.2 为 200 ms；实际长度需验收报告确认。 | float | 否 | 0.01 | — |
| --rate | LA 请求采样率，单位 samples/s，例如 500000000；实际能力取决于设备。 | int | 否 | 500000000 | — |
| --clock | LA 上的 TRACECLK 通道编号。 | int | 否 | 0 | — |
| --data | 四个 TRACEDATA 通道编号，按 D0、D1、D2、D3 顺序提供。 | int × 4 | 否 | [1, 2, 3, 4] | — |
| --fraction | 采样相位比例，默认 0.25；不是采集长度比例。 | float | 否 | 0.25 | — |
| --glitch-ns | 波形毛刺过滤阈值，单位 ns。 | float | 否 | 4 | — |
| --threshold | LA 数字输入判定电压阈值，单位 V。 | float | 否 | 1.65 | — |
| --no-decode | 仅保存采集结果，暂不启动解码。 | flag × 0 | 否 | False | — |
| --echo-port | CDC echo 负载使用的串口名；指定后会实际打开串口发送负载。 | str | 否 | 未指定 | — |
| --echo-interval | CDC echo 发送间隔，单位秒，0.001～1。 | float | 否 | 0.004 | — |
| --expect-core-hz | 预期 CPU 实际时钟，单位 Hz，用于回读校验，不能替代目标 PLL 配置。 | float | 否 | 未指定 | — |
| --expect-trace-hz | 预期 Trace 实际时钟，单位 Hz，用于回读校验。 | float | 否 | 未指定 | — |
| --entry | 函数、文件:行或*0x地址；LA启动后才从起点继续CPU | str | 否 | 未指定 | — |
| --address | 多个断点候选中精确选择 | <lambda> | 否 | 未指定 | — |
| --reset-to-entry | 采集前复位并运行至指定起点，会改变目标运行状态。 | flag × 0 | 否 | False | — |
| --entry-timeout | 等待起点断点的超时，单位秒，大于 0 且不超过 180。 | float | 否 | 15 | — |

## toolbox.py trace decode

原始输入或已有记录→新目录（默认原生优化）

```text
usage: toolbox.py trace decode [-h] (--input INPUT | --record RECORD)
                               [--elf ELF] [--config CONFIG] --out OUT
                               [--sources-json SOURCES_JSON]
                               [--source-root SOURCE_ROOT]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --input | 原始 KVDAT/CSV/ETM 输入文件；import-record 时为 .etmrecord 包。 | Path | 否 | 未指定 | — |
| --record | 已有记录目录路径，包含 session/解码或 TraceStore 等绑定文件。 | Path | 否 | 未指定 | — |
| --elf | 与采集/目标固件匹配的 ELF 文件路径；仅提供文件不代表烧录。 | Path | 否 | 未指定 | — |
| --config | 离线采集配置 JSON，须匹配输入的 Trace 协议、ID、通道与镜像。 | Path | 否 | 未指定 | — |
| --out | 新的输出文件或目录（取决于命令）；拒绝覆盖已有结果。 | Path | 是 | 未指定 | — |
| --sources-json | 路径→正文的源码字典 | Path | 否 | 未指定 | — |
| --source-root | 只读取此目录内、ELF记录的实际源码路径；不猜basename | Path | 否 | 未指定 | — |

必须且只能选一项：--input、--record。

## toolbox.py trace query

只读TraceStore查询

```text
usage: toolbox.py trace query [-h] --record RECORD
                              [--view {summary,overview,page,event,navigate,window,timeline,issues,source,functions,hotspots,flame,integrity}]
                              [--table {events,packets}] [--offset OFFSET]
                              [--limit LIMIT] [--seq SEQ] [--direction {-1,1}]
                              [--mode {instruction,call,return,exception,issue,source,pc}]
                              [--pc PC] [--search SEARCH] [--hide-skipped]
                              [--file FILE] [--bins BINS] [--from-seq START]
                              [--to-seq END] [--compact] [--fields FIELDS]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --record | 已有记录目录路径，包含 session/解码或 TraceStore 等绑定文件。 | Path | 是 | 未指定 | — |
| --view | 查询视图：summary/overview 概览；page/event 执行事件；navigate 导航；window/timeline 区间；issues/integrity 质量；source 源码；functions/hotspots/flame 调用统计。 | str | 否 | summary | ['summary', 'overview', 'page', 'event', 'navigate', 'window', 'timeline', 'issues', 'source', 'functions', 'hotspots', 'flame', 'integrity'] |
| --table | 分页对象：events 执行事件，packets 为 ETM 包。 | str | 否 | events | ['events', 'packets'] |
| --offset | 分页起始偏移，按条目计数。 | int | 否 | 0 | — |
| --limit | 分页条数，1～500。 | int | 否 | 150 | — |
| --seq | 查询/导航使用的执行序号，非负整数，不是时间。 | int | 否 | 0 | — |
| --direction | 导航方向：-1 向前，1 向后。 | int | 否 | 1 | [-1, 1] |
| --mode | 导航模式：instruction 指令、call 调用、return 返回、exception 异常、issue 质量问题、source 源码、pc 地址。 | str | 否 | instruction | ['instruction', 'call', 'return', 'exception', 'issue', 'source', 'pc'] |
| --pc | 期望/查找的 PC 地址，接受十进制或 0x 十六进制。 | <lambda> | 否 | 未指定 | — |
| --search | 搜索文本；elf inspect 筛选符号，query 按所选视图筛选。 | str | 否 |  | — |
| --hide-skipped | 隐藏被标记为跳过/未执行的事件，保留原始 TraceStore。 | flag × 0 | 否 | False | — |
| --file | 本地文件路径；web load-elf/flash 读取镜像，query source 用作源码路径筛选。 | str | 否 |  | — |
| --bins | Timeline 聚合桶数量，1～5000。 | int | 否 | 1200 | — |
| --from-seq | 序号区间起点，非负整数；与 to-seq 一起限定查询/导出，边界规则沿用对应原接口。 | int | 否 | 未指定 | — |
| --to-seq | 序号区间终点，不小于起点；不是毫秒值，不能代替采集时长。 | int | 否 | 未指定 | — |
| --compact | 摘要省略大函数表/分析树 | flag × 0 | 否 | False | — |
| --fields | 仅返回逗号分隔的字段路径，例如summary.instructions,quality.unknownExecution | str | 否 | 未指定 | — |

## toolbox.py trace verify

输入指纹、实际长度、store绑定和解码质量

```text
usage: toolbox.py trace verify [-h] --record RECORD [--require-decoded]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --record | 已有记录目录路径，包含 session/解码或 TraceStore 等绑定文件。 | Path | 是 | 未指定 | — |
| --require-decoded | 要求记录已解码；未解码时验证失败。 | flag × 0 | 否 | False | — |

## toolbox.py trace export

导出执行流、原始数据或完整记录包

```text
usage: toolbox.py trace export [-h] --record RECORD
                               --format {simplified,source,html,csv,json,events-jsonl,flame-svg,raw,etm,elf,record}
                               --out OUT [--from-seq START] [--to-seq END]
                               [--flame-mode {timeline,aggregate}]
                               [--flame-weight {records,cycles}]
                               [--flame-root FLAME_ROOT]
                               [--readability {original,learning}]
                               [--projection-manifest PROJECTION_MANIFEST]
                               [--repeat-threshold REPEAT_THRESHOLD]
                               [--repeat-keep-first REPEAT_KEEP_FIRST]
                               [--repeat-keep-last REPEAT_KEEP_LAST]
                               [--repeat-max-block REPEAT_MAX_BLOCK]
                               [--no-fold-repeats] [--no-fold-libraries]
                               [--no-group-unknown] [--no-statements]
                               [--macro-view {original,annotate,expanded}]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --record | 已有记录目录路径，包含 session/解码或 TraceStore 等绑定文件。 | Path | 是 | 未指定 | — |
| --format | 导出格式：simplified 简化C，source 完整C，html 阅读HTML，csv 执行流，json 完整解码，events-jsonl 原始事件行，flame-svg 完整函数名火焰图（执行顺序/调用权重），raw 原始波形，etm ETM二进制，elf 绑定镜像，record 可重解记录包。 | str | 是 | 未指定 | ['simplified', 'source', 'html', 'csv', 'json', 'events-jsonl', 'flame-svg', 'raw', 'etm', 'elf', 'record'] |
| --out | 新的输出文件或目录（取决于命令）；拒绝覆盖已有结果。 | Path | 是 | 未指定 | — |
| --from-seq | 序号区间起点，非负整数；与 to-seq 一起限定查询/导出，边界规则沿用对应原接口。 | int | 否 | 未指定 | — |
| --to-seq | 序号区间终点，不小于起点；不是毫秒值，不能代替采集时长。 | int | 否 | 未指定 | — |
| --flame-mode | flame-svg模式：timeline按原始执行顺序保留完整调用深度；aggregate按调用权重聚合 | str | 否 | timeline | ['timeline', 'aggregate'] |
| --flame-weight | 仅聚合图的宽度权重；cycles要求所选区间有周期计数 | str | 否 | records | ['records', 'cycles'] |
| --flame-root | 仅聚合图：原始调用树children索引组成的放大节点路径，例如0.2；空为整棵树 | str | 否 |  | — |
| --readability | learning生成C/HTML阅读投影与序号映射；original保留既有行为 | str | 否 | original | ['original', 'learning'] |
| --projection-manifest | 阅读清单新文件；默认<out>.manifest.json | Path | 否 | 未指定 | — |
| --repeat-threshold | 总次数严格超过此值才折叠；10表示至少11次 | int | 否 | 10 | — |
| --repeat-keep-first | 折叠重复执行时保留开头的完整次数。 | int | 否 | 2 | — |
| --repeat-keep-last | 折叠重复执行时保留末尾的完整次数。 | int | 否 | 1 | — |
| --repeat-max-block | 匹配缓冲的最大源码节点块长1～512 | int | 否 | 128 | — |
| --no-fold-repeats | 关闭重复执行块折叠。 | flag × 0 | 否 | False | — |
| --no-fold-libraries | 关闭无源码库调用折叠。 | flag × 0 | 否 | False | — |
| --no-group-unknown | 关闭相邻条件未知提示分组。 | flag × 0 | 否 | False | — |
| --no-statements | 关闭多行语句归并，保留原来源码行表达。 | flag × 0 | 否 | False | — |
| --macro-view | 宏显示：original 保留原写法，annotate 增加确定可证明的注释，expanded 显示可证明的展开；不能推断的宏保持未知。 | str | 否 | annotate | ['original', 'annotate', 'expanded'] |

## toolbox.py trace import-record

安全解压并核验.etmrecord，不自动重解

```text
usage: toolbox.py trace import-record [-h] --input INPUT --out OUT
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --input | 原始 KVDAT/CSV/ETM 输入文件；import-record 时为 .etmrecord 包。 | Path | 是 | 未指定 | — |
| --out | 新的输出文件或目录（取决于命令）；拒绝覆盖已有结果。 | Path | 是 | 未指定 | — |

## toolbox.py trace record

本地记录库、时长/配置查看、后配源码

```text
usage: toolbox.py trace record [-h] {list,info,config,sources} ...
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |

## toolbox.py trace record list

列出本地记录，可选择包含实验记录。

```text
usage: toolbox.py trace record list [-h] [--root ROOT] [--include-experiments]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --root | 扫描的本地记录库根目录。 | Path | 否 | <仓库>\etm\records | — |
| --include-experiments | 记录列表包含实验历史，默认隐藏。 | flag × 0 | 否 | False | — |

## toolbox.py trace record info

读取记录信息、绑定和采集实际时长。

```text
usage: toolbox.py trace record info [-h] --record RECORD
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --record | 已有记录目录路径，包含 session/解码或 TraceStore 等绑定文件。 | Path | 是 | 未指定 | — |

## toolbox.py trace record config

从记录导出可重用的采集配置。

```text
usage: toolbox.py trace record config [-h] --record RECORD --out OUT
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --record | 已有记录目录路径，包含 session/解码或 TraceStore 等绑定文件。 | Path | 是 | 未指定 | — |
| --out | 新的输出文件或目录（取决于命令）；拒绝覆盖已有结果。 | Path | 是 | 未指定 | — |

## toolbox.py trace record sources

为已有记录后配匹配源码，不改变解码执行事实。

```text
usage: toolbox.py trace record sources [-h] --record RECORD
                                       [--sources-json SOURCES_JSON]
                                       [--source-root SOURCE_ROOT]
```

| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |
|---|---|---|---|---|---|
| -h / --help | show this help message and exit | flag × 0 | 否 | 未指定 | — |
| --record | 已有记录目录路径，包含 session/解码或 TraceStore 等绑定文件。 | Path | 是 | 未指定 | — |
| --sources-json | 路径→正文的源码字典 | Path | 否 | 未指定 | — |
| --source-root | 只读取此目录内、ELF记录的实际源码路径；不猜basename | Path | 否 | 未指定 | — |

共覆盖 **63 个命令/分类节点**。
