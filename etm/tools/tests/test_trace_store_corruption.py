"""trace-store v1 损坏/鲁棒性测试：截断、校验和、未知版本/字节序、越界、超大计数、
未完成发布、跨代混读都必须被拒绝或明确报错，绝不返回拼凑结果。"""
import hashlib
import json
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import trace_store
from trace_store import TraceStore, TraceStoreError, write
from test_trace_store import build_fixture


HEADER_SIZE = trace_store.HEADER_SIZE
INDEX_ENTRY_SIZE = trace_store.INDEX_ENTRY_SIZE


class StoreImage:
    """一个 trace-store 代的可改写镜像（测试用）：块级/头部/manifest 都能改。"""

    def __init__(self, store_dir):
        self.store_dir = Path(store_dir)
        self.manifest = json.loads((self.store_dir / 'manifest.json').read_text(encoding='utf-8'))
        self.generation = self.manifest['generation']
        self.generation_dir = self.store_dir / 'generations' / self.generation
        self.raw = (self.generation_dir / 'store.bin').read_bytes()
        self.header = bytearray(self.raw[:HEADER_SIZE])
        (magic, version, file_type, order) = (
            bytes(self.header[0:8]), *struct.unpack_from('<HHI', self.header, 8))
        self.magic, self.version, self.file_type, self.order = magic, version, file_type, order
        self.tag, blocks, self.required, self.payload_bytes, self.file_bytes, \
            self.header_crc, self.optional = struct.unpack_from('<QIIQQII', self.header, 16)
        self.blocks = []
        for position in range(blocks):
            offset, length, count, block_type, flags, crc = struct.unpack_from(
                '<QIIHHI', self.raw, HEADER_SIZE + position * INDEX_ENTRY_SIZE)
            self.blocks.append(dict(offset=offset, length=length, count=count,
                                    block_type=block_type, flags=flags, crc=crc,
                                    payload=bytes(self.raw[offset:offset + length])))

    # -- 改写 ------------------------------------------------------------------------------
    def set_header(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)

    def build_bytes(self, *, header_crc=True):
        """按 blocks 重建 store.bin；块 CRC 默认按新负载重算，keep_crc=True 时保留旧值。"""
        index = bytearray(len(self.blocks) * INDEX_ENTRY_SIZE)
        payload = bytearray()
        start = HEADER_SIZE + len(index)
        for position, block in enumerate(self.blocks):
            body = block['payload']
            crc = block['crc'] if block.get('keep_crc') else zlib.crc32(body) & 0xFFFFFFFF
            struct.pack_into('<QIIHHI', index, position * INDEX_ENTRY_SIZE,
                             start + len(payload), len(body), block['count'],
                             block['block_type'], block['flags'], crc)
            payload += body
        header = bytearray(HEADER_SIZE)
        header[0:8] = self.magic
        struct.pack_into('<HHI', header, 8, self.version, self.file_type, self.order)
        struct.pack_into('<QIIQQII', header, 16, self.tag, len(self.blocks), self.required,
                         len(payload), HEADER_SIZE + len(index) + len(payload), 0, self.optional)
        if header_crc:
            struct.pack_into('<I', header, 48, zlib.crc32(bytes(header[:48])) & 0xFFFFFFFF)
        else:
            struct.pack_into('<I', header, 48, (self.header_crc ^ 0xFFFFFFFF) & 0xFFFFFFFF)
        return bytes(header) + bytes(index) + bytes(payload)

    def block_of(self, block_type):
        for index, block in enumerate(self.blocks):
            if block['block_type'] == block_type:
                return index
        raise AssertionError('缺少块类型 %d' % block_type)

    def install(self, store_dir=None, *, raw=None, manifest_patch=None):
        """把改写后的镜像写成 store 目录（manifest 同步更新，便于只测目标问题）。"""
        store_dir = Path(store_dir or self.store_dir)
        manifest = json.loads(json.dumps(self.manifest))
        data = raw if raw is not None else self.build_bytes()
        generation_dir = store_dir / 'generations' / self.generation
        generation_dir.mkdir(parents=True, exist_ok=True)
        (generation_dir / 'store.bin').write_bytes(data)
        for entry in manifest['files']:
            entry['bytes'] = len(data)
            entry['sha256'] = hashlib.sha256(data).hexdigest()
        if manifest_patch is not None:
            manifest_patch(manifest)
        (store_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False),
                                                 encoding='utf-8')
        return store_dir


