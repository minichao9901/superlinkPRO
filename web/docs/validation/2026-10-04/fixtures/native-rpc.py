import sys, json, base64, threading, concurrent.futures
import hid, usb.core, usb.util
sys.path.insert(0, 'E:/Share/github/akaLinkPro/script_test')
import scope_hss_test as h
h.hid = hid
ud = usb.core.find(idVendor=h.VID, idProduct=h.PID)
hd = h.open_hid()
hid_lock = threading.Lock()
out_lock = threading.Lock()
pool = concurrent.futures.ThreadPoolExecutor(max_workers=24)
def answer(req):
    global hd
    try:
        op = req['op']
        if op == 'meta':
            cfg = ud.get_active_configuration()
            grouped = {}
            for iface in cfg:
                grouped.setdefault(iface.bInterfaceNumber, []).append({'alternateSetting': iface.bAlternateSetting, 'interfaceClass': iface.bInterfaceClass,
                    'endpoints': [{'endpointNumber':ep.bEndpointAddress&127, 'direction':'in' if ep.bEndpointAddress&128 else 'out', 'packetSize':ep.wMaxPacketSize,'type':'bulk' if ep.bmAttributes&3==2 else 'interrupt'} for ep in iface]})
            result = {'serialNumber':ud.serial_number,'vendorId':ud.idVendor,'productId':ud.idProduct,'configuration':{'interfaces':[{'interfaceNumber':n,'alternates':alts} for n,alts in grouped.items()]}}
        elif op == 'hid':
            with hid_lock:
                hd.write([1]+req['data'])
                response = hd.read(64,timeout_ms=3000)
                if not response: raise TimeoutError('HID no reply')
                result = list(response[1:])
        elif op == 'hidClose':
            with hid_lock:
                hd.close(); hd = None; result = True
        elif op == 'hidOpen':
            with hid_lock:
                if hd is None: hd = h.open_hid()
                result = True
        elif op == 'claim': usb.util.claim_interface(ud, req['iface']); result = True
        elif op == 'release': usb.util.release_interface(ud, req['iface']); result = True
        elif op == 'clear': ud.clear_halt(req['ep']); result = True
        elif op == 'read': result = base64.b64encode(bytes(ud.read(req['ep'],req['size'],timeout=5000))).decode()
        elif op == 'write': result = ud.write(req['ep'],base64.b64decode(req['data']),timeout=5000)
        elif op == 'close': usb.util.dispose_resources(ud); result = True
        elif op == 'reset': ud.reset(); result = True
        else: raise ValueError(op)
        payload = {'id':req['id'],'result':result}
    except BaseException as e: payload = {'id':req['id'],'error':repr(e)}
    with out_lock: print(json.dumps(payload),flush=True)
for line in sys.stdin:
    pool.submit(answer,json.loads(line))
pool.shutdown(wait=True)
if hd is not None: hd.close()
usb.util.dispose_resources(ud)
