"""Run the same HPM6800EVK debugger workload against both workbench backends."""

import argparse
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import toolbox

FIXTURE = ROOT / 'web/tools/target-firmware/hpm6800evk_dbgstress'
LOCK = ROOT.parent / 'web-etm-trace-coordination/hardware.lock'
SYMBOLS = ('main', 'engine_linear', 'engine_deep_chain', 'engine_dispatch', 'model_update',
           'is_even', 'engine_leaf', 'model_bitfield_touch', 'engine_branchy', 'deep_l5',
           'engine_deep_l4', 'engine_deep_l3', 'engine_deep_l2', 'deep_l1', 'g_stage', 'g_trap_count')


def require(condition, evidence):
    if not condition:
        raise RuntimeError(evidence)


def symbol_info():
    code = '''
import { readFileSync } from 'node:fs';
import { SymTab } from './web/app/dbg/symbols.js';
const bytes = Uint8Array.from(readFileSync(process.argv[2]));
const st = SymTab.fromBuffer(bytes.buffer);
console.log(JSON.stringify(Object.fromEntries(process.argv.slice(3).map(name => {
  const s = st.find(name);
  if (!s) throw new Error('Missing fixture symbol: ' + name);
  const at = st.at(s.addr);
  return [name, {addr:s.addr, size:s.size, line:at?.line, file:at?.file?.split(/[\\\\/]/).at(-1)}];
}))));
'''
    return json.loads(subprocess.check_output(['node', '--input-type=module', '-', str(FIXTURE / 'fw.elf'), *SYMBOLS],
                                             input=code, encoding='utf-8', cwd=ROOT))


class Workbench:
    def __init__(self, runtime):
        self.client = toolbox.Client(runtime, 35)
        self.unsettled = False

    def request(self, op, **params):
        try:
            result = self.client.request({'action': 'workbench', 'page': 'dbg', 'op': op, **params})
        except (TimeoutError, RuntimeError) as error:
            if isinstance(error, TimeoutError) or any(s in str(error) for s in ('可能仍在执行', '是否已排队未知')):
                self.unsettled = True
            raise
        if 'job' in result:
            self.client.http('ack/' + result['job'], {})
        require(result.get('ok'), result.get('error'))
        return result['value']

    def invoke(self, method, *args, target='session'):
        result = self.request('invoke', target=target, method=method, args=list(args))
        require(not result['errors'], result['errors'])
        return result['value']

    def line(self, line, allow_error=False):
        result = self.invoke('runLine', line, target='view')
        if not allow_error:
            require(not result.get('error'), result.get('error'))
        return '\n'.join(row['t'] for row in result.get('lines', [])) or result.get('error', '')

    def state(self):
        return self.request('state')['summary']

    def set(self, selector, value):
        self.request('set', selector=selector, value=value)

    def upload(self, selector, files):
        self.request('upload', selector=selector, files=files)


