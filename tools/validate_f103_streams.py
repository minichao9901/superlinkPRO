"""Real HID RTT forwarding / HSS capture; stores raw bytes for production decoding."""
import argparse,hashlib,json,re,struct,time,threading,math
from pathlib import Path
import hid,serial,usb.core,usb.util,libusb_package
from usb_bulk_reader import BulkReader
def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('kind',choices=['rtt','scope','serial']);ap.add_argument('--out',type=Path,required=True);ap.add_argument('--address',type=lambda s:int(s,0),default=0);ap.add_argument('--speed',type=int,default=60000000);ap.add_argument('--period',type=float,default=2.5);ap.add_argument('--flags',type=lambda s:int(s,0),default=0xa1);ap.add_argument('--variables',type=int,default=1);ap.add_argument('--seconds',type=float,default=8);ap.add_argument('--warmup',type=float,default=.8);ap.add_argument('--pattern',choices=['seq','hello'],default='seq');a=ap.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    if not 2<=a.period<=1e6 or not 1<=a.variables<=8 or a.seconds<=0:raise ValueError('Invalid capture plan')
    if a.kind=='serial':
        try:
            with serial.Serial('COM34',115200,timeout=.1,write_timeout=2) as s:
                s.reset_input_buffer();s.write(b'PING\n');end=time.monotonic()+2;raw=b''
                while time.monotonic()<end and b'PONG' not in raw:raw+=s.read(1024)
                result={'passed':b'PONG' in raw,'reply':raw.decode(errors='replace')}
        except Exception as error:result={'passed':False,'error':repr(error)}
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
    raw=bytearray();result={'kind':a.kind,'requestedClockHz':a.speed,'requestedPeriodUs':a.period,'flags':a.flags if a.kind=='scope' else None,'warmupSeconds':a.warmup,'pattern':a.pattern};dev=None;port=None;started=False;reader=None;bulk_reader=None;reading=threading.Event();small_reads=threading.Event();tail_ready=threading.Event();read_errors=[]
    try:
        reply=call(0x31,[10,0]);result['targetReply']=reply.hex();assert reply[3]==0,'target type: '+reply.hex()
        if a.kind=='rtt':
            port=serial.Serial('COM43',115200,timeout=.05,write_timeout=2);port.reset_input_buffer()
            assert call(0x31,[7,*struct.pack('<IHB',a.speed,2048,0),255])[3]==0
            reply=call(0x31,[1,*struct.pack('<IIB',a.address,0,0)]);assert reply[3]==0,reply.hex();started=True
        else:
            dev=usb.core.find(idVendor=0x0d28,idProduct=0x0204,backend=libusb_package.get_libusb1_backend());usb.util.claim_interface(dev,0)
            # Consume prior queued packets before CLEAR_FEATURE or CONFIG can
            # discard a native request whose completion frees a probe buffer.
            drained=0
            while drained<64:
                try:dev.read(0x83,512,timeout=30);drained+=1
                except usb.core.USBTimeoutError:break
            result['stalePacketsDrained']=drained
            reply=call(0x32,[3,*struct.pack('<I',a.speed)]);result['clockReply']=reply.hex();assert reply[3]==0 or reply[3]==156 and len(reply)>=52 and struct.unpack_from('<I',reply,8)[0]==a.speed,'scope clock: '+reply.hex()
            fractional=not a.period.is_integer();period=round(a.period*24) if fractional else int(a.period)
            cfg=bytes([10 if fractional else 7])+struct.pack('<IBB',period,a.flags,a.variables)
            for i in range(a.variables):cfg+=struct.pack('<IBB',a.address+i*4,4,4)
            reply=call(0x32,cfg);result['configReply']=reply.hex();assert reply[3] in (0,156),reply.hex()
        def receive():
            while reading.is_set():
                try:
                    if port:raw.extend(port.read(16384))
                    else:
                        size=512 if small_reads.is_set() else 8192
                        if size==512:tail_ready.set()
                        raw.extend(dev.read(0x83,size,timeout=100))
                except usb.core.USBTimeoutError:pass
                except Exception as error:read_errors.append(repr(error));break
        def metrics():
            r=call(0x32,[11]);assert len(r)>=52,'Short metrics'
            w=struct.unpack_from('<12I',r,4);assert w[0]==0x31535348 and w[2]==24000000,'Invalid metrics'
            return dict(zip(['magic','tick','hz','produced','skipped','usb','errors','yields','tx','bytes','periodTicks','flags'],w))
        if dev:
            wire=512/(496//(a.variables*4))*1e6/a.period
            fast=wire*.030>3*4096;depth=max(6,min(16,math.ceil(wire*.080/8192))) if fast else 3;chunk=8192 if fast else 4096
            bulk_reader=BulkReader(dev,raw,depth,chunk);result['readAhead']={'depth':depth,'bytes':chunk,'ordered':True,'backend':'libusb async; production configureReadAhead formula'}
        else:reading.set();reader=threading.Thread(target=receive);reader.start()
        if not port:
            reply=call(0x32,[1]);assert reply[3] in (0,156),reply.hex();started=True
        time.sleep(a.warmup)
        before=metrics() if dev else None;first_bytes=len(raw);start=time.perf_counter();time.sleep(a.seconds);seconds=time.perf_counter()-start;active_bytes=len(raw)-first_bytes
        if dev:
            after=metrics();delta=lambda k:(after[k]-before[k])&0xffffffff;dt=delta('tick')/24000000
            result.update(metricsBefore=before,metricsAfter=after,stable={**{k:delta(k) for k in ['produced','skipped','usb','errors','yields']},'seconds':dt,'Hz':delta('produced')/dt,'runningBefore':bool(before['flags']&256),'runningAfter':bool(after['flags']&256)})
            bulk_reader.finite_tail();time.sleep(.02)
        reply=call(0x31 if port else 0x32,[0]);started=False;result['stopReply']=reply.hex()
        time.sleep(.3)
        if bulk_reader:bulk_reader.stop();read_errors.extend(bulk_reader.errors);bulk_reader=None
        if reader:reading.clear();reader.join(2);assert not reader.is_alive(),'Receiver did not stop'
        status=call(0x31 if port else 0x32,[2]);result['statusReply']=status.hex()
        result.update(bytes=len(raw),seconds=seconds,activeBytes=active_bytes,MBps=active_bytes/seconds/1e6,sha256=hashlib.sha256(raw).hexdigest(),receiveErrors=read_errors)
        if port:
            records=[int(v) for v in re.findall(rb'seq (\d{7})\r\n',raw)];gaps=sum(b!=(a+1)%10000000 for a,b in zip(records,records[1:]));result.update(records=len(records),gaps=gaps,passed=len(records)>1000 and gaps==0)
            if a.pattern=='hello':
                pattern=b'hello world!\n';first=raw.find(pattern);payload=raw[first:];complete=len(payload)//len(pattern)*len(pattern);bad=sum(x!=pattern[i%len(pattern)] for i,x in enumerate(payload[:complete]));result.update(patternBadBytes=bad,passed=complete>13000 and bad==0)
        else:result['passed']=len(raw)>512 and result['stable']['errors']==0 and result['stable']['runningBefore'] and result['stable']['runningAfter']
        result['passed']=result['passed'] and not read_errors
        (a.out/'capture.bin').write_bytes(raw)
    except BaseException as error:result['error']=repr(error)
    finally:
        if started:
            try:call(0x31 if port else 0x32,[0])
            except Exception as error:result['stopError']=repr(error)
        reading.clear()
        if reader:reader.join(2)
        if bulk_reader:bulk_reader.stop()
        if port:port.close()
        if dev is not None:
            try:usb.util.release_interface(dev,0)
            finally:usb.util.dispose_resources(dev)
        h.close();(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
    return 0 if result.get('passed') else 1
if __name__=='__main__':raise SystemExit(main())
