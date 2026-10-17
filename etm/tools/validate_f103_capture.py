"""验收真实 F103ZE ETMv3 记录：分支落点、交替调用和 1..8 次循环。"""
import argparse
import json
import re
from pathlib import Path
from decode_trace import Image


def validate(result, image):
    functions = {name: address for address, _, name in image.functions}
    leaves = {functions[n]: n for n in ('trace_even', 'trace_odd')}
    round_pc = functions['trace_workload_round']
    direct = skipped = linear = rounds = mixes = 0
    active = None
    mix_count = None
    for i, e in enumerate(result['events']):
        if e['kind'] != 'instruction':
            if e['type'] in (1, 2, 6, 7, 8):
                active = None
                mix_count = None
            continue
        if e['pc'] == round_pc:
            active = {'leaves': [], 'mixes': []}
        target = re.fullmatch(r'#(0x[0-9a-f]+)', e['operands'])
        address = int(target[1], 16) if target else None
        following = result['events'][i+1] if i+1 < len(result['events']) else None
        if following and following['kind'] == 'instruction':
            if address is not None and e['flow'] in ('branch', 'call'):
                expected = address if e['executed'] else e['pc'] + e['size']
                if following['pc'] != expected:
                    raise ValueError(f"分支落点不符：#{e['seq']} {e['mnemonic']} {e['operands']}")
                direct += 1
                skipped += not e['executed']
            elif e['flow'] == 'linear':
                if following['pc'] != e['pc'] + e['size']:
                    raise ValueError(f"线性指令路径不连续：#{e['seq']}")
                linear += 1
        if e['flow'] == 'call' and e['executed'] and active is not None:
            if address in leaves:
                active['leaves'].append(leaves[address])
            if address == functions['trace_mix']:
                mix_count = 0
        if e['function'] == 'trace_mix' and mix_count is not None:
            if e['flow'] == 'branch' and address is not None and address < e['pc'] and e['executed']:
                mix_count += 1
            if e['flow'] == 'return' and e['executed']:
                active['mixes'].append(mix_count)
                mix_count = None
                mixes += 1
        if active is not None and e['function'] == 'trace_workload_round' and e['flow'] == 'return' and e['executed']:
            seq = active['leaves']
            if len(seq) != 8 or any(a == b for a, b in zip(seq, seq[1:])):
                raise ValueError(f'完整 round 的八次交替调用不符：{active}')
            if active['mixes'] != list(range(1, 9)):
                raise ValueError(f'完整 round 的循环次数不符：{active}')
            rounds += 1
            active = None
    if rounds < 5 or direct < 1000 or skipped < 100:
        raise ValueError('验收窗口覆盖不足')
    quality = result['quality']
    for key in ('packetIssues', 'overflowPackets', 'unprocessedBytes', 'overlapBytes', 'unaccountedBytes'):
        if quality[key]:
            raise ValueError(f'字节/包核算未通过：{key}={quality[key]}')
    return dict(completeRounds=rounds, checkedMixCalls=mixes,
                checkedDirectBranches=direct, checkedNotTakenBranches=skipped,
                checkedLinearTransitions=linear, exceptions=result['summary']['exceptions'],
                summary=result['summary'], quality=quality)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('record', type=Path)
    parser.add_argument('--elf', type=Path, required=True)
    args = parser.parse_args()
    result = json.loads(args.record.read_text(encoding='utf8'))
    report = validate(result, Image(args.elf))
    args.record.with_name('f103-validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
