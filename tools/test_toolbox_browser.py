"""Read/preview/export acceptance against an already-open CLI browser, no device I/O."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import toolbox


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime', required=True)
    p.add_argument('--out', required=True)
    args = p.parse_args()
    if Path(args.out).exists():
        raise FileExistsError(args.out)
    client = toolbox.Client(args.runtime, 30)
    evidence = {'hardwareOperations': False, 'pages': {}, 'checks': []}

    def call(page, op, **extra):
        result = client.request({'action': 'workbench', 'page': page, 'op': op, **extra})
        if not result.get('ok'):
            raise RuntimeError(str(result))
        client.http('ack/' + result['job'], {})
        return result['value']

    try:
        for page in toolbox.PAGES:
            # ETM frame load is async; retry read-only discovery only.
            for attempt in range(20):
                try:
                    value = call(page, 'describe')
                    break
                except RuntimeError:
                    if page != 'etm' or attempt == 19:
                        raise
                    time.sleep(.2)
            if not value['controls']:
                raise AssertionError('missing controls: ' + page)
            evidence['pages'][page] = {'controls': len(value['controls']), 'methods': len(value['methods']), 'targets': value['targets']}
        evidence['checks'].append('all-15-live-page-descriptions')
        fixture = toolbox.ROOT / 'web/tools/fixtures/gen/uvprojx-sample/CubeMX_Config.uvprojx'
        call('gen', 'upload', selector='#g-uvfile', files=[toolbox.pack_file(fixture)])
        for attempt in range(30):
            project = call('gen', 'read', selector='#g-project')['value']
            if project == 'CubeMX_Config':
                break
            time.sleep(.1)
        assert project == 'CubeMX_Config', project
        evidence['checks'].append('real-file-input-uvprojx-upload-and-parse')
        # FlashView intentionally mounts its picker outside #tab-flash. Loading
        # this tiny image exercises that entry without programming the device.
        flash = call('flash', 'describe')
        picker = next(v['selector'] for v in flash['controls'] if v['selector'].startswith('@file:'))
        call('flash', 'upload', selector=picker, files=[{'$base64': 'AAECAw==', 'name': 'cli-offline-check.bin'}])
        assert 'cli-offline-check.bin' in call('flash', 'read', selector='#f-file-info')['text']
        evidence['checks'].append('out-of-page-flash-file-input-without-programming')
        old = call('analog', 'read', selector='#an-frequency')['value']
        try:
            changed = call('analog', 'set', selector='#an-frequency', value=250)
            assert changed['completed'] is False
            assert call('analog', 'read', selector='#an-frequency')['value'] == '250'
            call('analog', 'invoke', method='preview', args=[])
            summary = call('analog', 'invoke', method='summary', args=[])['value']
            assert summary['wavePoints'] > 0
            assert summary['connected'] is False
            evidence['checks'].append('set-read-and-preview-without-device')
            call('analog', 'click', selector='#an-wave-export')
            for attempt in range(20):
                downloads = call('analog', 'downloads')
                if downloads:
                    break
                time.sleep(.1)
            exported = downloads[-1]
            destination = Path(args.out).with_suffix('.csv')
            # Exercise the user-facing executable entry, including exclusive
            # partial-file handling, rather than only the Python client helper.
            assert toolbox.main(['--runtime', args.runtime, 'page', 'analog', 'download', exported['id'], str(destination)]) == 0
            content = bytearray()
            while len(content) < exported['bytes']:
                chunk = call('analog', 'download', id=exported['id'], offset=len(content), length=1024)
                content.extend(base64.b64decode(chunk['base64'], validate=True))
            text = content.decode('utf-8-sig')
            assert destination.read_bytes() == content
            assert text.splitlines()[0] == 'time_s,volts,code', text[:150]
            assert len(text.splitlines()) == summary['wavePoints'] + 1
            evidence['export'] = {'name': exported['name'], 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'lines': len(text.splitlines())}
            call('analog', 'discard', id=exported['id'])
            evidence['checks'].append('real-page-waveform-csv-blob-and-chunks')
        finally:
            call('analog', 'set', selector='#an-frequency', value=old)
        evidence['passed'] = True
    except BaseException as error:
        evidence.update(passed=False, error=str(error))
        raise
    finally:
        toolbox.emit(evidence, args.out)


if __name__ == '__main__':
    main()
