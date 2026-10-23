"""Machine-readable, local-first Trace workflow for AI and shell users."""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'trace-cli/1'
MAX_MEMBER = 1 << 30
MAX_TOTAL = 2 << 30
HARDWARE_PROFILES = ('stm32h743', 'stm32h7b0')


class CliError(Exception):
    def __init__(self, message, code=2, details=None):
        super().__init__(message)
        self.code, self.details = code, details


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError(message)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def record_input(metadata):
    name = metadata.get('input')
    if name not in ('capture.kvdat', 'capture.csv', 'capture.bin'):
        raise CliError('记录必须指定本目录的capture.kvdat/capture.csv/capture.bin')
    return name


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def fingerprint(path):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return dict(bytes=path.stat().st_size, sha256=digest.hexdigest())


def new_folder(path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()  # Never reuse a record directory, even after a failed attempt.
    return path


@contextlib.contextmanager
def destination(path):
    """Reserve a new destination, then atomically publish only our own file."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb'):
        pass
    temporary = path.with_name('.'+path.name+'.tmp-'+uuid.uuid4().hex)
    try:
        yield temporary
        os.replace(temporary, path)
    except BaseException:
        if path.exists() and path.stat().st_size == 0:
            path.unlink()
        raise
    finally:
        if temporary.exists():
            temporary.unlink()


def engine_environment():
    # Match the portable default; inherited developer flags must not silently change it.
    os.environ.update(ETM_STORE_WRITE='1', ETM_STORE_ENGINE='native', ETM_DECODE_ENGINE='fast',
                      ETM_NATIVE_FORMAT='binary', ETM_STORE_ENGINE_FALLBACK='0',
                      ETM_EXPORT_ENGINE='native', ETM_QUERY_ENGINE='native', ETM_PREPARE_CACHE='1')


def native_engine():
    engine_environment()
    for name in ('etm_frontend.exe', 'etm_decode.exe', 'etm_storebuild.exe', 'etm_export.exe'):
        if not (ROOT/'build-native'/name).is_file():
            raise CliError('缺少原生工具，请先构建：'+str(ROOT/'build-native'/name), 3)


def elf_info(path):
    from decode_trace import Image
    image = Image(Path(path).resolve())
    segments = []
    for s in image.elf.iter_segments():
        if s['p_type'] == 'PT_LOAD' and s['p_filesz']:
            lo, hi = int(s['p_paddr']), int(s['p_paddr']+s['p_filesz'])
            if 0x08000000 <= lo < hi <= 0x08200000:
                segments.append(dict(address=lo, data=base64.b64encode(s.data()).decode('ascii')))
            elif s['p_flags'] & 1:
                raise CliError('硬件CLI目前只验收内部Flash执行镜像')
    if not segments:
        raise CliError('ELF不含内部Flash镜像')
    return dict(sha256=hashlib.sha256(image.bytes).hexdigest(), segments=segments,
                sourceFiles=sorted({file for _, file, _ in image.lines.values() if file}))


def source_texts(args, elf):
    sources = load(args.sources_json) if getattr(args, 'sources_json', None) else {}
    if not isinstance(sources, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                          for k, v in sources.items()):
        raise CliError('sources JSON必须是 文件路径→源码正文 的字典')
    root = getattr(args, 'source_root', None)
    if root:
        root = Path(root).resolve()
        if not root.is_dir():
            raise CliError('源码根目录不存在')
        for name in elf['sourceFiles']:
            candidate = Path(name).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                sources[name] = candidate.read_text(encoding='utf8')
    return sources


def input_bindings(folder):
    meta = load(folder/'session.json')
    names = [record_input(meta), 'firmware.elf']
    # Keep decoder configuration separate from mutable session status/summary.
    config_sha = hashlib.sha256(json.dumps(meta['config'], sort_keys=True).encode()).hexdigest()
    value = dict(schema=1, configSha256=config_sha,
                 files={name: fingerprint(folder/name) for name in names})
    save(folder/'inputs.json', value)
    return value


def stored_view(folder):
    from trace_store_adapter import TraceStoreAdapter
    view = TraceStoreAdapter(Path(folder))
    view.ensure()
    return view


def quality_check(view, overview):
    q = overview['quality']
    failures = {key: q.get(key) for key in ('overflowPackets', 'unprocessedBytes',
        'unaccountedBytes', 'overlapBytes', 'decoderLastError') if q.get(key)}
    framing = overview.get('framing') or {}
    for key in ('errors', 'mid_frame_fsyncs', 'dropped_partial_frame_bytes', 'idle_resyncs'):
        if framing.get(key):
            failures['framing.'+key] = framing[key]
    if (overview.get('status') or {}).get('lastErrorCode'):
        failures['decoderStatus'] = overview['status']['lastErrorCode']
    if (overview.get('summary') or {}).get('gaps', 0) > 1:
        failures['gaps'] = overview['summary']['gaps']
    if (overview.get('physical') or {}).get('clock_gaps'):
        failures['clockGaps'] = overview['physical']['clock_gaps']
    terminal = []
    if q.get('packetIssues'):
        first = view.issues(0, 1)
        packet_issues = []
        for offset in range(0, first['total'], 500):
            packet_issues.extend(row for row in view.issues(offset, 500)['rows']
                                 if row['category'] == 'packet')
        terminal = [row for row in packet_issues if row['reason'] == '包问题：I_INCOMPLETE_EOT'
                    and row.get('offset', -1)+row.get('bytes', 0) == q.get('inputBytes')]
        if len(terminal) != q['packetIssues'] or len(packet_issues) != q['packetIssues']:
            failures['packetIssues'] = q['packetIssues']
    return dict(passed=not failures, failures=failures, terminalBoundaryPackets=terminal,
                note='尾部INCOMPLETE_EOT单列保留；初始化未同步/条件未知不视作已证实执行；字节核算不等于无损证明')


def extent(folder, metadata):
    raw = folder/record_input(metadata)
    if raw.suffix.lower() != '.kvdat':
        return dict(known=False, complete=None, reason='该原始格式没有KVDAT采样头；不从请求参数猜测实际时长')
    from kingst_record import layout
    info = layout(raw)
    requested = metadata.get('seconds')
    expected = round(float(requested)*info['rate']) if requested is not None else None
    result = dict(known=True, actualSamples=info['depth'], actualRate=info['rate'],
                  actualSeconds=info['depth']/info['rate'], requestedSeconds=requested,
                  requestedSamples=expected,
                  complete=abs(info['depth']-expected) <= 1 if expected is not None else None)
    if metadata.get('rate') is not None and info['rate'] != int(metadata['rate']):
        raise CliError('KVDAT采样率与配置不一致', 5, result)
    return result


def verify_record(folder, require_decoded=False):
    folder = Path(folder).resolve()
    meta = load(folder/'session.json')
    raw = folder/record_input(meta)
    if Path(meta['input']).name != meta['input'] or not raw.is_file():
        raise CliError('记录必须包含本地单窗原始输入', 5)
    result = dict(recordDir=str(folder), extent=extent(folder, meta),
                  raw=fingerprint(raw), elf=fingerprint(folder/'firmware.elf'))
    if (folder/'inputs.json').is_file():
        expected = load(folder/'inputs.json')
        config_sha = hashlib.sha256(json.dumps(meta['config'], sort_keys=True).encode()).hexdigest()
        if config_sha != expected['configSha256']:
            raise CliError('记录解码配置指纹不符', 5)
        for name, binding in expected['files'].items():
            if Path(name).name != name or fingerprint(folder/name) != binding:
                raise CliError('记录输入指纹不符：'+name, 5)
        result['inputManifestVerified'] = True
    else:
        result['inputManifestVerified'] = False
    result['passed'] = result['extent']['complete'] is not False
    if (folder/'store/manifest.json').is_file():
        view = stored_view(folder)
        try:
            overview = view.overview()
            bound_raw = (overview.get('capture') or {}).get('sha256')
            if bound_raw and bound_raw != result['raw']['sha256']:
                raise CliError('原始波形与解码结果的指纹不符', 5)
            result['storeInputVerification'] = view.input_verification()
            result['quality'] = quality_check(view, overview)
            result['summary'] = overview['summary']
            result['passed'] &= result['quality']['passed']
        finally:
            view.close()
    elif require_decoded:
        raise CliError('尚未生成原生TraceStore，请先decode', 5)
    else:
        result['decode'] = 'not-run'
    return result


def decode_folder(folder):
    from decode_stream import decode_capture_stream, decode_stream
    folder = Path(folder)
    meta = load(folder/'session.json')
    raw = folder/record_input(meta)
    sources = load(folder/'sources.json') if (folder/'sources.json').exists() else {}
    started = time.perf_counter()
    if raw.suffix.lower() in ('.kvdat', '.csv'):
        result = decode_capture_stream(raw, folder/'firmware.elf', folder, meta['config'], sources,
            fraction=meta.get('fraction', .25), clock=meta.get('clock', 0),
            data=meta.get('data', [1, 2, 3, 4]), glitch_ns=meta.get('glitchNs', 4))
    else:
        result = decode_stream(raw.read_bytes(), folder/'firmware.elf', folder, meta['config'], sources)
        result['capture'] = dict(name=raw.name, sha256=fingerprint(raw)['sha256'])
    result['targetSnapshot'] = meta.get('targetSnapshot')
    save(folder/'decoded.json', result)
    if result.get('performance'):
        save(folder/'decode-performance.json', result['performance'])
    meta['decodeStatus'] = 'done'
    save(folder/'session.json', meta)
    return dict(wallSeconds=time.perf_counter()-started, engine='native',
                verification=verify_record(folder, require_decoded=True))


def validated_config(value):
    from decode_trace import REG_ORDER, V3_REG_ORDER, decoder_config
    meta = value if isinstance(value, dict) else {}
    config = meta.get('config') if isinstance(meta.get('config'), dict) else meta
    required = V3_REG_ORDER if config.get('protocol') == 'ETMv3' else REG_ORDER
    if not all(key in config for key in required):
        raise CliError('缺少采集时的完整ETM寄存器配置；请使用session.json或target configure生成的状态，不能假定默认ETMv4')
    return decoder_config(config)


def decode_command(args):
    native_engine()
    if args.record:
        source = Path(args.record).resolve()
        metadata = load(source/'session.json')
        raw, elf = source/record_input(metadata), source/'firmware.elf'
    else:
        source = None
        raw, elf = Path(args.input).resolve(), Path(args.elf).resolve() if args.elf else None
        if elf is None or not args.config:
            raise CliError('原始输入解码必须提供 --elf 和 --config')
        value = load(args.config)
        expected_elf = value.get('elfSha256')
        if value.get('target'):
            value = value['target']
        if value.get('snapshot'):
            value = dict(config=value['snapshot']['config'], targetSnapshot=value['snapshot'],
                         elfSha256=expected_elf)
        metadata = dict(value) if isinstance(value, dict) else {}
        metadata['config'] = validated_config(value)
    if metadata.get('longWindow'):
        raise CliError('CLI当前只处理单窗记录；历史自动分窗记录请用原兼容工具')
    metadata['config'] = validated_config(metadata)
    if raw.suffix.lower() not in ('.kvdat', '.csv', '.bin') or not raw.is_file() or not elf.is_file():
        raise CliError('请提供存在的KVDAT/CSV/ETM BIN和配套ELF')
    info = elf_info(elf)
    if metadata.get('elfSha256') and metadata['elfSha256'] != info['sha256']:
        raise CliError('ELF与采集配置中的SHA256不一致', 5)
    folder = new_folder(args.out)
    rawname = 'capture'+raw.suffix.lower()
    shutil.copy2(raw, folder/rawname);shutil.copy2(elf, folder/'firmware.elf')
    metadata.update(input=rawname, elfSha256=info['sha256'], decodeStatus='pending')
    save(folder/'session.json', metadata)
    sources = source_texts(args, info)
    if not sources and source and (source/'sources.json').is_file():
        sources = load(source/'sources.json')
    save(folder/'sources.json', sources)
    input_bindings(folder)
    try:
        result = decode_folder(folder)
        if not result['verification']['passed']:
            raise CliError('记录未通过实际长度或解码质量检查', 5, result)
        return dict(recordDir=str(folder), **result)
    except BaseException as error:
        save(folder/'cli-error.json', dict(error=str(error), phase='decode', recordDir=str(folder)))
        raise


def target_command(args):
    from trace_cli_hardware import HardwareLease, TargetBridge
    if args.target_action == 'configure' and Path(args.state).exists():
        raise CliError('状态文件已存在，拒绝覆盖：'+args.state)
    with HardwareLease('target '+args.target_action), TargetBridge(args.probe, args.profile, args.hse*1e6, args.swd_hz) as bridge:
        if args.target_action == 'inspect':
            return dict(probe=bridge.probe_id, snapshot=bridge.info)
        if args.target_action == 'stop':
            value = bridge.call('stop', halt=args.halt)
            bridge.restore_on_close = False
            return value
        if args.target_action == 'restore':
            state = load(args.state)
            if state.get('schema') != 1 or state.get('restored'):
                raise CliError('状态版本不匹配，或状态已经恢复')
            if state['probe'] != bridge.probe_id:
                raise CliError('状态文件属于另一个探针，拒绝恢复')
            expected = state.pop('sha256', None)
            if hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest() != expected:
                raise CliError('状态文件指纹不符', 5)
            result = bridge.call('restore', state=state['target'])
            bridge.restore_on_close = False
            state.update(sha256=expected, restored=True, restoredAt=time.time())
            save(args.state, state)
            return result
        info = elf_info(args.elf)
        bridge.call('verify', elf=info)
        bridge.call('configure', options=target_options(args))
        if args.resume:
            bridge.call('run')
        state = dict(schema=1, probe=bridge.probe_id, target=bridge.call('state'), restored=False,
                     elfSha256=info['sha256'])
        state['sha256'] = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        with destination(args.state) as temporary:
            save(temporary, state)
        bridge.restore_on_close = False
        return dict(stateFile=str(Path(args.state).resolve()), snapshot=state['target']['snapshot'],
                    halted=not args.resume, probe=bridge.probe_id)


def target_options(args):
    return dict(traceid=args.trace_id, branchBroadcast=args.branch_broadcast,
                returnStack=args.return_stack, cycleCount=args.cycles, pinSpeed=args.pin_speed)


def capture_command(args):
    from trace_cli_hardware import HardwareLease, TargetBridge, EchoLoad
    from la.kingst_la import Vis, do_export, read_glitch_settings
    if not 0 < args.seconds <= 1 or not 0 < args.rate <= 500000000 or args.rate*args.seconds > 500000000:
        raise CliError('单次上限1s/5亿样本，采样率≤500MSa/s')
    if len(set([args.clock]+args.data)) != 5 or any(not 0 <= ch <= 15 for ch in [args.clock]+args.data):
        raise CliError('需要五个不同的CH0～15通道')
    if not 0 < args.fraction < 1 or not 0 <= args.glitch_ns <= 100 or not -4 <= args.threshold <= 4:
        raise CliError('相位/滤波/阈值参数无效')
    if not args.no_decode:
        native_engine()
    info = elf_info(args.elf)
    sources = source_texts(args, info)
    folder = new_folder(args.out)
    cleanup = dict(targetRestored=False, laIdle=None)
    echo, v, sampling, bridge = None, None, False, None
    metadata = dict(input='capture.kvdat', rate=args.rate, seconds=args.seconds,
        clock=args.clock, data=args.data, fraction=args.fraction, glitchNs=args.glitch_ns,
        threshold=args.threshold, sync=False, rawFormat='kvdat', created=time.time(),
        captureScope='cli-continuous-window', captureStatus='preparing', elfSha256=info['sha256'])
    shutil.copy2(args.elf, folder/'firmware.elf');save(folder/'sources.json', sources)
    save(folder/'session.json', metadata)
    started = time.perf_counter()
    load_result = None
    try:
        bridge = TargetBridge(args.probe, args.profile, args.hse*1e6, args.swd_hz)
        with HardwareLease('capture'), bridge:
            try:
                bridge.call('verify', elf=info)
                snapshot = bridge.call('configure', options=target_options(args))['snapshot']
                for key, expected in [('coreHz', args.expect_core_hz), ('physicalTraceHz', args.expect_trace_hz)]:
                    if expected is not None and not math.isclose(snapshot['clocks'].get(key) or 0, expected, rel_tol=1e-6):
                        raise CliError('目标时钟不符：'+key, details=snapshot['clocks'])
                metadata.update(config=snapshot['config'], targetSnapshot=snapshot,
                                targetVerified=True, probe=bridge.probe_id)
                save(folder/'target-state.json', dict(schema=1, probe=bridge.probe_id, target=bridge.call('state')))
                save(folder/'session.json', metadata)
                bridge.call('run')
                if args.echo_port:
                    echo = EchoLoad(args.echo_port, folder, args.echo_interval).start()
                    time.sleep(.35)
                    if echo.failure or echo.received < 5:
                        raise CliError('采集前echo负载未正常回收', 4)
                else:
                    time.sleep(.05)
                v = Vis(host=args.la_host, port=args.la_port)
                rates = [int(n) for n in v.cmd('get-supported-sample-rate').split()]
                if args.rate not in rates:
                    raise CliError('LA不支持所选采样率')
                v.cmd('set-sample-rate '+str(args.rate))
                v.cmd('set-sample-time '+str(args.seconds))
                v.cmd('set-threshold-voltage '+str(args.threshold))
                v.cmd('set-trigger --reset')
                sample_start = time.perf_counter()
                sampling = True
                v.cmd('start', timeout=args.seconds+60)
                sampling = False
                cleanup['laIdle'] = True
                metadata['captureAndTransferSeconds'] = time.perf_counter()-sample_start
                if echo:
                    load_result = echo.stop();echo = None
                save_start = time.perf_counter()
                do_export(v, str(folder/'capture.kvdat'), [args.clock]+args.data)
                metadata.update(captureStatus='saved', actualDepth=v.cmd('get-actual-sample-depth'),
                                actualTime=v.cmd('get-actual-sample-time'), saveSeconds=time.perf_counter()-save_start)
                metadata['laFilter'] = read_glitch_settings(folder/'capture.kvdat', args.rate)
                value = extent(folder, metadata)
                metadata['captureExtent'] = value
                save(folder/'session.json', metadata);input_bindings(folder)
                # KVDAT and device must agree; set-time ACK alone is not sufficient.
                if int(metadata['actualDepth']) != value['actualSamples']:
                    raise CliError('设备回读深度与保存的KVDAT不一致', 5, value)
                if not value['complete']:
                    raise CliError('未录满请求窗口，原始波形已保留', 5, value)
                if load_result and not load_result['passed']:
                    raise CliError('CDC负载丢包或异常；原始波形已保存但采集不能验收', 5, load_result)
            finally:
                if echo:
                    try:
                        load_result = echo.stop();echo = None
                    except Exception as error:
                        cleanup['echoError'] = str(error)
                if sampling and v:
                    try:
                        v.cmd('stop', timeout=10);cleanup['laIdle'] = True
                    except Exception as error:
                        cleanup['laError'] = str(error)
                if v:
                    v.close()
        cleanup['targetRestored'] = True
        acquisition_seconds = time.perf_counter()-started
        if not args.no_decode:
            decoded = decode_folder(folder)
            if not decoded['verification']['passed']:
                raise CliError('解码质量检查失败，原始记录保留', 5, decoded)
        else:
            decoded = dict(verification=verify_record(folder))
        return dict(recordDir=str(folder), acquisitionSeconds=acquisition_seconds,
                    cleanup=cleanup, **decoded)
    except BaseException as error:
        cleanup['targetRestored'] = bool(bridge and bridge.cleanup_result.get('restored'))
        save(folder/'cli-error.json', dict(error=str(error), phase='capture', cleanup=cleanup,
                                         recordDir=str(folder), notes=getattr(error, '__notes__', [])))
        if isinstance(error, CliError):
            error.details = dict(recordDir=str(folder), cleanup=cleanup, reason=error.details)
        raise
    finally:
        save(folder/'cleanup.json', cleanup)


def query_command(args):
    view = stored_view(Path(args.record).resolve())
    try:
        if args.view == 'summary':
            value = view.overview()
            return {k: value.get(k) for k in ('summary', 'quality', 'physical', 'framing', 'eventCount')}
        if args.view == 'overview':
            return view.overview()
        if args.view == 'page':
            return view.page(args.table, args.offset, args.limit, args.search, args.hide_skipped, args.start, args.end)
        if args.view == 'event':
            return view.event(args.seq)
        if args.view == 'navigate':
            return view.navigate(args.seq, args.direction, args.mode, args.pc, args.start, args.end)
        if args.view == 'window':
            return view.window(args.start, args.end, args.bins)
        if args.view == 'timeline':
            return view.function_timeline(args.start, args.end, args.search, args.hide_skipped, args.bins)
        if args.view == 'issues':
            return view.issues(args.offset, args.limit)
        return view.source(args.file)
    finally:
        view.close()


def export_record(folder, path):
    folder = Path(folder)
    metadata = load(folder/'session.json')
    names = {record_input(metadata), 'session.json', 'firmware.elf'}
    names.update(name for name in ('sources.json', 'decoded.json', 'inputs.json', 'physical.json',
        'framing.json', 'native-config.txt', 'native.bin', 'etm.bin', 'tpiu.bin') if (folder/name).is_file())
    names.update(p.name for p in folder.glob('image-*.bin'))
    if (folder/'store/manifest.json').is_file():
        manifest = load(folder/'store/manifest.json')
        generation = manifest['generation']
        if '/' in generation or '\\' in generation or '..' in generation:
            raise CliError('store代名称无效')
        names.add('store/manifest.json')
        for item in manifest['files']:
            if Path(item['name']).name != item['name']:
                raise CliError('store成员名无效')
            names.add('store/generations/'+generation+'/'+item['name'])
    if any((folder/name).stat().st_size > MAX_MEMBER for name in names) or sum((folder/name).stat().st_size for name in names) > MAX_TOTAL:
        raise CliError('记录包超出单成员1GiB或解压总量2GiB预算')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for name in sorted(names):
            archive.write(folder/name, name)


def export_command(args):
    folder = Path(args.record).resolve()
    if (args.start is None) != (args.end is None):
        raise CliError('区间必须同时提供 --from-seq 与 --to-seq')
    if args.start is not None and (args.start < 0 or args.end < args.start):
        raise CliError('导出序号范围无效')
    kind = 'simplified-html' if args.format == 'html' else args.format
    view = None
    try:
        if kind in ('record', 'raw', 'etm', 'json') and args.start is not None:
            raise CliError('该格式导出整份输入/记录/摘要，不接受执行序号裁剪')
        if kind in ('raw', 'etm'):
            name = record_input(load(folder/'session.json')) if kind == 'raw' else 'etm.bin'
            if Path(name).name != name:
                raise CliError('原始文件路径无效')
            writer = lambda p: shutil.copyfile(folder/name, p)
        elif kind == 'record':
            verification = verify_record(folder)
            if not verification['passed']:
                raise CliError('输入或质量未通过，拒绝当成合格记录包导出', 5, verification)
            writer = lambda p: export_record(folder, p)
        else:
            view = stored_view(folder)
            result = view.overview()
            if args.start is not None and args.end >= result['eventCount']:
                raise CliError('导出结束序号超过记录末尾')
            if kind == 'json':
                writer = lambda p: save(p, dict(result, sources=view.sources(),
                    cliExportNote='摘要/分析JSON；逐事件数据请导出events-jsonl'))
            elif kind == 'events-jsonl':
                def writer(p):
                    with p.open('w', encoding='utf8') as out:
                        for row in view.iter_events(0 if args.start is None else args.start, args.end):
                            out.write(json.dumps(row, ensure_ascii=False)+'\n')
            else:
                native_engine()
                result['sources'] = view.sources()
                if args.start is not None:
                    result['exportRange'] = dict(start=args.start, end=args.end)
                def writer(p):
                    if not view.export_execution(result, p, kind, args.start, args.end):
                        from source_export import write_execution_export
                        write_execution_export(result, view.iter_events(0 if args.start is None else args.start, args.end), p, kind)
        with destination(args.out) as temp:
            writer(temp)
        return dict(path=str(Path(args.out).resolve()), format=args.format,
                    fromSeq=args.start, toSeq=args.end, **fingerprint(args.out))
    finally:
        if view:
            view.close()


def import_command(args):
    archive_path = Path(args.input).resolve()
    if archive_path.stat().st_size > MAX_MEMBER:
        raise CliError('上传记录包超过1GiB预算')
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        names = [e.filename for e in entries]
        if len(set(names)) != len(names) or len(names) > 1000:
            raise CliError('记录包存在重复成员或成员过多')
        for item in entries:
            path = Path(item.filename)
            if item.is_dir() or path.is_absolute() or '..' in path.parts or '\\' in item.filename or ':' in item.filename:
                raise CliError('记录包成员路径无效：'+item.filename)
            if item.file_size > MAX_MEMBER or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise CliError('记录包成员超限或为符号链接')
            if item.filename in ('session.json','inputs.json','store/manifest.json') and item.file_size > 2 << 20:
                raise CliError('记录包配置/manifest超过2MiB元数据预算')
        if sum(e.file_size for e in entries) > MAX_TOTAL:
            raise CliError('记录包解压总量超过2GiB预算')
        if not {'session.json', 'firmware.elf'} <= set(names):
            raise CliError('记录包缺少session或ELF')
        metadata = json.loads(archive.read('session.json'))
        if metadata.get('input') not in ('capture.kvdat', 'capture.csv', 'capture.bin') or metadata['input'] not in names:
            raise CliError('记录包缺少合法原始输入')
        validated_config(metadata)
        folder = new_folder(args.out)
        for item in entries:
            output = folder/item.filename
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as src, output.open('xb') as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    result = verify_record(folder)
    if not result['passed']:
        raise CliError('导入记录未通过窗口长度或质量校验', 5, result)
    return result


def doctor_command(args):
    dependencies = {name: importlib.util.find_spec(module) is not None for name, module in
        [('pyelftools', 'elftools'), ('capstone', 'capstone'), ('numpy', 'numpy'),
         ('pyocd', 'pyocd'), ('pyserial', 'serial')]}
    tools = {name: str(ROOT/'build-native'/name) if (ROOT/'build-native'/name).is_file() else None
             for name in ('etm_frontend.exe', 'etm_decode.exe', 'etm_storebuild.exe', 'etm_export.exe')}
    result = dict(dependencies=dependencies, native=tools, node=shutil.which('node'),
                  hardware='not-probed', captureMaxSeconds=1, captureMaxSamples=500000000)
    if args.hardware:
        from trace_cli_hardware import HardwareLease, probes
        from la.kingst_la import Vis
        from serial.tools import list_ports
        with HardwareLease('doctor'):
            result['probes'] = [dict(uniqueId=p.unique_id, description=p.description) for p in probes()]
            result['serialPorts'] = [dict(port=p.device, description=p.description, vid=p.vid, pid=p.pid)
                                     for p in list_ports.comports()]
            v = Vis(args.la_host, args.la_port)
            try:
                result['la'] = {command: v.cmd(command) for command in
                    ('get-supported-sample-rate', 'get-sample-rate', 'get-actual-sample-depth', 'get-last-error')}
            finally:
                v.close()
            result['hardware'] = 'enumerated; target not configured'
    result['ready'] = all(dependencies.values()) and all(tools.values()) and bool(result['node'])
    if not result['ready']:
        raise CliError('依赖检查未通过', 3, result)
    return result


def build_parser():
    parser = Parser(description='AI Trace CLI；默认JSON输出，运行日志写stderr；不自动烧录或复位。')
    parser.add_argument('--json-out', type=Path, help='将本次结果另存为新JSON文件（不能覆盖）')
    sub = parser.add_subparsers(dest='command', required=True)
    def la(p):
        p.add_argument('--la-host', default='127.0.0.1');p.add_argument('--la-port', type=int, default=23367)
    def hw(p):
        p.add_argument('--probe', help='精确探针序列号；只连一个时可省略')
        p.add_argument('--profile', choices=HARDWARE_PROFILES, default='stm32h743')
        p.add_argument('--hse', type=float, default=25, help='HSE MHz，仅用于计算，不修改PLL')
        p.add_argument('--swd-hz', type=int, default=1000000)
    def options(p):
        p.add_argument('--trace-id', type=int, choices=range(1,112), default=2)
        p.add_argument('--pin-speed', type=int, choices=(1,2,3), default=2)
        p.add_argument('--branch-broadcast', action='store_true')
        p.add_argument('--return-stack', action=argparse.BooleanOptionalAction, default=True)
        p.add_argument('--cycles', action=argparse.BooleanOptionalAction, default=True)
    def sources(p):
        p.add_argument('--sources-json', type=Path, help='路径→正文的源码字典')
        p.add_argument('--source-root', type=Path, help='只读取此目录内、ELF记录的实际源码路径；不猜basename')
    def bounds(p):
        p.add_argument('--from-seq', dest='start', type=int)
        p.add_argument('--to-seq', dest='end', type=int)
    p=sub.add_parser('doctor', help='检查依赖；--hardware才枚举硬件');p.add_argument('--hardware', action='store_true');la(p);p.set_defaults(func=doctor_command)
    target=sub.add_parser('target', help='目标回读、配置、恢复、停止').add_subparsers(dest='target_action', required=True)
    for name in ('inspect','configure','restore','stop'):
        p=target.add_parser(name);hw(p);p.set_defaults(func=target_command)
        if name in ('configure','restore'):p.add_argument('--state', required=True)
        if name=='configure':
            p.add_argument('--elf', required=True, type=Path);p.add_argument('--resume', action='store_true');options(p)
        if name=='stop':p.add_argument('--halt', action='store_true')
    p=sub.add_parser('capture', help='校验ELF→配置→预运行→KVDAT→恢复→原生解码');hw(p);la(p);options(p);sources(p)
    p.add_argument('--elf', required=True, type=Path);p.add_argument('--out', required=True, type=Path)
    p.add_argument('--seconds', type=float, default=.01);p.add_argument('--rate', type=int, default=500000000)
    p.add_argument('--clock', type=int, default=0);p.add_argument('--data', type=int, nargs=4, default=[1,2,3,4])
    p.add_argument('--fraction', type=float, default=.25);p.add_argument('--glitch-ns', type=float, default=4)
    p.add_argument('--threshold', type=float, default=1.65);p.add_argument('--no-decode', action='store_true')
    p.add_argument('--echo-port');p.add_argument('--echo-interval', type=float, default=.004)
    p.add_argument('--expect-core-hz', type=float);p.add_argument('--expect-trace-hz', type=float)
    p.set_defaults(func=capture_command)
    p=sub.add_parser('decode', help='原始输入或已有记录→新目录（默认原生优化）');g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--input', type=Path);g.add_argument('--record', type=Path)
    p.add_argument('--elf', type=Path);p.add_argument('--config', type=Path);p.add_argument('--out', required=True, type=Path);sources(p);p.set_defaults(func=decode_command)
    p=sub.add_parser('query', help='只读TraceStore查询');p.add_argument('--record', required=True, type=Path)
    p.add_argument('--view', choices=('summary','overview','page','event','navigate','window','timeline','issues','source'), default='summary')
    p.add_argument('--table', choices=('events','packets'), default='events');p.add_argument('--offset', type=int, default=0)
    p.add_argument('--limit', type=int, default=150);p.add_argument('--seq', type=int, default=0);p.add_argument('--direction', type=int, choices=(-1,1), default=1)
    p.add_argument('--mode', choices=('instruction','call','return','exception','issue','source','pc'), default='instruction')
    p.add_argument('--pc', type=lambda s:int(s,0));p.add_argument('--search', default='');p.add_argument('--hide-skipped', action='store_true')
    p.add_argument('--file', default='');p.add_argument('--bins', type=int, default=1200);bounds(p);p.set_defaults(func=query_command)
    p=sub.add_parser('verify', help='输入指纹、实际长度、store绑定和解码质量');p.add_argument('--record', required=True, type=Path)
    p.add_argument('--require-decoded', action='store_true');p.set_defaults(func=lambda a:verify_record(a.record,a.require_decoded))
    p=sub.add_parser('export', help='导出执行流、原始数据或完整记录包');p.add_argument('--record', required=True, type=Path)
    p.add_argument('--format', required=True, choices=('simplified','source','html','csv','json','events-jsonl','raw','etm','record'))
    p.add_argument('--out', required=True, type=Path);bounds(p);p.set_defaults(func=export_command)
    p=sub.add_parser('import-record', help='安全解压并核验.etmrecord，不自动重解');p.add_argument('--input', required=True, type=Path)
    p.add_argument('--out', required=True, type=Path);p.set_defaults(func=import_command)
    return parser


def main(argv=None):
    started=time.perf_counter();stdout=sys.stdout;args=None;report_reserved=False
    envelope=dict(protocol=VERSION, runId=time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
    code=0
    try:
        args=build_parser().parse_args(argv)
        envelope['command']=args.command
        if args.json_out and args.json_out.exists():
            raise CliError('结果文件已存在，拒绝覆盖：'+str(args.json_out))
        if args.json_out:
            args.json_out.parent.mkdir(parents=True,exist_ok=True)
            with args.json_out.open('xb'):pass
            report_reserved=True
        if getattr(args,'limit',150) not in range(1,501) or getattr(args,'bins',1200) not in range(1,5001):
            raise CliError('limit须为1～500；bins须为1～5000')
        if hasattr(args,'hse') and (not 0 < args.hse <= 100 or not 0 < args.swd_hz <= 50000000):
            raise CliError('HSE须为0～100MHz的正数，SWD须为正数且≤50MHz')
        if hasattr(args,'echo_interval') and not .001 <= args.echo_interval <= 1:
            raise CliError('echo间隔须为0.001～1秒')
        if getattr(args,'start',None) is not None and args.start < 0:
            raise CliError('区间起点不能为负数')
        if getattr(args,'start',None) is not None and getattr(args,'end',None) is not None and args.end < args.start:
            raise CliError('区间结束不能小于起点')
        with contextlib.redirect_stdout(sys.stderr):
            engine_environment()
            result=args.func(args)
        if isinstance(result,dict) and result.get('passed') is False:
            raise CliError('验证未通过',5,result)
        envelope.update(ok=True,result=result)
    except KeyboardInterrupt:
        code=130;envelope.update(ok=False,error=dict(type='Interrupted',message='用户中断；检查输出目录cleanup.json'))
    except Exception as error:
        code=getattr(error,'code',None) or (3 if isinstance(error,ImportError) else 4 if isinstance(error,(OSError,TimeoutError,RuntimeError)) else 2)
        envelope.update(ok=False,error=dict(type=type(error).__name__,message=str(error),
            details=getattr(error,'details',None),notes=getattr(error,'__notes__',[])))
    envelope.update(exitCode=code,elapsedSeconds=time.perf_counter()-started)
    if report_reserved:
        try:
            if args.json_out.stat().st_size:
                raise FileExistsError('结果预留文件被其他进程修改')
            save(args.json_out,envelope)
        except Exception as error:
            envelope.update(ok=False,exitCode=4,error=dict(type=type(error).__name__,message='结果文件保存失败：'+str(error)));code=4
    print(json.dumps(envelope,ensure_ascii=False),file=stdout)
    return code
