#!/usr/bin/env python3
"""校验 app/flash/algos.js 里的 flash 算法条目是否自洽（不连板子也能查掉一批低级错）。

查什么：
  1. code 能 base64 解码、长度是 4 的倍数；
  2. 每个 pc_* 入口都**落在 blob 内**，且除 Thumb 位后指向的指令**不是 0x0000/0xFFFF**（那种跑飞）；
  3. RAM 布局自洽：static_base < page_buffers[0] ≤ … < begin_stack，且都在芯片 RAM 范围内；
  4. flash 参数合理：flash_start/length/page_size，page_size 是 2 的幂且 ≤1MB。

用法：python tools\\dev\\verify-algo.py [stm32h7b0 stm32f103 ...]      # 不给参数就查全部
"""
import base64
import json
import pathlib
import re
import sys

# Windows 控制台默认 GBK，输出里只要有编不出的字符（✅❌ 之类）就 UnicodeEncodeError 崩
# —— 这个坑本项目已经踩过好几次（见 F103 那几个工具的注释），一律强制 UTF-8 + replace。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

ALGOS = pathlib.Path('app/flash/algos.js')


def load_algos():
    src = ALGOS.read_text(encoding='utf-8')
    # 抓 ALGOS 对象：从 "export const ALGOS = {" 到文件末尾的 "};"
    i = src.index('export const ALGOS')
    body = src[src.index('{', i):]
    # 每个条目形如  "name": { ... },
    out = {}
    for m in re.finditer(r'"([a-z0-9_]+)"\s*:\s*\{', body):
        name = m.group(1)
        depth = 1
        j = m.end()
        while j < len(body) and depth:
            if body[j] == '{':
                depth += 1
            elif body[j] == '}':
                depth -= 1
            j += 1
        obj = body[m.end():j - 1]
        d = {}
        for k, v in re.findall(r'"(\w+)"\s*:\s*("[^"]*"|\[[^\]]*\]|\d+)', obj, re.S):
            if v.startswith('"'):
                d[k] = v[1:-1]
            elif v.startswith('['):
                d[k] = json.loads(v.replace('\n', ''))
            else:
                d[k] = int(v)
        out[name] = d
    return out


def thumb_instr(blob, off):
    """取 Thumb 指令（2 字节，小端）"""
    if off + 2 > len(blob):
        return None
    return blob[off] | (blob[off + 1] << 8)


