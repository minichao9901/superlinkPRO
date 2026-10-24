"""Loopback command queue. Browser executes fixed Web commands, never Python eval."""
import hmac
from pathlib import Path
import secrets
import threading
import time


def install_web_cli(app, token_file):
    from flask import request, jsonify, abort
    path = Path(token_file).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='ascii') as stream:
            stream.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    token = path.read_text(encoding='ascii').strip()
    if len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('CLI token file must contain 64 hex characters')
    condition = threading.Condition()
    jobs, pending = {}, []
    worker = {'id':None, 'seen':0}

    @app.before_request
    def authorize_cli():
        if not request.path.startswith('/trace-cli/'):
            return
        if request.remote_addr not in ('127.0.0.1','::1'):
            abort(403)
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'):
            abort(403)
        if not hmac.compare_digest(request.headers.get('X-Trace-CLI',''), token):
            abort(403)
        if request.content_length and request.content_length > 48 << 20:
            abort(413)

    @app.post('/trace-cli/request')
    def submit():
        value = request.get_json()
        if not isinstance(value,dict) or value.get('action') not in (
                'ping','status','connect','disconnect','elf','command','flash','wait-halt','cancel','help','target'):
            abort(400)
        with condition:
            now = time.monotonic()
            for key in list(jobs):
                if now-jobs[key]['created'] > 3600 and jobs[key]['state'] == 'done':
                    del jobs[key]
            if len(pending) >= 20 or len(jobs) >= 200:
                abort(429)
            if not worker['id'] or now-worker['seen'] > 15:
                return jsonify(error='工作台CLI执行页面未连接；请打开带CLI授权的本地工作台'),409
            jid = secrets.token_hex(16)
            jobs[jid] = dict(id=jid,request=value,state='queued',created=now)
            if value['action']=='cancel':pending.insert(0,jid)
            else:pending.append(jid)
            condition.notify_all()
            return jsonify(id=jid)

    @app.get('/trace-cli/result/<jid>')
    def result(jid):
        with condition:
            value = jobs.get(jid)
            if value is None:abort(404)
            return jsonify(id=jid,state=value['state'],result=value.get('result'))

    @app.post('/trace-cli/ack/<jid>')
    def acknowledge(jid):
        with condition:
            value=jobs.get(jid)
            if value is None:abort(404)
            if value['state']!='done':abort(409)
            del jobs[jid]
            return jsonify(removed=True)

    @app.post('/trace-cli/poll')
    def poll():
        data = request.get_json()
        wid = data.get('worker') if isinstance(data,dict) else None
        if not isinstance(wid,str) or len(wid)!=32:abort(400)
        with condition:
            now=time.monotonic()
            if worker['id'] and worker['id'] != wid:
                return jsonify(error='已有另一工作台CLI执行页面；关闭服务后再更换'),409
            worker.update(id=wid,seen=now)
            if not pending:condition.wait(timeout=2)
            worker['seen']=time.monotonic()
            if not pending:return jsonify(job=None)
            jid=pending.pop(0);value=jobs[jid];value['state']='running';value['worker']=wid
            return jsonify(job=dict(id=jid,request=value['request']))

    @app.post('/trace-cli/complete/<jid>')
    def complete(jid):
        data=request.get_json()
        if not isinstance(data,dict) or 'result' not in data:abort(400)
        with condition:
            value=jobs.get(jid)
            if not value or value['state']!='running' or data.get('worker')!=value.get('worker'):abort(409)
            value.update(state='done',result=data['result'])
            condition.notify_all()
            return jsonify(saved=True)
    return dict(tokenFile=str(path))
