"""Managed local Web-command runtime. No browser, pyOCD or global settings."""
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from trace_cli_paths import asset_root, backend_root, node_executable, host_command


ROOT=asset_root()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def write(path,value):
    temporary=Path(path).with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
    os.replace(temporary,path)


def request(runtime,payload):
    token=(Path(runtime)/'cli-token').read_text(encoding='ascii').strip()
    state=read(Path(runtime)/'runtime.json')
    headers={'X-Trace-CLI':token,'Content-Type':'application/json'}
    with urlopen(Request(state['url']+'/trace-cli/request',data=json.dumps(payload).encode(),headers=headers),timeout=5) as response:
        jid=json.load(response)['id']
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        with urlopen(Request(state['url']+'/trace-cli/result/'+jid,headers=headers),timeout=5) as response:
            value=json.load(response)
        if value['state']=='done':
            with urlopen(Request(state['url']+'/trace-cli/ack/'+jid,data=b'{}',headers=headers),timeout=5):pass
            return value['result']
        time.sleep(.1)
    raise TimeoutError('运行时状态查询超时')


def start(folder,url,probe=None,image_root=None):
    folder=Path(folder).resolve()
    parsed=urlsplit(url)
    if parsed.hostname not in ('127.0.0.1','localhost') or parsed.scheme!='http' or parsed.path not in ('','/') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('运行时URL须为 http://127.0.0.1:端口')
    port=parsed.port or 80
    if not 1024<=port<=65535:raise ValueError('端口范围1024～65535')
    node=node_executable()
    dependency=ROOT/'etm/tools/node-transport/node_modules/usb'
    if not dependency.is_dir() or not (dependency.parent/'linkedom').is_dir():raise RuntimeError('请先 npm ci --prefix etm/tools/node-transport')
    # Never reuse another application's listener or overwrite an old runtime.
    with socket.socket() as check:
        check.bind(('127.0.0.1',port))
    folder.mkdir(parents=True,exist_ok=False)
    token=folder/'cli-token';token.write_text(secrets.token_hex(32),encoding='ascii')
    options=dict(url=f'http://127.0.0.1:{port}',probe=probe,imageRoot=str(Path(image_root).resolve()) if image_root else None,
                 nonce=secrets.token_hex(16),status='starting',root=str(ROOT),created=time.time())
    write(folder/'runtime.json',options)
    log=(folder/'host.log').open('xb')
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    try:
        child=subprocess.Popen(host_command(folder),
            cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=flags)
    except OSError as error:
        write(folder/'runtime.json',dict(options,status='failed',error=str(error)))
        raise
    finally:log.close()
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        state=read(folder/'runtime.json')
        if state['status']=='failed':raise RuntimeError(state.get('error','运行时启动失败'))
        if state['status']=='ready':return state
        if child.poll() is not None:raise RuntimeError('运行时已退出，请查 '+str(folder/'host.log'))
        time.sleep(.1)
    write(folder/'stop-request.json',dict(nonce=options['nonce']))
    raise TimeoutError('运行时尚未就绪，已请求正常退出；保留目录供检查：'+str(folder))


def stop(folder):
    folder=Path(folder).resolve();state=read(folder/'runtime.json')
    if state['status']=='stopped':return state
    if state['status']=='failed':raise RuntimeError(state.get('error','运行时已失败，请检查host.log'))
    write(folder/'stop-request.json',dict(nonce=state['nonce']))
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        state=read(folder/'runtime.json')
        if state['status']=='stopped':return state
        if state['status']=='failed':raise RuntimeError(state.get('error','运行时退出时有清理错误'))
        time.sleep(.2)
    raise TimeoutError('运行时尚在清理；不强制终止硬件操作，请查host.log与runtime.json')


def host(folder):
    import importlib.util
    from werkzeug.serving import make_server
    folder=Path(folder);state=read(folder/'runtime.json')
    state.update(hostPid=os.getpid(),status='starting')
    server=worker=None
    try:
        spec=importlib.util.spec_from_file_location('trace_web_service',ROOT/'web/tools/dev/serve-etm.py')
        service=importlib.util.module_from_spec(spec);spec.loader.exec_module(service)
        os.environ['WEB_ETM_RECORDS']=str(folder/'records')
        app=service.create_application(backend_root(),ROOT/'web',folder/'cli-token')
        sys.modules['decode_trace'].ROOT=backend_root()
        server=make_server('127.0.0.1',urlsplit(state['url']).port,app,threaded=True)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        argv=[node_executable(),str(ROOT/'etm/tools/node-transport/worker.mjs'),'--web-url',state['url'],
              '--token-file',str(folder/'cli-token')]
        if state['probe']:argv+=['--probe',state['probe']]
        if state['imageRoot']:argv+=['--image-root',state['imageRoot']]
        worker=subprocess.Popen(argv,cwd=ROOT,stdin=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        state['workerPid']=worker.pid;write(folder/'runtime.json',state)
        deadline=time.monotonic()+12
        while True:
            if worker.poll() is not None:raise RuntimeError('WebUSB worker退出，请查host.log')
            try:
                answer=request(folder,dict(action='ping'))
                if answer.get('ok'):break
            except Exception:
                if time.monotonic()>deadline:raise
                time.sleep(.1)
        state['status']='ready';write(folder/'runtime.json',state)
        while worker.poll() is None:
            stop_file=folder/'stop-request.json'
            if stop_file.is_file() and read(stop_file).get('nonce')==state['nonce']:
                state['status']='stopping';write(folder/'runtime.json',state)
                worker.stdin.write(b'stop\n');worker.stdin.flush()
                worker.wait(timeout=960)
                break
            time.sleep(.2)
        if worker.returncode not in (None,0):raise RuntimeError('worker异常退出，硬件清理需检查')
        state['status']='stopped'
    except BaseException as error:
        state.update(status='failed',error=str(error))
        raise
    finally:
        # A failed handshake must not orphan a worker. Never force-kill a
        # possible Flash operation: the worker finishes it before disconnect.
        if worker and worker.poll() is None:
            try:
                worker.stdin.write(b'stop\n');worker.stdin.flush()
                worker.wait(timeout=960)
                if worker.returncode:
                    state.update(status='failed',error='worker退出清理失败，请检查host.log')
            except Exception as cleanup:
                state.update(status='failed',cleanupPending=True,cleanupError=str(cleanup))
        if server:server.shutdown();server.server_close()
        state['ended']=time.time();write(folder/'runtime.json',state)


if __name__=='__main__':host(sys.argv[2])