def rewrite_records(payload, transform):
    """对序列块做记录级改写：transform(type, record_payload) -> 新 payload 或 None（删除）。"""
    output = bytearray()
    position = 0
    while position < len(payload):
        record_type = payload[position]
        position += 1
        length, position = trace_store._dvar(payload, position)
        body = payload[position:position + length]
        position += length
        changed = transform(record_type, bytes(body))
        if changed is None:
            continue
        output += bytes([record_type])
        length_buffer = bytearray()
        trace_store._wv(length_buffer, len(changed))
        output += length_buffer
        output += changed
    return bytes(output)


def encode_run(start_seq, count, tail_code, offset, cycles, span, internal_unknown, identifiers):
    body = bytearray()
    trace_store._wv(body, start_seq)
    trace_store._wv(body, count)
    body.append(tail_code)
    trace_store._wv(body, offset)
    trace_store._wv(body, cycles)
    trace_store._wv(body, span)
    trace_store._wv(body, internal_unknown)
    trace_store._wv(body, identifiers[0])
    previous = identifiers[0]
    for identifier in identifiers[1:]:
        trace_store._wv(body, ((identifier - previous) << 1) ^ ((identifier - previous) >> 63)
                        if identifier - previous < 0 else (identifier - previous) << 1)
        previous = identifier
    return bytes(body)


