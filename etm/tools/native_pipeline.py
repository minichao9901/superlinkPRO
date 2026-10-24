"""Native execution store + numeric facts index, published as one generation.

Python handles unique-code preparation, vector reductions and publication. No
Python per-instruction reconstruction or reference index participates in build.
"""
import json
import secrets
import struct
import time
import hashlib
from pathlib import Path

import numpy as np
import trace_store as ts


def _entries(store):
    fields = ('pc', 'size', 'flow', 'predicated', 'function', 'file', 'line',
              'bytes', 'mnemonic', 'operands', 'conditional')
    return [{key: getattr(entry, key) for key in fields} for entry in store.static]


def _issues(core, facts, quality, diagnostics):
    import native_binary
    import trace_store_write as writer
    offsets = dict(diagnostics['events'])
    result = []
    events = facts['events']
    for event in events:
        if int(event['type']) in ts.BOUNDARY_TYPES:
            seq = int(event['seq'])
            result.append(dict(category='boundary', startSeq=seq, endSeq=seq,
                               offset=offsets[seq], reason=facts['evtexts'][int(event['textId'])], records=1))
    if 'predicateIssues' in diagnostics:
        result.extend(dict(category='predicate',startSeq=lo,endSeq=hi,records=hi-lo+1,
                           offset=offset,reason=ts.IT_REASON)
                      for lo,hi,offset in diagnostics['predicateIssues'])
        unknown = np.empty(0, np.int64)
    else:
        unknown = np.flatnonzero(facts['states'] == 2)
    if len(unknown):
        runs = facts['runs']
        run_indexes = np.searchsorted(runs['startOrdinal'], unknown, side='right') - 1
        seq = runs['startSeq'][run_indexes] + unknown - runs['startOrdinal'][run_indexes]
        first = np.concatenate(([0], np.flatnonzero(np.diff(seq) != 1) + 1))
        last = np.concatenate((first[1:] - 1, [len(unknown) - 1]))
        for begin, end in zip(first, last):
            run = runs[run_indexes[begin]]
            body = core._sequence_payload(int(run['blockOrdinal']))
            pos = int(run['recordOffset']) + 1
            _, pos = ts._dvar(body, pos)  # record length
            _, pos = ts._dvar(body, pos)  # first seq
            _, pos = ts._dvar(body, pos)  # count
            pos += 1                    # tail state
            offset, _ = ts._dvar(body, pos)
            result.append(dict(category='predicate', startSeq=int(seq[begin]), endSeq=int(seq[end]),
                               records=int(end - begin + 1), offset=offset, reason=ts.IT_REASON))
    # The ordinary writer emits predicate and boundary issues in event order.
    result.sort(key=lambda row: row['startSeq'])
    faults = [dict(category='packet', startSeq=None, endSeq=None, records=0,
                   offset=offset, bytes=size,
                   reason='包问题：' + native_binary.packet_name(kind, core.meta['record']['protocol']))
              for offset, size, kind in diagnostics['faults']]
    result.extend(writer._packet_issues(quality, diagnostics['status'], quality['inputBytes'],
                                       diagnostics['packetEnd'], faults))
    return result


