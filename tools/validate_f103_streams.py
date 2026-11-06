"""Real HID RTT forwarding / HSS capture; stores raw bytes for production decoding."""
import argparse,hashlib,json,re,struct,time
from pathlib import Path
import hid,serial,usb.core,usb.util,libusb_package
def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('kind',choices=['rtt','scope','serial']);ap.add_argument('--out',type=Path,required=True);ap.add_argument('--address',type=lambda s:int(s,0),default=0);ap.add_argument('--speed',type=int,default=60000000);ap.add_argument('--period',type=int,default=20);ap.add_argument('--variables',type=int,default=1);ap.add_argument('--seconds',type=float,default=3);a=ap.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    if a.kind=='serial':
        with serial.Serial('COM34',115200,timeout=.1,write_timeout=2) as s:
            s.reset_input_buffer();s.write(b'PING\n');end=time.monotonic()+2;raw=b''
            while time.monotonic()<end and b'PONG' not in raw:raw+=s.read(1024)
            result={'passed':b'PONG' in raw,'reply':raw.decode(errors='replace')}
        (a.out/'result.json').write_text(json.dumps(result,indent=2));print(result);return 0 if result['passed'] else 1
    devices=[d for d in hid.enumerate(0x0d28,0x0204) if d.get('usage_page')==0xff00];assert len(devices)==1
    h=hid.device();h.open_path(devices[0]['path']);h.set_nonblocking(1)
    def call(cmd,data):
        while h.read(64):pass
        req=bytes([1,1+len(data),cmd])+bytes(data);h.write(req+bytes(64-len(req)));end=time.monotonic()+3
        while time.monotonic()<end:
            r=bytes(h.read(64,timeout_ms=100))
            if len(r)>3 and r[0]==2 and r[2]==cmd:return r
        raise TimeoutError('HID response missing; command was not replayed')
    raw=bytearray();result={'kind':a.kind,'requestedClockHz':a.speed};dev=None;port=None;started=False
    try:
        reply=call(0x31,[10,0]);result['targetReply']=reply.hex();assert reply[3]==0,'target type: '+reply.hex()
        if a.kind=='rtt':
            port=serial.Serial('COM43',115200,timeout=.05,write_timeout=2);port.reset_input_buffer()
            assert call(0x31,[7,*struct.pack('<IHB',a.speed,2048,0),255])[3]==0
            reply=call(0x31,[1,*struct.pack('<IIB',a.address,0,0)]);assert reply[3]==0,reply.hex();started=True
        else:
            dev=usb.core.find(idVendor=0x0d28,idProduct=0x0204,backend=libusb_package.get_libusb1_backend());usb.util.claim_interface(dev,0)
            reply=call(0x32,[3,*struct.pack('<I',a.speed)]);result['clockReply']=reply.hex();assert reply[3]==0 or reply[3]==156 and len(reply)>=52 and struct.unpack_from('<I',reply,8)[0]==a.speed,'scope clock: '+reply.hex()
            cfg=bytes([7])+struct.pack('<IBB',a.period,1,a.variables)
            for i in range(a.variables):cfg+=struct.pack('<IBB',a.address+i*4,4,4)
            reply=call(0x32,cfg);result['configReply']=reply.hex();assert reply[3] in (0,156),reply.hex()
            reply=call(0x32,[1]);assert reply[3] in (0,156),reply.hex();started=True
        start=time.perf_counter();end=start+a.seconds
        while time.perf_counter()<end:
            if port:raw.extend(port.read(65536))
            else:
                # Large reads while running; exact packets for the finite tail.
                try:raw.extend(dev.read(0x83,32768 if a.period<=100 else 512,timeout=200))
                except usb.core.USBTimeoutError:pass
        seconds=time.perf_counter()-start
        reply=call(0x31 if port else 0x32,[0]);started=False;result['stopReply']=reply.hex()
        tail=time.monotonic()+.4
        while time.monotonic()<tail:
            if port:
                b=port.read(65536)
                if not b:break
                raw.extend(b)
            else:
                try:raw.extend(dev.read(0x83,512,timeout=30))
                except usb.core.USBTimeoutError:break
        status=call(0x31 if port else 0x32,[2]);result['statusReply']=status.hex()
        result.update(bytes=len(raw),seconds=seconds,MBps=len(raw)/seconds/1e6,sha256=hashlib.sha256(raw).hexdigest())
        if port:
            records=[int(v) for v in re.findall(rb'seq (\d{7})\r\n',raw)];gaps=sum(b!=(a+1)%10000000 for a,b in zip(records,records[1:]));result.update(records=len(records),gaps=gaps,passed=len(records)>1000 and gaps==0)
        else:result['passed']=len(raw)>512
        (a.out/'capture.bin').write_bytes(raw)
    except BaseException as error:result['error']=repr(error)
    finally:
        if started:
            try:call(0x31 if port else 0x32,[0])
            except Exception as error:result['stopError']=repr(error)
        if port:port.close()
        if dev is not None:
            try:usb.util.release_interface(dev,0)
            finally:usb.util.dispose_resources(dev)
        h.close();(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
    return 0 if result.get('passed') else 1
if __name__=='__main__':raise SystemExit(main())
