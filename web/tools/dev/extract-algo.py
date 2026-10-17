#!/usr/bin/env python3
"""从本机 pyOCD 的 STM32H7B0xx target 定义里抽出 FLASH_ALGO，转成 app/flash/algos.js 的条目格式。

为什么这么做：algos.js 里那 7 个系列的算法本来就是从 pyOCD 抄的（见其文件头注释），
新加 H7B0 也照同一规矩来 —— 手抄 base64 必错，直接 import 出来转最稳。

用法：python tools/dev/extract-algo.py stm32h7b0
输出：stdout 打印可直接粘进 algos.js 的 JSON 片段（同时写到 tmp/algo-<name>.json）
"""
import json
import pathlib
import sys

PYOCD_TARGETS = {
    'stm32h7b0': ('pyocd.target.builtin.target_STM32H7B0xx', 'STM32H7B0xx'),
}


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else 'stm32h7b0'
    if name not in PYOCD_TARGETS:
        print(f'支持的算法：{", ".join(PYOCD_TARGETS)}')
        return 2
    mod_name, cls_name = PYOCD_TARGETS[name]
    try:
        import importlib
        mod = importlib.import_module(mod_name)
    except ImportError as e:
        print(f'导入 pyOCD 失败（{e}）—— 用装了 pyocd 的解释器跑本脚本：\n'
              r'  %USERPROFILE%\.venvs\pyocd\Scripts\python.exe tools\dev\extract-algo.py ' + name)
        return 1

    algo = mod.FLASH_ALGO
    instr = algo['instructions']
    # pyOCD 的 instructions 是**32 位字数组**（不是字节流），按小端打包成字节
    if isinstance(instr, (bytes, bytearray)):
        code = bytes(instr)
    else:
        import struct
        code = b''.join(struct.pack('<I', int(w) & 0xFFFFFFFF) for w in instr)
    out = {
        'code': __import__('base64').b64encode(code).decode(),
        'load_address': algo['load_address'],
        'pc_init': algo['pc_init'],
        'pc_eraseAll': algo.get('pc_erase_all', algo.get('pc_eraseAll', 0)),
        'pc_erase_sector': algo['pc_erase_sector'],
        'pc_program_page': algo['pc_program_page'],
        'static_base': algo['static_base'],
        'begin_stack': algo['begin_stack'],
        'page_buffers': list(algo.get('page_buffers', [])),
        'flash_start': algo['flash_start'],
        'flash_length': algo.get('flash_length', 0),
        'page_size': algo['page_size'],
    }
    # 目标定义的 flash 区域（用于核对 flash_length / 扇区大小）
    tgt = getattr(mod, cls_name)
    mm = getattr(tgt, 'memory_map', None)
    if mm is None:
        mm = type(tgt).memory_map if hasattr(type(tgt), 'memory_map') else None
    regions = []
    reg_list = getattr(mm, 'regions', mm) if mm is not None else []
    for r in (reg_list or []):
        if getattr(r, 'is_flash', False):
            regions.append({'start': hex(r.start), 'length': hex(r.length),
                            'sector_size': hex(getattr(r, 'sector_size', 0) or 0),
                            'page_size': hex(getattr(r, 'page_size', 0) or 0),
                            'name': getattr(r, 'name', '')})
    print('// ==== pyOCD 抽出的 flash 区域（核对用，不进 algos.js）====')
    print(json.dumps({'regions': regions, 'sector_sizes': list(algo.get('sector_sizes', []))}, indent=2))
    print('// ==== 可直接粘进 app/flash/algos.js 的条目 ====')
    body = json.dumps(out, indent=4)
    print(f'  "{name}": {body},'.replace('\n', '\n  '))
    pathlib.Path('tmp').mkdir(exist_ok=True)
    pathlib.Path(f'tmp/algo-{name}.json').write_text(json.dumps(out, indent=2), encoding='utf-8')
    print(f'\n// 已写到 tmp/algo-{name}.json（code {len(code)} 字节）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
