"""Bounded, serial HID control for the native F103 performance harness."""
import json,sys,time
import hid

devices=[d for d in hid.enumerate(0x0d28,0x0204) if d.get('usage_page')==0xff00]
if len(devices)!=1:raise RuntimeError('Unique akaLinkPro HID required')
h=hid.device();h.open_path(devices[0]['path']);h.set_nonblocking(1)
try:
    for line in sys.stdin:
        request=json.loads(line)
        try:
            cmd=request['cmd'];data=bytes(request['data'])
            if cmd not in (0x31,0x32) or len(data)>61:raise ValueError('Unsupported diagnostic control')
            while h.read(64):pass
            packet=bytes([1,1+len(data),cmd])+data;h.write(packet+bytes(64-len(packet)))
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                response=bytes(h.read(64,timeout_ms=100))
                if len(response)>3 and response[0]==2 and response[2]==cmd:break
            else:raise TimeoutError('HID response missing; command not replayed')
            result={'id':request['id'],'bytes':list(response)}
        except Exception as error:result={'id':request['id'],'error':str(error)}
        print(json.dumps(result),flush=True)
finally:h.close()