def stress(wb, backend, info, sdk):
    report = {'backend': backend, 'checks': {}, 'reconnections': 0}
    checks = report['checks']
    known = set()
    connected = False

    def check(name, action):
        nonlocal connected
        try:
            evidence = action()
            checks[name] = {'passed': True, 'evidence': evidence}
            print(f'{backend} PASS {name}', flush=True)
        except Exception as error:
            checks[name] = {'passed': False, 'error': str(error)}
            print(f'{backend} FAIL {name}: {error}', flush=True)
            if wb.unsettled:
                raise
            # A failed step can leave a trigger installed even when the page
            # no longer lists it. Reconnect before attributing another failure.
            wb.invoke('disconnect', target='view')
            connected = False
            require(not wb.state()['connected'], '失败后未断开')
            connected = wb.invoke('connect', target='view') is True
            require(connected, '失败后未重连')
            report['reconnections'] += 1
            wb.invoke('halt')
        else:
            wb.invoke('bpClear')

    def addr(name):
        return info[name]['addr']

    def add(name):
        wb.line('b ' + name)

    def hit(timeout=4):
        wb.invoke('cont')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = wb.invoke('refresh')
            if snap['halted']:
                return snap['pc']
        raise RuntimeError('断点未在时限内命中')

    def stop_at(name):
        add(name)
        pc = hit()
        require(pc == addr(name), f'{name}: PC={pc:#x}, expected {addr(name):#x}')
        wb.invoke('afterStop', target='view')
        return pc

    def source_breakpoint():
        hits = []
        for name in ('engine_linear', 'model_update'):
            symbol = info[name]
            spec = f"{symbol['file']}:{symbol['line']}"
            add(spec)
            pc = hit()
            wb.invoke('afterStop', target='view')
            src = wb.state()['src']
            cur = src['cur'] or {}
            require(pc == symbol['addr'] and cur.get('line') == symbol['line'] and src['shown'] > 0, (spec, pc, src))
            hits.append({'pc': pc, 'source': cur})
            wb.invoke('bpClear')
        return hits

    def four_breakpoints():
        names = ('engine_linear', 'engine_deep_chain', 'engine_dispatch', 'model_update')
        for name in names:
            add(name)
        expected = {addr(name) for name in names}
        seen = [hit() for _ in range(8)]
        require(set(seen) == expected and len(wb.state()['bps']) == 4, seen)
        return seen

    def repeated(name, rounds):
        audit = backend == 'openocd' and rounds == 40
        if audit:
            require(wb.invoke('verifyBreakpoints') == [], '源码步进结束后仍有 OpenOCD 断点')
        add(name)
        seen = []
        for _ in range(rounds):
            pc = hit()
            require(pc == addr(name), f'第 {len(seen) + 1} 轮落到 {pc:#x}，期望 {addr(name):#x}')
            seen.append(pc)
            if len(seen) % 10 == 0:
                require(len(wb.state()['bps']) == 1, '页面断点数量变化')
                if audit:
                    require(wb.invoke('verifyBreakpoints') == [addr(name)], '停走过程中 OpenOCD 断点不一致')
                print(f'{backend} {name}: {len(seen)}/{rounds}', flush=True)
        wb.invoke('bpClear')
        require(not wb.state()['bps'], '页面断点未清空')
        if audit:
            require(wb.invoke('verifyBreakpoints') == [], '停走结束后仍有 OpenOCD 断点')
        return {'rounds': len(seen), 'pc': seen[0]}

    def step_over(command):
        stop_at('engine_linear')
        result = [wb.state()['pc']]
        for i in range(19):
            wb.line(command)
            state = wb.state()
            require(state['halted'] and state['pc'] != result[-1] and (state['src']['cur'] or {}).get('line', 0) > 0,
                    f'第 {i + 1} 步失败，PC={state["pc"]:#x}, source={state["src"]}')
            result.append(state['pc'])
        return result

    def step_into():
        call_line = next(i for i, s in enumerate((FIXTURE / 'src/engine.c').read_text().splitlines(), 1)
                         if 'engine_leaf(a, 3u)' in s)
        add(f'engine.c:{call_line}')
        hit()
        wb.invoke('afterStop', target='view')
        require((wb.state()['src']['cur'] or {}).get('line') == call_line, '未命中调用行')
        pcs = []
        for _ in range(8):
            wb.line('si')
            state = wb.state()
            require(state['halted'], '单步后目标未暂停')
            pcs.append(state['pc'])
            if state['pc'] == addr('engine_leaf'):
                return pcs
        raise RuntimeError(f'没有进入 engine_leaf: {pcs}')

    def step_out():
        stop_at('deep_l5')
        pcs = []
        for name in ('engine_deep_l4', 'engine_deep_l3', 'engine_deep_l2', 'deep_l1', 'engine_deep_chain'):
            wb.line('fin')
            s = wb.state()
            require(s['halted'] and addr(name) <= s['pc'] < addr(name) + info[name]['size'],
                    f'跳出后 PC={s["pc"]:#x}，未回到 {name}')
            pcs.append(s['pc'])
        return pcs

    def reset(command):
        add('engine_linear')
        pcs = []
        for _ in range(2):
            wb.line(command)
            initial = wb.invoke('refresh')
            if command == 'reset halt':
                require(initial['halted'], initial)
            pc = hit() if not initial['halted'] or initial['pc'] != addr('engine_linear') else initial['pc']
            require(pc == addr('engine_linear'), hex(pc))
            pcs.append(pc)
        return pcs

    def print_value(expression, pattern, minimum_lines=1, allow_error=False):
        result = wb.line('p ' + expression, allow_error=allow_error)
        require(re.search(pattern, result) and len(result.splitlines()) >= minimum_lines, result[:500])
        return result.splitlines()[0]

    def variables():
        stop_at('model_update')
        return [print_value(expr, pattern, minimum, negative) for expr, pattern, minimum, negative in (
            ('g_model', 'struct', 20, False), ('g_model.flags.bits.level', '位域', 1, False),
            ('g_model.flags.sbits.bias', '有符号', 1, False), ('g_model.nodes[1].cell.scale', 'f64|double', 1, False),
            ('g_model.word.halves.hi', 'u16|short', 1, False), ('g_model.tag', '01234567', 1, False),
            ('g_model.nodes[9]', '越界', 1, True), ('g_model.nosuchmember', '没有成员', 1, True))]

    def watches():
        stop_at('model_update')
        expressions = ['g_model', 'g_model.flags.bits.level', 'g_model.nodes[2].cell.scale']
        try:
            for expr in expressions:
                wb.line('w ' + expr)
            rows = {w['expr']: w for w in wb.state()['watch']}
            require(all(expr in rows and not rows[expr]['error'] for expr in expressions), rows)
            return [rows[expr] for expr in expressions]
        finally:
            for expr in expressions:
                if expr not in known:
                    wb.line('wd ' + expr, allow_error=True)

    try:
        desc = wb.request('describe', target='view')
        require({'#d-backend', '#d-elf-file', '#d-src-dir'} <= {row['selector'] for row in desc['controls']}, '缺少调试控件')
        require(not wb.state()['connected'], '页面已有调试连接')
        wb.set('#d-backend', backend)
        if backend == 'openocd':
            files = [toolbox.pack_file(f, 'openocd/' + f.relative_to(sdk).as_posix()) for f in sorted(sdk.rglob('*')) if f.is_file()]
            wb.upload('#d-cfg-dir', files)
            wb.set('#d-cfg-entry', 'hpm6800_all_in_one.cfg')
            deadline = time.monotonic() + 5
            while '0x1000563d' not in wb.request('read', selector='#d-cfg-info')['text']:
                require(time.monotonic() < deadline, 'Wasm CFG 未导入')
        else:
            wb.upload('#d-cfg-file', [toolbox.pack_file(sdk / 'boards/hpm6800evk.cfg')])
            deadline = time.monotonic() + 5
            while '8000 kHz' not in wb.request('read', selector='#d-cfg-info')['text']:
                require(time.monotonic() < deadline, '内置 JTAG CFG 未导入')
        for selector in ('#d-rtt-on', '#d-watch-live'):
            control = wb.request('read', selector=selector)
            if control.get('value') is True:
                wb.set(selector, False)
        wb.upload('#d-elf-file', [toolbox.pack_file(FIXTURE / 'fw.elf')])
        wb.upload('#d-src-dir', [toolbox.pack_file(f, f'src/{f.name}') for f in sorted((FIXTURE / 'src').iterdir()) if f.is_file()])
        connected = wb.invoke('connect', target='view') is True
        require(connected, wb.request('read', selector='#d-out')['text'][-500:])
        s = wb.state()
        expected_backend = 'OpenOCD Wasm / WebUSB' if backend == 'openocd' else 'RISC-V/JTAG'
        require(s['clockKhz'] == 8000 and s['backend'] == expected_backend and s['src']['ready'] and s['elf']['lines'] > 0, s)
        report['session'] = s['backend']
        report['clockKhz'] = s['clockKhz']
        known.update(row['expr'] for row in s['watch'])
        wb.invoke('halt')
        for name, action in (
            ('源码行断点与高亮', source_breakpoint), ('四断点同时命中', four_breakpoints),
            ('同一断点连续五次', lambda: repeated('is_even', 5)),
            ('19 步指令单步', lambda: step_over('s')), ('19 步源码跳过', lambda: step_over('n')),
            ('源码进入被调函数', step_into), ('连续五次源码跳出', step_out),
            ('40 轮停走压力', lambda: repeated('engine_branchy', 40)),
            ('复位并停后断点重命中', lambda: reset('reset halt')), ('复位并运行后断点命中', lambda: reset('reset run')),
            ('RAM 结构体、位域和异常路径', variables),
            ('XIP 常量结构体', lambda: (stop_at('model_update'), print_value('g_model_const', 'struct', 20))),
            ('三类监视表达式', watches),
        ):
            check(name, action)
        trap = wb.invoke('memRead', addr('g_trap_count'), 4)['$bytes']
        require(not any(trap), f'靶子异常计数非零: {trap}')
        report['trapCount'] = 0
        report['pageErrors'] = wb.request('state')['errors']
        require(not report['pageErrors'], report['pageErrors'])
    except Exception as error:
        report['error'] = str(error)
        print(f'{backend} ERROR {error}', flush=True)
    finally:
        if connected and not wb.unsettled:
            try:
                wb.invoke('halt')
                wb.invoke('bpClear')
                pc = wb.invoke('readReg', 'pc')
                require(0x80003000 <= pc < 0x8000e000, f'收尾时 PC 不在测试固件 XIP 段: {pc:#x}')
                wb.invoke('run')
            except Exception as error:
                report['cleanupError'] = str(error)
            finally:
                if not wb.unsettled:
                    wb.invoke('disconnect', target='view')
                    require(not wb.state()['connected'], '页面没有断开')
                    report['disconnected'] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('sdk_openocd', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--lock-held', action='store_true')
    parser.add_argument('--backend', choices=['both', 'riscv', 'openocd'], default='both')
    args = parser.parse_args()
    require(not args.output.exists(), '输出文件已存在')
    info = symbol_info()
    wb = Workbench(args.runtime)
    if args.lock_held:
        require(LOCK.is_dir(), '缺少探针协调锁')
    else:
        LOCK.mkdir()
    results = {'elfSha256': hashlib.sha256((FIXTURE / 'fw.elf').read_bytes()).hexdigest(), 'backends': []}
    try:
        for backend in ('riscv', 'openocd') if args.backend == 'both' else (args.backend,):
            results['backends'].append(stress(wb, backend, info, args.sdk_openocd))
            if wb.unsettled:
                break
    finally:
        with args.output.open('x', encoding='utf-8') as out:
            json.dump(results, out, ensure_ascii=False, indent=2)
            out.write('\n')
        if not wb.unsettled and not wb.state()['connected']:
            LOCK.rmdir()
    return int(any(row.get('error') or row.get('cleanupError') or any(not c['passed'] for c in row['checks'].values())
                   for row in results['backends']))


if __name__ == '__main__':
    sys.exit(main())
