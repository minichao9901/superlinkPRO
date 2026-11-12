"""Run the first-use manual's fixed CLI commands from the local workbench."""

from dataclasses import dataclass
import hmac
import json
from pathlib import Path
import re
import shlex
import subprocess
import threading
import uuid

from flask import abort, jsonify, request


WEB = 'python -X utf8 toolbox.py --runtime tmp/first-run-web '
TARGET = 'target-flash --chip "$Chip" --file "$Firmware"'


@dataclass(frozen=True)
class Command:
    name: str
    line: str
    confirmation: str = ''
    fields: tuple[str, ...] = ()


COMMANDS = {item.line: item for item in (
    Command('workbench', WEB + 'serve --port 8896'),
    Command('ping', WEB + 'ping'),
    Command('catalog', 'python -X utf8 toolbox.py catalog'),
    Command('targets', WEB + 'target info'),
    Command('options', WEB + 'page flash describe'),
    Command('chip', "$Chip = Read-Host '输入目标芯片 ID'", fields=('chip',)),
    Command('firmware', "$Firmware = Read-Host '输入目标固件 ELF 的绝对路径'", fields=('firmware',)),
    Command('prepare', WEB + TARGET + ' --prepare-only', fields=('chip', 'firmware')),
    Command('identity', WEB + 'page flash invoke readIdcode'),
    Command('program', WEB + '--timeout 240 ' + TARGET,
            '擦写所选目标 Flash，读回校验并复位。', ('chip', 'firmware')),
    Command('flash-result', WEB + "page flash read '#f-result'"),
    Command('flash-log', WEB + "page flash read '#f-log'"),
    Command('job', WEB + 'job JOB_ID', fields=('job',)),
    Command('probe-firmware', 'python -X utf8 toolbox.py probe-flash --file "$ProbeFirmware"',
            '写入已进入 DFU 的 SuperLinkPRO 探针 APP，读回校验后重启。', ('probe-firmware',)),
)}
BY_NAME = {item.name: item for item in COMMANDS.values()}
FENCE = re.compile(r'^   ```powershell\n([^\n]+)\n   ```$', re.M)


def read_manual(manual):
    source = manual.read_text(encoding='utf8').strip()
    rows = []
    start = 0
    for match in FENCE.finditer(source):
        rows.append({'text': source[start:match.start()].strip()})
        line = match[1].strip()
        command = COMMANDS.get(line)
        if command is None:
            raise ValueError(f'手册命令尚未绑定执行操作：{line}')
        rows.append({'id': command.name, 'line': line, 'fields': command.fields,
                     'confirmation': command.confirmation})
        start = match.end()
    if not rows or source.count('```powershell') * 2 != source.count('```'):
        raise ValueError('入门手册的命令块格式有误')
    rows.append({'text': source[start:].strip()})
    return rows


def firmware_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or path.suffix.lower() != '.elf' or not path.is_file():
        raise ValueError('请输入本机已有的 ELF 文件的绝对路径')
    return str(path.resolve())


def start_command(command, values, runtime, prefix, cwd, output):
    selected = {field: values.get(field, '').strip() for field in command.fields}
    if 'chip' in selected and not re.fullmatch('[a-z0-9_-]{1,64}', selected['chip']):
        raise ValueError('请输入烧录器显示的内置芯片 ID')
    if 'firmware' in selected:
        selected['firmware'] = firmware_path(selected['firmware'])
    if 'probe-firmware' in selected:
        path = Path(selected['probe-firmware']).expanduser()
        if not path.is_absolute() or path.suffix.lower() != '.bin' or not path.is_file():
            raise ValueError('请输入已有的探针 APP BIN 文件的绝对路径')
        selected['probe-firmware'] = str(path.resolve())
    if 'job' in selected and not re.fullmatch('[0-9a-f]{32}', selected['job']):
        raise ValueError('请输入 32 位十六进制的 CLI job ID')
    if command.name in ('chip', 'firmware'):
        output.write(f"已保存 {selected[command.name]}\n".encode('utf8'))
        return None
    if command.name == 'workbench':
        state = json.loads((runtime / 'toolbox-runtime.json').read_text(encoding='utf8'))
        output.write(json.dumps({
            'ready': True, 'url': state['url'], 'runtime': str(runtime),
        }, indent=2).encode('utf8'))
        return None
    substitutions = {'tmp/first-run-web': str(runtime), '$Chip': selected.get('chip'),
                     '$Firmware': selected.get('firmware'), 'JOB_ID': selected.get('job'),
                     '$ProbeFirmware': selected.get('probe-firmware')}
    args = [substitutions.get(arg, arg) for arg in shlex.split(command.line)[4:]]
    if command.name == 'probe-firmware':
        args.extend(('--backup-dir', str(Path(output.name).parent / 'artifacts')))
    argv = [*prefix, *args]
    output.write((subprocess.list2cmdline(argv) + '\n\n').encode('utf8'))
    output.flush()
    return subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def install(app, token_file, manual, prefix, cwd):
    """Install only on the authenticated, loopback workbench origin."""
    token = token_file.read_bytes().strip()
    runtime = token_file.parent.resolve()
    jobs = {}
    lock = threading.Lock()

    def authorize():
        supplied = request.headers.get('X-Trace-CLI', '').encode('utf8')
        if not hmac.compare_digest(token, supplied):
            abort(403)
        if request.method == 'POST' and request.headers.get('Origin') != request.host_url.rstrip('/'):
            abort(403)

    @app.get('/command-manual/status')
    def command_manual_status():
        authorize()
        return jsonify(available=True)

    @app.get('/command-manual')
    def command_manual():
        authorize()
        try:
            return jsonify(rows=read_manual(manual))
        except (OSError, ValueError) as error:
            return jsonify(error=str(error)), 409

    @app.post('/command-manual/run')
    def run_manual_command():
        authorize()
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get('id'), str):
            abort(400)
        command = BY_NAME.get(body['id'])
        if command is None:
            abort(404)
        if command.confirmation and body.get('confirmed') is not True:
            return jsonify(error='请确认此操作。'), 400
        values = body.get('values', {})
        if not isinstance(values, dict) or any(not isinstance(values.get(field, ''), str)
                                              or len(values.get(field, '')) > 4096 for field in command.fields):
            abort(400)
        with lock:
            if any(process is not None and process.poll() is None for process, _, _ in jobs.values()):
                return jsonify(error='手册中已有命令在运行，请等它结束。'), 409
            job = uuid.uuid4().hex
            folder = runtime / 'manual' / job
            folder.mkdir(parents=True)
            log = folder / 'output.log'
            exit_code = 0
            with log.open('wb') as output:
                try:
                    process = start_command(command, values, runtime, prefix, cwd, output)
                except (OSError, ValueError, KeyError) as error:
                    output.write(f'{error}\n'.encode('utf8'))
                    process = None
                    exit_code = 1
            if len(jobs) >= 64:
                del jobs[next(iter(jobs))]
            jobs[job] = process, exit_code, log
        return jsonify(job=job)

    @app.get('/command-manual/job/<job>')
    def manual_job(job):
        authorize()
        entry = jobs.get(job)
        if entry is None:
            abort(404)
        process, exit_code, log = entry
        if process is not None:
            exit_code = process.poll()
        with log.open('rb') as output:
            size = output.seek(0, 2)
            offset = max(0, size - 96 * 1024)
            output.seek(offset)
            text = output.read(96 * 1024).decode('utf8', errors='replace')
        return jsonify(exitCode=exit_code, output=text, truncated=bool(offset), log=str(log))