def check(name, a):
    bad, warn = [], []
    code = base64.b64decode(a['code'])
    if len(code) % 4:
        bad.append(f'blob 长度 {len(code)} 不是 4 的倍数')
    load = a['load_address']
    end = load + len(code)

    # 入口：本工程只用 init / erase_sector / program_page（eraseAll 不走），
    # 所以 eraseAll 只在明显不合理时**警告**，不当错误。
    for key in ('pc_init', 'pc_erase_sector', 'pc_program_page', 'pc_eraseAll'):
        pc = a.get(key) or 0
        hard = key != 'pc_eraseAll'
        if not pc:
            (bad if hard else warn).append(f'{key} 缺失/为 0')
            continue
        off = (pc & ~1) - load
        if not (0 <= off < len(code)):
            (bad if hard else warn).append(f'{key}=0x{pc:08X} 不在 blob 内（load 0x{load:08X} + {len(code)}B）'
                                           + ('' if hard else ' —— 本工程不用它擦整片，可忽略'))
            continue
        ins = thumb_instr(code, off)
        if ins in (0x0000, 0xFFFF):
            (bad if hard else warn).append(f'{key}=0x{pc:08X} 指向 0x{ins:04X}（不是有效指令，算法会跑飞）')

    # RAM 布局。注意 pyOCD 的约定：**blob = 代码 + 静态数据**，static_base 就指在 blob 里面
    # （本机几个算法实测：static_base = blob 末尾往前几个字），所以"重叠"是正常的，别误报。
    st, bufs, sp = a.get('static_base'), (a.get('page_buffers') or []), a.get('begin_stack')
    ps = a.get('page_size') or 0
    if None in (st, sp) or not bufs:
        bad.append('static_base / page_buffers / begin_stack 有缺失')
    else:
        if not (load <= st < end):
            # 不是错误：pyOCD 各算法的 static_base 语义不统一 —— H7/H7B0/L0/L4/F4/F7 指在 blob 内，
            # 而 F1/F0 指向 blob **之后**的一块独立静态区。执行器会把它装进 R9。静态区可以合法位于 blob 之后，所以只提示。
            warn.append(f'static_base=0x{st:08X} 在 blob（0x{load:08X}..0x{end - 1:08X}）之外'
                        f' —— 部分算法（F1/F0）就是这样，执行器通过 R9 使用此地址')
        for i, b in enumerate(bufs):
            if b < end:
                bad.append(f'page_buffers[{i}]=0x{b:08X} 落在 blob 内（会被算法自己的代码/数据盖掉）')
            if not (0x20000000 <= b < 0x40000000):
                bad.append(f'page_buffers[{i}]=0x{b:08X} 不像 RAM 地址')
        if len(set(bufs)) != len(bufs):
            bad.append('page_buffers 里有重复地址')
        # 缓冲容量：多缓冲才算得出（间距就是容量）；单缓冲时 pyOCD 保证 ≥ 一页，
        # 不能拿"栈 - 缓冲"去算 —— 有些算法把缓冲放在栈顶之上，差值是负数（L0/F0/F4 就是这样）。
        if len(bufs) > 1:
            room = bufs[1] - bufs[0]
            if room < 256:
                bad.append(f'两个页缓冲间距只有 {room} B（<256，算法没法装一页）')
            elif room < ps:
                warn.append(f'页缓冲间距 {room} B < 擦除粒度 {ps} B —— 编程会按 {room} B 分更小的块（能用但慢）')
        if sp < bufs[0]:
            warn.append(f'begin_stack=0x{sp:08X} 在页缓冲之下（算法把缓冲放在栈顶之上，正常；'
                        f'本工程的 chunkSize() 单缓冲时按页大小取，不受影响）')
        if not (0x20000000 <= sp < 0x40000000):
            bad.append(f'begin_stack=0x{sp:08X} 不像 RAM 地址')

    # flash 参数
    fl_start, fl_len = a.get('flash_start'), a.get('flash_length')
    if not fl_start:
        bad.append('flash_start 缺失')
    elif not (0x08000000 <= fl_start < 0x10000000):
        warn.append(f'flash_start=0x{fl_start:08X} 不像片上 flash（本工程按 0x08xxxxxx 处理）')
    if not fl_len:
        bad.append('flash_length 为 0（本工程拿它挡住越界镜像）')
    if not ps:
        bad.append('page_size 缺失/为 0')
    elif ps & (ps - 1):
        bad.append(f'page_size={ps} 不是 2 的幂（本工程当"擦除粒度"用，应取扇区大小）')
    elif ps > (1 << 20):
        bad.append(f'page_size={ps} 大得不像话')
    wg = a.get('write_granularity', 4)
    if wg & (wg - 1) or wg < 4 or wg > 256:
        bad.append(f'write_granularity={wg} 不合理（应 2 的幂、4~256）')

    print(f'\n=== {name} ===')
    print(f'  blob      : {len(code)} B（load 0x{load:08X} .. 0x{end - 1:08X}）')
    print(f'  入口      : init 0x{a["pc_init"]:08X} / erase_all 0x{a.get("pc_eraseAll", 0):08X} / '
          f'erase_sector 0x{a["pc_erase_sector"]:08X} / program 0x{a["pc_program_page"]:08X}')
    if None not in (st, sp) and bufs:
        print(f'  RAM       : static 0x{st:08X} / buf[0] 0x{bufs[0]:08X} / 栈 0x{sp:08X}')
    print(f'  flash     : 起 0x{fl_start:08X} 长 0x{fl_len:X}（{fl_len // 1024}KB）· 擦除粒度 {ps} B · 编程粒度 {wg} B')
    if warn:
        print('  ⚠️ 提示：')
        for w in warn:
            print('     - ' + w)
    if bad:
        print('  ❌ 问题：')
        for b in bad:
            print('     - ' + b)
    else:
        print('  ✅ 自洽')
    return not bad


def main() -> int:
    algos = load_algos()
    names = sys.argv[1:] or list(algos)
    ok = True
    for n in names:
        if n not in algos:
            print(f'{n}: 不在 algos.js 里（现有：{", ".join(algos)}）')
            ok = False
            continue
        ok = check(n, algos[n]) and ok
    print('\n结论：' + ('全部自洽 ✅' if ok else '有问题 ❌'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
