"""仅用于本项目真机验收：把 pyOCD 操作适配给 target.js 的公开接口。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pyocd.core.helpers import ConnectHelper
from pyocd.core.target import Target


session = ConnectHelper.session_with_chosen_probe(target_override='stm32h7b0xx', options={
    'frequency': 1000000, 'connect_mode': 'attach', 'auto_unlock': False, 'resume_on_disconnect': True})
guard = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            args = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with guard:
                target = session.target
                op = args['op']
                value = None
                if op == 'read':
                    value = target.read_memory_block8(args['address'], args['length'])
                elif op == 'write':
                    target.write_memory_block8(args['address'], args['bytes'])
                    target.flush()
                elif op == 'halt':
                    target.halt()
                elif op == 'run':
                    target.resume()
                elif op == 'state':
                    value = target.get_state() == Target.State.HALTED
                elif op == 'reset':
                    # Preserve production sysReset semantics: vector catch must hold.
                    target.reset_and_halt(reset_type=Target.ResetType.SYSRESETREQ)
                elif op == 'quit':
                    target.resume()
                    threading.Thread(target=server.shutdown, daemon=True).start()
                else:
                    raise ValueError('Unknown test operation')
            body = json.dumps({'value': value}).encode()
            self.send_response(200)
        except Exception as error:
            body = json.dumps({'error': str(error)}).encode()
            self.send_response(400)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    with session:
        server = ThreadingHTTPServer(('127.0.0.1', 8767), Handler)
        print('Hardware acceptance adapter ready at 127.0.0.1:8767', flush=True)
        server.serve_forever()
