"""Local test transport for the actual web EtmTarget controller; one persistent SWD session."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pyocd.core.helpers import ConnectHelper


def main():
    session=ConnectHelper.session_with_chosen_probe(target_override='cortex_m',options={
        'frequency':1000000,'connect_mode':'attach','auto_unlock':False,
        'resume_on_disconnect':True,'enable_memory_cache':False,'enable_register_cache':False})
    with session:
        target=session.target
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                try:
                    body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if self.path=='/read':
                        value={'data':base64.b64encode(bytes(target.read_memory_block8(body['address'],body['length']))).decode()}
                    elif self.path=='/write':
                        target.write_memory_block8(body['address'],list(base64.b64decode(body['data'])));target.flush();value={}
                    elif self.path=='/halt':target.halt();value={}
                    elif self.path=='/run':target.resume();value={}
                    elif self.path=='/shutdown':
                        value={};threading.Thread(target=server.shutdown,daemon=True).start()
                    else:raise ValueError('Unknown action')
                    data=json.dumps(value).encode();self.send_response(200)
                except Exception as error:
                    data=json.dumps({'error':str(error)}).encode();self.send_response(400)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        server=HTTPServer(('127.0.0.1',8772),Handler)
        print('F103 SWD test transport ready on 8772',flush=True)
        try:server.serve_forever()
        finally:server.server_close()


if __name__=='__main__':main()