class CorruptionCase(unittest.TestCase):
    cycles = True
    jsonl = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.record = self.root / 'record'
        build_fixture(self.record, cycles=self.cycles, jsonl=self.jsonl)
        self.store_dir = self.root / 'store'
        self.result = write(self.record, self.store_dir, verify_image=False)
        self.image = StoreImage(self.store_dir)

    def tearDown(self):
        import gc
        gc.collect()
        self.tmp.cleanup()

    def broken(self, name='broken', **kwargs):
        return self.image.install(self.root / name, **kwargs)

    def assert_rejected(self, store_dir, expected=None, verify='sizes'):
        with self.assertRaises(TraceStoreError) as caught:
            store = TraceStore(store_dir, verify=verify)
            try:
                list(store.events())
            finally:
                store.close()
        message = str(caught.exception)
        if expected:
            self.assertIn(expected, message)
        return message

    # -- 头部 ------------------------------------------------------------------------------
    def test_wrong_magic_rejected(self):
        self.image.set_header(magic=b'BADMAGIC')
        self.assertIn('magic', self.assert_rejected(self.broken()))

    def test_unknown_schema_version_rejected(self):
        self.image.set_header(version=2)
        self.assertIn('schemaVersion', self.assert_rejected(self.broken()))

    def test_big_endian_file_rejected(self):
        self.image.set_header(order=0x04030201)
        self.assertIn('大端', self.assert_rejected(self.broken()))

    def test_unknown_required_feature_rejected(self):
        self.image.set_header(required=1)
        self.assertIn('强制特性', self.assert_rejected(self.broken()))

    def test_header_checksum_error_rejected(self):
        store = self.broken(raw=self.image.build_bytes(header_crc=False))
        self.assertIn('校验和', self.assert_rejected(store))

    def test_generation_tag_mismatch_rejected(self):
        self.image.set_header(tag=self.image.tag ^ 0x5A5A)
        self.assertIn('generation', self.assert_rejected(self.broken()))

    # -- 索引/块 ---------------------------------------------------------------------------
    def test_truncated_file_rejected(self):
        path = self.store_dir / 'generations' / self.image.generation / 'store.bin'
        data = path.read_bytes()
        self.image.install(self.root / 'truncated', raw=data[:-16])
        self.assertIn('长度', self.assert_rejected(self.root / 'truncated', verify='none'))

    def test_block_length_overflow_rejected(self):
        raw = bytearray(self.image.build_bytes())
        struct.pack_into('<I', raw, HEADER_SIZE + 8, 0x40000000)     # 块 0 长度字段
        self.image.install(self.root / 'bad-length', raw=bytes(raw))
        self.assertIn('上限', self.assert_rejected(self.root / 'bad-length', verify='none'))

    def test_block_flag_unknown_rejected(self):
        self.image.blocks[0]['flags'] = 0x4
        self.image.install(self.root / 'bad-flag')
        self.assertIn('未知标志位', self.assert_rejected(self.root / 'bad-flag', verify='none'))

    def test_block_checksum_error_rejected(self):
        index = self.image.block_of(trace_store.BLK_SEQUENCE)
        payload = bytearray(self.image.blocks[index]['payload'])
        payload[len(payload) // 2] ^= 0xFF
        self.image.blocks[index]['payload'] = bytes(payload)
        self.image.blocks[index]['keep_crc'] = True     # 保留旧 CRC：只有校验和能发现等长改写
        store = self.broken(name='bad-crc')
        # install 会按新数据更新 manifest sha256，因此 open 只有在访问块时才能发现 CRC 错误
        self.assertIn('校验和', self.assert_rejected(store, verify='sizes'))
        # 关闭块校验后不再报“校验和”错误（可能结构性失败，也可能读出被篡改的数据）：
        # 说明等长改写的捕获责任确实在块 CRC 上，而不是别的校验。
        relaxed = TraceStore(store, verify='sizes', verify_crc=False)
        try:
            try:
                list(relaxed.events())
            except TraceStoreError as error:
                self.assertNotIn('校验和', str(error))
        finally:
            relaxed.close()

    def test_huge_declared_uncompressed_size_rejected(self):
        index = self.image.block_of(trace_store.BLK_META)
        self.assertTrue(self.image.blocks[index]['flags'] & 0x1)
        self.image.blocks[index]['count'] = 0x7FFFFFFF
        self.assertIn('巨量分配', self.assert_rejected(self.broken(name='huge'), verify='none'))

    def test_block_offset_overlap_rejected(self):
        raw = bytearray(self.image.build_bytes())
        first = HEADER_SIZE + 0 * INDEX_ENTRY_SIZE
        second = HEADER_SIZE + 1 * INDEX_ENTRY_SIZE
        offset = struct.unpack_from('<Q', raw, second)[0]
        struct.pack_into('<Q', raw, second, offset - 4)
        self.image.install(self.root / 'overlap', raw=bytes(raw))
        self.assertIn('偏移不连续', self.assert_rejected(self.root / 'overlap', verify='none'))

    def test_truncated_static_block_rejected(self):
        index = self.image.block_of(trace_store.BLK_STATIC)
        payload = self.image.blocks[index]['payload']
        self.image.blocks[index]['payload'] = payload[:-3]
        self.assertIn('截断', self.assert_rejected(self.broken(name='static-cut'),
                                                   verify='none'))

    # -- 记录级 ----------------------------------------------------------------------------
    def test_out_of_range_static_id_rejected(self):
        with TraceStore(self.store_dir) as store:
            next_seq = store.block_spans[-1]['lastSeq'] + 1
            static_count = len(store.static)
        index = self.image.block_of(trace_store.BLK_SEQUENCE)

        def append_run(record_type, body):
            return body
        payload = bytearray(self.image.blocks[index]['payload'])
        payload += b'\x01'
        crafted = encode_run(next_seq, 1, 0, 0, 0, 0, 0, [static_count + 10])
        length = bytearray()
        trace_store._wv(length, len(crafted))
        payload += length + crafted
        self.image.blocks[index]['payload'] = bytes(payload)
        self.assertIn('越界静态 ID', self.assert_rejected(self.broken(name='bad-id')))

    def test_zero_length_run_rejected(self):
        index = self.image.block_of(trace_store.BLK_SEQUENCE)

        def zero_count(record_type, body):
            if record_type != trace_store.REC_RUN:
                return body
            position = 0
            start, position = trace_store._dvar(body, position)
            count, position = trace_store._dvar(body, position)
            rebuilt = bytearray()
            trace_store._wv(rebuilt, start)
            trace_store._wv(rebuilt, 0)
            rebuilt += body[position:]
            return bytes(rebuilt)
        self.image.blocks[index]['payload'] = rewrite_records(
            self.image.blocks[index]['payload'], zero_count)
        self.assertIn('指令数无效', self.assert_rejected(self.broken(name='zero-run')))

    def test_seq_discontinuity_rejected(self):
        index = self.image.block_of(trace_store.BLK_SEQUENCE)

        def bump(record_type, body):
            if record_type != trace_store.REC_RUN:
                return body
            position = 0
            start, position = trace_store._dvar(body, position)
            rebuilt = bytearray()
            trace_store._wv(rebuilt, start + 5)
            rebuilt += body[position:]
            return bytes(rebuilt)
        self.image.blocks[index]['payload'] = rewrite_records(
            self.image.blocks[index]['payload'], bump)
        self.assertIn('seq 不连续', self.assert_rejected(self.broken(name='seq-jump')))

    def test_cycle_span_inconsistency_rejected(self):
        index = self.image.block_of(trace_store.BLK_SEQUENCE)
        state = {'done': False}

        def tamper(record_type, body):
            if record_type != trace_store.REC_RUN or state['done']:
                return body
            position = 0
            start, position = trace_store._dvar(body, position)
            count, position = trace_store._dvar(body, position)
            tail = body[position]
            position += 1
            offset, position = trace_store._dvar(body, position)
            cycles, position = trace_store._dvar(body, position)
            span, position = trace_store._dvar(body, position)
            rest = body[position:]
            rebuilt = bytearray()
            trace_store._wv(rebuilt, start)
            trace_store._wv(rebuilt, count)
            rebuilt.append(tail)
            trace_store._wv(rebuilt, offset)
            trace_store._wv(rebuilt, 0)          # cycles=0
            trace_store._wv(rebuilt, 3)          # 但 span=3 → 矛盾
            rebuilt += rest
            state['done'] = True
            return bytes(rebuilt)
        self.image.blocks[index]['payload'] = rewrite_records(
            self.image.blocks[index]['payload'], tamper)
        self.assertIn('周期跨度', self.assert_rejected(self.broken(name='span')))

    # -- manifest / 发布 -------------------------------------------------------------------
    def test_missing_manifest_rejected(self):
        (self.store_dir / 'manifest.json').unlink()
        self.assertIn('未完成', self.assert_rejected(self.store_dir))

    def test_manifest_state_not_complete_rejected(self):
        def patch(manifest):
            manifest['state'] = 'building'
        self.image.install(self.root / 'state', manifest_patch=patch)
        self.assertIn('未完成', self.assert_rejected(self.root / 'state'))

    def test_manifest_missing_generation_rejected(self):
        def patch(manifest):
            manifest['generation'] = 'g-missing'
        self.image.install(self.root / 'missing-gen', manifest_patch=patch)
        self.assertIn('不存在', self.assert_rejected(self.root / 'missing-gen'))

    def test_manifest_size_mismatch_rejected(self):
        def patch(manifest):
            manifest['files'][0]['bytes'] += 1
        self.image.install(self.root / 'size', manifest_patch=patch)
        self.assertIn('长度', self.assert_rejected(self.root / 'size'))

    def test_manifest_sha256_mismatch_rejected(self):
        def patch(manifest):
            manifest['files'][0]['sha256'] = '0' * 64
        self.image.install(self.root / 'sha', manifest_patch=patch)
        self.assertIn('sha256', self.assert_rejected(self.root / 'sha', verify='full'))

    def test_store_bin_from_another_generation_rejected(self):
        other_dir = self.root / 'other'
        write(self.record, other_dir, verify_image=False)
        other = StoreImage(other_dir)
        self.assertNotEqual(other.tag, self.image.tag)
        # 用另一代的 store.bin 覆盖本代，但 manifest 的 sha256 同步更新 → 只能靠 generation 标记发现
        self.image.install(self.root / 'cross', raw=other.raw)
        self.assertIn('generation', self.assert_rejected(self.root / 'cross'))

    def test_reader_never_returns_partial_result_after_block_failure(self):
        """块损坏时 page/event 直接报错，不返回短结果。"""
        index = self.image.block_of(trace_store.BLK_SEQUENCE)
        payload = bytearray(self.image.blocks[index]['payload'])
        payload[3] ^= 0x01
        self.image.blocks[index]['payload'] = bytes(payload)
        self.image.blocks[index]['keep_crc'] = True
        store_dir = self.broken(name='partial')
        store = TraceStore(store_dir, verify='sizes')
        try:
            with self.assertRaises(TraceStoreError):
                store.page(limit=10)
            with self.assertRaises(TraceStoreError):
                store.event(0)
        finally:
            store.close()


class StreamedCorruptionTests(CorruptionCase):
    cycles = True
    jsonl = True


class InlineCorruptionTests(CorruptionCase):
    cycles = False
    jsonl = False


if __name__ == '__main__':
    unittest.main()