def write(record_dir, store_dir, **options):
    import decode_stream
    import native_binary
    import native_facts_index
    import native_storebuild as native
    import static_dict
    import trace_store_index as query
    import trace_store_write as writer
    from decode_trace import Image, decoder_config

    started = time.perf_counter()
    record_dir, store_dir = Path(record_dir).resolve(), Path(store_dir).resolve()
    metadata = options.get('metadata')
    if metadata is None and (record_dir / 'decoded.json').exists():
        metadata = json.loads((record_dir / 'decoded.json').read_text(encoding='utf8'))
    metadata = metadata or {}
    config = options.get('config') or metadata.get('config')
    if config is None and (record_dir / writer.CONFIG_FILE).exists():
        config = writer.manifest_config(record_dir / writer.CONFIG_FILE)
    config = decoder_config(config)
    elf = Path(options.get('elf') or record_dir / 'firmware.elf')
    image = options.get('image') or Image(elf, options.get('sources'))
    generation = 'g-%s-%s' % (time.strftime('%Y%m%dT%H%M%S'), secrets.token_hex(4))
    tag = secrets.randbits(63) or 1
    directory = store_dir / 'generations' / generation
    directory.mkdir(parents=True)
    work = directory / 'work'
    work.mkdir()
    phases = {}
    since = started

    def mark(name):
        nonlocal since
        now = time.perf_counter()
        phases[name] = round(now - since, 6)
        since = now

    core = None
    published = False
    metadata_before = {}
    try:
        etm = Path(options.get('etm') or record_dir / 'etm.bin')
        native_path = Path(options.get('native_bin') or record_dir / 'native.bin')
        decode_info = None
        if not native_path.is_file():
            native_path, decode_info = writer.decode_to_native(etm, image, config, work,
                                                               decoder=options.get('decoder'))
        native_sha = ts._sha256_file(native_path)
        elf_sha = hashlib.sha256(image.bytes).hexdigest()
        if ts._sha256_file(elf) != elf_sha:
            raise ts.TraceStoreError('ELF 在读取期间发生变化')
        protocol = native_binary.NativeFile(native_path).protocol
        expected_protocol = 'ETMv3' if config.get('protocol') == 'ETMv3' else 'ETMv4'
        if protocol != expected_protocol:
            raise ts.TraceStoreError('原生输入协议与配置不符')
        mark('decodeAndInputs')
        scan_path = work / 'ranges.bin'
        scan = native.prescan(native_path, out=scan_path)
        raw = scan_path.read_bytes()
        if raw[:9] != b'R2RANGES1' or len(raw) < 25:
            raise ts.TraceStoreError('原生唯一范围表无效')
        count = struct.unpack_from('<Q', raw, 9)[0]
        if len(raw) != 25 + count * 12:
            raise ts.TraceStoreError('唯一范围表长度不符')
        ranges = list(struct.iter_unpack('<III', raw[25:]))
        entries, static_costs = static_dict.build_static_entries(ranges, elf, image=image,
                                                       verify_image=options.get('verify_image', True))
        dictionary = work / 'static.bin'
        static_dict.write_static_dict(dictionary, entries, elf_sha)
        mark('prescanAndStatic')
        facts_path = work / 'facts.bin'
        index_facts_path = (work / 'index-facts.bin' if 'index-facts-v1' in
                            (options.get('native_capabilities') or {}).get('capabilities', []) else None)
        built = native.build(native_path, dictionary, directory, generation=generation,
                             generation_tag=tag, analysis=True, facts_out=facts_path,
                             reserve_instructions=scan.get('rangeInstructions'),
                             index_facts_out=index_facts_path)
        if 'diagnostics' not in built:
            raise writer.NativeStorebuildUnavailable('原生 builder 缺少完整质量诊断，请重新构建')
        writer._validate_native_build_result(built,
            inputs=dict(nativeSha256=native_sha, elfSha256=elf_sha),
            static_dict_sha256=ts._sha256_file(dictionary))
        counts, diagnostics = built['counts'], built['diagnostics']
        if options.get('max_events') and counts['instructions'] > options['max_events']:
            raise ts.TraceStoreError('指令记录超过 max_events')
        if not counts['instructions']:
            raise ts.TraceStoreError('没有恢复到可信指令范围')
        mark('nativeBuild')
        # Private manifest opens the completed core; public manifest is untouched.
        private = work / 'core'
        target = private / 'generations' / generation
        target.mkdir(parents=True)
        import os
        os.link(directory / 'store.bin', target / 'store.bin')
        initial = dict(format=ts.FORMAT_NAME, schemaVersion=1, state='complete',
                       byteOrder='little', generation=generation, generationTag=hex(tag),
                       files=built['files'], counts=counts, bindings={})
        (private / 'manifest.json').write_text(json.dumps(initial), encoding='utf8')
        core = ts.TraceStore(private)
        facts = native_facts_index.read_facts_any(facts_path)
        entries = _entries(core)  # facts IDs use core order, not the PC-sorted transport dictionary.
        if index_facts_path is not None:
            facts['indexFacts'] = native_facts_index.read_index_facts(
                index_facts_path, facts, entries, native_sha)
        full = native_facts_index.build_full_index(facts, entries, core.first_seq, core.last_seq,
                                                   view=core, static_first_seen=True)
        if full['meta']['compatPending']:
            raise ts.TraceStoreError('查询索引尚不完整')
        index_meta = query._index_meta(full['meta'], dict(payloadBytes=len(full['payload']),
            buildSeconds=full['timings']['totalSeconds'], defaultsSeconds=full['timings'].get('defaultViewsSeconds', 0)))
        mark('queryIndex')
        status = dict(diagnostics['status'], kind='status')
        input_bytes = options.get('input_bytes')
        if input_bytes is None:input_bytes = etm.stat().st_size if etm.exists() else status['processed']
        counters = dict(diagnostics, unknown_execution=counts['unknownExecution'],
                        first_path_offset=diagnostics['firstRangeOffset'])
        framing = options.get('framing')
        if framing is None:framing = metadata.get('framing')
        quality = decode_stream.quality_from_counters(status, input_bytes, counters, framing)
        analysis = core.summary['analysis']
        core_hz = ((metadata.get('analysis') or {}).get('timing') or {}).get('coreHz')
        if core_hz is not None:analysis['timing']['coreHz'] = core_hz
        source_mapped = int(np.count_nonzero(np.array([bool(e['line']) for e in entries])[facts['ids']]))
        summary = dict(packets=counts['packets'], ranges=counts['ranges'], instructions=counts['instructions'],
                       executed=counts['executed'], sourceMapped=source_mapped,
                       exceptions=counts['exceptions'], gaps=counts['gaps'], packetIssues=quality['packetIssues'],
                       unknownExecution=counts['unknownExecution'],
                       functions={r['function']:r['knownExecuted'] for r in analysis['functions'] if r['knownExecuted']})
        source_data = {name:image.source(name) for name in sorted({e['file'] for e in entries if e['file']})}
        decoded = dict(version=3, protocol=protocol, config=config, inputBytes=input_bytes,
            elf=dict(name=elf.name, sha256=ts._sha256_file(elf)), summary=summary, quality=quality,
            analysis=analysis, status=status, timeBasis=writer.TEXT_BASIS,
            streamed=dict(eventsFile=ts.STORE_FILE, events=counts['events'], elements=diagnostics['elements'],
                          packets=counts['packets'], nativeJsonl='native.bin', schema=2))
        for key in writer.METADATA_KEYS:
            if key in metadata and key not in decoded:decoded[key] = metadata[key]
        for key in ('physical','framing','capture'):
            if options.get(key) is not None:decoded[key] = options[key]
        session = writer._load_json(record_dir / 'session.json') if (record_dir/'session.json').exists() else {}
        for key in ('targetSnapshot','captureScope','laFilter'):
            if key not in decoded and key in session:decoded[key] = session[key]
        decoded['traceStore'] = dict(format=ts.FULL_FORMAT_NAME,schemaVersion=1,store='store',source='native.bin',
                                    generation=generation,counts={k:counts[k] for k in ('events','instructions','runs','staticPcs','packets')})
        issues = _issues(core, facts, quality, diagnostics)
        meta = dict(core.meta, queryIndex=index_meta, issues=len(issues),
                    verify=dict(elements=True,elementsVerified=counts['ranges'],image=True,
                                machineCodeCrossCheck=True,staticPcs=counts['staticPcs']),
                    packetText=dict(available=False,reason='按需重放原始 ETM 生成包说明'))
        stored_summary = dict(decoded,eventCount=counts['events'],packetCount=counts['packets'],
                              sourceFiles=list(source_data),firstSeq=core.summary['firstSeq'])
        sink = ts._BlockSink(work / 'publish.payload', expected_blocks=
                             sum(b.block_type != ts.BLK_QUERY for b in core.container.blocks) + 1)
        try:
            replacements={ts.BLK_META:meta,ts.BLK_SUMMARY:stored_summary,ts.BLK_SOURCES:source_data,ts.BLK_ISSUES:issues}
            for ordinal, block in enumerate(core.container.blocks):
                if block.block_type in replacements:sink.add_compressed_json(block.block_type,replacements[block.block_type])
                elif block.block_type != ts.BLK_QUERY:sink.add_raw(block.block_type,core.container.raw_block(ordinal),block.count,block.flags)
            sink.add(ts.BLK_QUERY, full['payload'], int(full['meta']['cells']))
            core.close();core=None
            info=sink.assemble(directory/'store.bin',tag,optional_features=ts.QUERY_INDEX_FEATURE)
        finally:sink.close()
        mark('assemble')
        hash_inputs=options.get('hash_inputs',True)
        names=('etm.bin','native.bin','native-config.txt','firmware.elf','physical.json','framing.json','tpiu.bin',
               'session.json','sources.json')
        names += tuple(p.name for p in record_dir.glob('image-*.bin'))
        capture=decoded.get('capture') or {}
        if capture.get('name'):names += (Path(capture['name']).name,)
        inputs={name:dict(bytes=(record_dir/name).stat().st_size,
                         sha256=ts._sha256_file(record_dir/name) if hash_inputs else None)
                for name in names if (record_dir/name).is_file()}
        if hash_inputs:
            for name, expected in (('firmware.elf', elf_sha), ('native.bin', native_sha)):
                if name in inputs and inputs[name]['sha256'] != expected:
                    raise ts.TraceStoreError('输入在构建期间变化：' + name)
        mark('inputBindings')
        engine=dict(name='native',version=built['engine']['buildSha'],fallback=False,
                    buildCosts=dict(phases=phases,index=full['timings'],static=static_costs,
                                    image=getattr(image,'preparation',None)))
        bindings=dict(inputs=inputs,config=config,engine=engine,
                      processing=dict(traceStore=1,engine='native',nativeFormat='trace-bin native v1'))
        if not hash_inputs:bindings['inputHash']=dict(skipped=True,computed=False)
        manifest=dict(initial,files=[dict(name='store.bin',**info)],counts=counts,
                      fieldPresence=meta['fieldPresence'],bindings=bindings,queryIndex=index_meta,engine=engine)
        if options.get('write_decoded'):
            for name, value in (('decoded.json', decoded), ('sources.json', source_data)):
                destination = record_dir / name
                metadata_before[destination] = destination.read_bytes() if destination.exists() else None
                staged = work / name
                staged.write_text(json.dumps(value, ensure_ascii=False), encoding='utf8')
                os.replace(staged, destination)
                if name=='sources.json':
                    inputs[name]=dict(bytes=destination.stat().st_size,
                                      sha256=ts._sha256_file(destination) if hash_inputs else None)
        ts._publish_manifest(store_dir,manifest)
        published = True
        mark('publish')
        if decode_info is not None and options.get('keep_native'):
            kept = directory / 'native.bin'
            os.replace(native_path, kept)
            native_path = kept
        warnings = []
        writer._compare_declared(metadata, summary, quality, warnings)
        if not hash_inputs:
            warnings.append('--no-hash-inputs：发布绑定未计算输入哈希')
        # All persisted arrays are now in store.bin; scratch facts/native output
        # must not become a second permanent copy of the capture.
        del full, facts
        import gc
        gc.collect()
        ts._remove_tree(work)
        mark('cleanup')
        return dict(ok=True,storeDir=str(store_dir),generation=generation,counts=counts,
                    elapsedSeconds=time.perf_counter()-started,phases=phases,queryIndex=index_meta,
                    storeBytes=info['bytes'],storeSha256=info['sha256'],engine=engine,
                    inputBytes=input_bytes, nativePath=str(native_path) if native_path.is_file() else None,
                    nativeGenerated=decode_info is not None, decodeInfo=decode_info,
                    peakRssBytes=ts.peak_rss_bytes(), nativePeakRssBytes=built.get('peakRssBytes'),
                    elementsVerified=counts['ranges'],staticPcs=counts['staticPcs'],
                    runs=counts['runs'],events=counts['events'],instructions=counts['instructions'],
                    packetText=False, decodedWritten=bool(options.get('write_decoded')),
                    decodedPath=str(record_dir/'decoded.json') if options.get('write_decoded') else None,
                    warnings=warnings),decoded
    except Exception:
        if core is not None:core.close()
        if not published:
            for destination, payload in metadata_before.items():
                if payload is None:
                    destination.unlink(missing_ok=True)
                else:
                    staged = work / (destination.name + '.restore')
                    staged.write_bytes(payload)
                    os.replace(staged, destination)
            ts._remove_tree(directory)
        raise
