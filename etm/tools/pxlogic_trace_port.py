#!/usr/bin/env python3
"""PXView bit-packed channel exports -> synchronous four-bit DDR byte candidates."""
import argparse
import hashlib
import json
import statistics
from pathlib import Path


def edges(bits):
    previous = 0
    for byte_index, value in enumerate(bits):
        mask = (value ^ (value << 1)) & 0xFE
        if byte_index and (value & 1) != previous:
            mask |= 1
        while mask:
            bit = mask & -mask
            yield 8 * byte_index + bit.bit_length() - 1
            mask ^= bit
        previous = value >> 7


def extract(directory, rate, fraction=.25, clock=0, data=(1, 2, 3, 4)):
    if not 0 < fraction < 1:
        raise ValueError('fraction must lie between 0 and 1')
    channels = {n: (directory / f'channel_{n}.bin').read_bytes()
                for n in (clock, *data)}
    lengths = {len(bits) for bits in channels.values()}
    if len(lengths) != 1:
        raise ValueError('PXView channel binary lengths differ')
    samples = lengths.pop() * 8
    clock_edges = list(edges(channels[clock]))
    if len(clock_edges) < 10:
        raise ValueError('TRACECLK does not toggle enough to extract trace')
    periods = [b - a for a, b in zip(clock_edges, clock_edges[1:])]
    nibble = bytearray()
    for first, last in zip(clock_edges, clock_edges[1:]):
        position = first + max(0, min(last - first - 1, round((last - first) * fraction)))
        value = 0
        for bit, channel in enumerate(data):
            value |= ((channels[channel][position >> 3] >> (position & 7)) & 1) << bit
        nibble.append(value)
    streams = [bytes(nibble[i] | nibble[i + 1] << 4
                     for i in range(phase, len(nibble) - 1, 2)) for phase in (0, 1)]
    transition_counts = {str(ch): sum(1 for _ in edges(bits))
                         for ch, bits in channels.items()}
    report = {
        'directory': str(directory.resolve()),
        'input_sha256': {str(ch): hashlib.sha256(bits).hexdigest()
                         for ch, bits in channels.items()},
        'sample_rate_hz': rate, 'samples': samples,
        'window_ms': samples / rate * 1000,
        'clock_channel': clock, 'data_channels': list(data),
        'clock_edges': len(clock_edges),
        'clock_hz': (len(clock_edges) - 1) / ((clock_edges[-1] - clock_edges[0]) / rate) / 2,
        'median_half_period_samples': statistics.median(periods),
        'transitions': transition_counts,
        'sample_fraction': fraction,
        'nibbles': len(nibble),
        'candidates': [
            {'phase': phase, 'bytes': len(stream),
             'tpiu_fsync_count': stream.count(bytes.fromhex('ffffff7f')),
             'first_32_hex': stream[:32].hex(' ')}
            for phase, stream in enumerate(streams)
        ],
    }
    return report, streams


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('--rate', type=int, default=1000000000)
    p.add_argument('--fraction', type=float, default=.25)
    p.add_argument('--clock', type=int, default=0)
    p.add_argument('--data', nargs=4, type=int, default=[1, 2, 3, 4])
    p.add_argument('--out', type=Path)
    args = p.parse_args()
    report, streams = extract(args.directory, args.rate, args.fraction,
                              args.clock, args.data)
    prefix = args.out or args.directory / 'trace'
    prefix.parent.mkdir(parents=True, exist_ok=True)
    for index, stream in enumerate(streams):
        path = Path(f'{prefix}.phase{index}.bin')
        path.write_bytes(stream)
        report['candidates'][index]['file'] = str(path.resolve())
    path = Path(f'{prefix}.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
