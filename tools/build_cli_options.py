"""Generate a complete CLI argument reference from both real parsers; fail on missing explanations."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'etm/tools'))
import toolbox
import trace_cli

# Explanations absent from the original argparse definitions. Keys are option
# strings, not implementation variable names, so semantic changes are reviewed.
EXPLANATIONS = {
    '--port': '本机工作台 HTTP 监听端口，1024～65535；只绑定127.0.0.1。',
    '--la-host': 'KingstVIS / LA 控制服务主机地址。',
    '--la-port': 'KingstVIS / LA 控制服务 TCP 端口。',
    '--profile': '目标芯片配置族；auto 为自动识别，不能把另一型号配置用于当前目标。',
    '--swd-hz': 'SWD 请求频率，单位 Hz；Trace 控制当前固定 1000000，web 调试会话可选其他档位。',
    '--trace-id': 'ETM Trace ID，整数 1～111；必须与采集配置/解码配置一致。',
    '--pin-speed': 'Trace GPIO 输出速度档 1/2/3；不是 Trace 时钟频率。',
    '--branch-broadcast': '启用 ETM 分支广播；默认关闭，会影响数据量。',
    '--return-stack': '启用返回栈；--no-return-stack 关闭。',
    '--cycles': '启用周期计数；--no-cycles 关闭。',
    '--from-seq': '序号区间起点，非负整数；与 to-seq 一起限定查询/导出，边界规则沿用对应原接口。',
    '--to-seq': '序号区间终点，不小于起点；不是毫秒值，不能代替采集时长。',
    '--hardware': '除软件依赖检查外，还枚举硬件/查询 LA 服务；默认不访问硬件。',
    '--elf': '与采集/目标固件匹配的 ELF 文件路径；仅提供文件不代表烧录。',
    '--spec': '断点表达式：函数名、文件:行号或 *0x地址。',
    '--search': '搜索文本；elf inspect 筛选符号，query 按所选视图筛选。',
    '--file': '本地文件路径；web load-elf/flash 读取镜像，query source 用作源码路径筛选。',
    '--script': '调试命令脚本文件，每行传给网页原命令解释器。',
    '--base': 'BIN 等镜像基地址；接受十进制或 0x 十六进制，以芯片和实际镜像布局为准。',
    '--timeout': '等待结果的时间，单位秒；Trace 目标等待要求大于 0 且不超过 180。超时不代表任务已停止。',
    '--pc': '期望/查找的 PC 地址，接受十进制或 0x 十六进制。',
    '--runtime-dir': '原生 WebUSB worker 运行时目录；start 要求新目录，stop 使用原目录。',
    '--state': 'Trace 配置恢复状态 JSON 路径；configure 保存，restore 读取并核验目标身份/状态。',
    '--resume': '配置成功后继续运行目标。',
    '--halt': '停止 Trace 后保持目标暂停，不自动继续。',
    '--entry': '采集/运行起点：函数、文件:行号或 *0x地址，必须能在匹配 ELF 中解析。',
    '--address': '精确选择断点地址，接受十进制或 0x 十六进制。',
    '--out': '新的输出文件或目录（取决于命令）；拒绝覆盖已有结果。',
    '--seconds': '请求采集长度，单位秒，例如 0.2 为 200 ms；实际长度需验收报告确认。',
    '--rate': 'LA 请求采样率，单位 samples/s，例如 500000000；实际能力取决于设备。',
    '--clock': 'LA 上的 TRACECLK 通道编号。',
    '--data': '四个 TRACEDATA 通道编号，按 D0、D1、D2、D3 顺序提供。',
    '--fraction': '采样相位比例，默认 0.25；不是采集长度比例。',
    '--glitch-ns': '波形毛刺过滤阈值，单位 ns。',
    '--threshold': 'LA 数字输入判定电压阈值，单位 V。',
    '--no-decode': '仅保存采集结果，暂不启动解码。',
    '--echo-port': 'CDC echo 负载使用的串口名；指定后会实际打开串口发送负载。',
    '--echo-interval': 'CDC echo 发送间隔，单位秒，0.001～1。',
    '--expect-core-hz': '预期 CPU 实际时钟，单位 Hz，用于回读校验，不能替代目标 PLL 配置。',
    '--expect-trace-hz': '预期 Trace 实际时钟，单位 Hz，用于回读校验。',
    '--reset-to-entry': '采集前复位并运行至指定起点，会改变目标运行状态。',
    '--entry-timeout': '等待起点断点的超时，单位秒，大于 0 且不超过 180。',
    '--input': '原始 KVDAT/CSV/ETM 输入文件；import-record 时为 .etmrecord 包。',
    '--record': '已有记录目录路径，包含 session/解码或 TraceStore 等绑定文件。',
    '--config': '离线采集配置 JSON，须匹配输入的 Trace 协议、ID、通道与镜像。',
    '--view': '查询视图：summary/overview 概览；page/event 执行事件；navigate 导航；window/timeline 区间；issues/integrity 质量；source 源码；functions/hotspots/flame 调用统计。',
    '--table': '分页对象：events 执行事件，packets 为 ETM 包。',
    '--offset': '分页起始偏移，按条目计数。',
    '--limit': '分页条数，1～500。',
    '--seq': '查询/导航使用的执行序号，非负整数，不是时间。',
    '--direction': '导航方向：-1 向前，1 向后。',
    '--mode': '导航模式：instruction 指令、call 调用、return 返回、exception 异常、issue 质量问题、source 源码、pc 地址。',
    '--hide-skipped': '隐藏被标记为跳过/未执行的事件，保留原始 TraceStore。',
    '--bins': 'Timeline 聚合桶数量，1～5000。',
    '--require-decoded': '要求记录已解码；未解码时验证失败。',
    '--format': '导出格式：simplified 简化C，source 完整C，html 阅读HTML，csv 执行流，json 完整解码，events-jsonl 原始事件行，raw 原始波形，etm ETM二进制，elf 绑定镜像，record 可重解记录包。',
    '--repeat-keep-first': '折叠重复执行时保留开头的完整次数。',
    '--repeat-keep-last': '折叠重复执行时保留末尾的完整次数。',
    '--no-fold-repeats': '关闭重复执行块折叠。',
    '--no-fold-libraries': '关闭无源码库调用折叠。',
    '--no-group-unknown': '关闭相邻条件未知提示分组。',
    '--no-statements': '关闭多行语句归并，保留原来源码行表达。',
    '--macro-view': '宏显示：original 保留原写法，annotate 增加确定可证明的注释，expanded 显示可证明的展开；不能推断的宏保持未知。',
    '--root': '扫描的本地记录库根目录。',
    '--include-experiments': '记录列表包含实验历史，默认隐藏。',
    'id': 'job/ack 为队列返回的32位十六进制 job ID；download/discard 为 downloads 返回的 Blob ID，二者不能混用。',
    'file': 'UTF-8 JSON 请求文件；request 为单个对象，batch 为1～1000个对象数组。',
    'page': '网页功能分类，取下面 choices 中的值；先 describe 再操作。',
    'destination': '下载到本机的新文件完整路径，拒绝覆盖；失败保留 .partial。',
    'selector': 'describe 返回的唯一控件 selector；也支持其返回的 @file: 虚拟文件入口。',
    'event': '待分发的浏览器事件类型，参数通过 --data JSON 对象提供。',
    'files': '本机文件路径，可多文件；--directory 时仅接受一个精简源码目录。',
    'method': 'describe.methods 中真实公开的方法名，不支持任意对象路径、构造函数和私有方法。',
    '--args-file': 'UTF-8 JSON 位置参数数组文件；参数内支持 $file 引用，规则见总指南。',
}

COMMANDS = {
    'describe':'枚举当前控件、可选值、目标子对象、方法签名和停止白名单。',
    'state':'读取页面摘要、文字与近期错误，不代表任务已成功。',
    'show':'切换当前显示页面，仅投递切换事件。',
    'downloads':'列出该页面暂存的 Blob 导出。',
    'read':'读取唯一控件的值和文字。',
    'click':'点击控件，仅表示投递，需另查业务完成状态。',
    'interrupt':'长任务期间请求停止，只允许停止白名单中的控件。',
    'set':'设置参数并发送 input/change，checkbox 必须为 JSON boolean。',
    'upload':'经原网页 file input 导入文件；事件投递不代表解析完成。',
    'wait':'轮询控件文字直到匹配或超时，不代替采集质量检查。',
    'inspect':'离线查看 ELF 符号/文件，或回读目标当前配置，取决于上一级分类。',
    'resolve':'从匹配 ELF 解析精确断点候选。',
    'status':'查询原生运行时/调试会话状态。',
    'connect':'连接选定探针和目标调试会话，会占用设备。',
    'disconnect':'正常释放目标和探针会话。',
    'load-elf':'载入目标匹配的 ELF，供符号与断点使用，不执行烧录。',
    'command':'执行网页调试命令，命令语法以 web help 返回为准。',
    'help':'返回网页真实调试命令帮助。',
    'flash':'执行网页原烧录流程，校验后复位运行，会修改 Flash 和目标状态。',
    'wait-halt':'等待目标暂停，可要求停在指定 PC；不会擅自复位。',
    'cancel':'请求取消当前受支持的调试等待；不保证可打断烧录。',
    'start':'启动原生 WebUSB worker 运行时，不是浏览器页面运行时。',
    'stop':'正常停止运行时或 Trace；行为取决于上级 web/target。',
    'configure':'校验 ELF 并配置 Trace，保存可核验的恢复状态。',
    'restore':'核验目标身份和当前配置后恢复保存的 Trace 状态。',
    'halt':'暂停目标 CPU。',
    'run':'继续运行目标 CPU。',
    'recover':'尝试恢复目标调试口并保持暂停。',
    'run-to':'不复位地运行至指定起点。',
    'reset':'复位并运行至指定起点。',
    'list':'列出本地记录，可选择包含实验记录。',
    'info':'读取记录信息、绑定和采集实际时长。',
    'config':'从记录导出可重用的采集配置。',
    'sources':'为已有记录后配匹配源码，不改变解码执行事实。',
}


def clean(value):
    return str(value).replace(str(ROOT), '<仓库>').replace('|', '\\|').replace('\n', ' ')


def render():
    rows = ['# CLI 命令与参数完整参考', '',
            '由 `python tools/build_cli_options.py` 从真实 argparse 解析器生成；`--check` 检查是否过期。', '',
            '总工作流见 [AI 总指南](toolbox-cli-ai.md)，Trace 详解见 [Trace 指南](trace-cli-ai.md)。', '',
            '此处逐条覆盖命令行语法。`page invoke` 是高级网页方法适配器：内部方法的签名/源码位置不等于完整参数类型文档，复杂参数须检查源码，不可猜测。', '',
            '`toolbox.py` 全局选项放在子命令前；`trace` 必须紧接 toolbox.py，其全局选项放在 trace 后、Trace 子命令前。', '',
            '统一入口除 catalog、trace 外的命令必须提供全局 --runtime；该条件由运行时检查，表中的必填列显示 argparse 层规则。', '',
            '默认值为解析器默认值；环境变量未设置时的 Trace 默认值在此固定显示。必填互斥组也列出。', '']
    count = 0

    def walk(parser, path, explanation=None):
        nonlocal count
        parser.prog = path
        count += 1
        description = explanation or parser.description or COMMANDS.get(path.split()[-1])
        if not description:
            raise ValueError('命令缺说明: ' + path)
        rows.extend(['## ' + path, '', clean(description), '', '```text', parser.format_usage().strip(), '```', '',
                     '| 参数 / 选项 | 说明 | 类型 / 数量 | 必填 | 默认值 | 可选值 |',
                     '|---|---|---|---|---|---|'])
        children = []
        for a in parser._actions:
            if isinstance(a, argparse._SubParsersAction):
                help_map = {v.dest: v.help for v in a._choices_actions}
                children.extend((name, sub, help_map.get(name)) for name, sub in a.choices.items())
                continue
            name = ' / '.join(a.option_strings) or a.dest
            text = a.help or next((EXPLANATIONS[k] for k in [*a.option_strings, a.dest] if k in EXPLANATIONS), None)
            if not text:
                raise ValueError('参数缺说明: ' + path + ' ' + name)
            typ = 'flag' if a.nargs == 0 else getattr(a.type, '__name__', 'str')
            if a.nargs is not None:
                typ += ' × ' + str(a.nargs)
            default = '未指定' if a.default is None or a.default == argparse.SUPPRESS else clean(a.default)
            required = bool(a.required or (not a.option_strings and a.nargs not in ('?', '*')))
            choices = clean(list(a.choices)) if a.choices is not None else '—'
            rows.append('| ' + ' | '.join([clean(name), clean(text), clean(typ), '是' if required else '否', default, choices]) + ' |')
        for group in parser._mutually_exclusive_groups:
            names = ['/'.join(a.option_strings) or a.dest for a in group._group_actions]
            rows.extend(['', ('必须且只能选一项：' if group.required else '以下选项不能同时使用：') + '、'.join(names) + '。'])
        rows.append('')
        for name, child, help_text in children:
            if path == 'toolbox.py' and name == 'trace':
                continue  # expand the actual Trace parser below
            prefix = path + (' <page>' if path == 'toolbox.py page' else '')
            walk(child, prefix + ' ' + name, help_text)

    walk(toolbox.parser(), 'toolbox.py')
    # Avoid embedding this machine's environment or absolute record-library path.
    env = {key: os.environ.pop(key, None) for key in ('TRACE_CLI_WEB', 'TRACE_CLI_TOKEN_FILE')}
    try:
        walk(trace_cli.build_parser(), 'toolbox.py trace')
    finally:
        for key, value in env.items():
            if value is not None:
                os.environ[key] = value
    rows.extend(['共覆盖 **' + str(count) + ' 个命令/分类节点**。', ''])
    return '\n'.join(rows), count


if __name__ == '__main__':
    text, count = render()
    target = ROOT / 'docs/toolbox-cli-options.md'
    if '--check' in sys.argv:
        if target.read_text(encoding='utf-8') != text:
            raise SystemExit('CLI 参数参考过期，请运行 python tools/build_cli_options.py')
    else:
        target.write_text(text, encoding='utf-8')
    print('CLI 参数参考：', count, '个命令/分类节点，所有参数均有说明')
