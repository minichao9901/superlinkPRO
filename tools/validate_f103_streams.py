"""Real HID RTT forwarding / HSS capture; stores raw bytes for production decoding."""
import argparse,hashlib,json,re,struct,time,threading,math
from pathlib import Path
import hid,serial,usb.core,usb.util,libusb_package
from usb_bulk_reader import BulkReader
from board_acceptance_rtt import check_payload
def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('kind',choices=['rtt','scope','serial']);ap.add_argument('--out',type=Path,required=True);ap.add_argument('--address',type=lambda s:int(s,0),default=0);ap.add_argument('--speed',type=int,default=60000000);ap.add_argument('--period',type=float,default=2.5);ap.add_argument('--flags',type=lambda s:int(s,0),default=0xa1);ap.add_argument('--variables',type=int,default=1);ap.add_argument('--seconds',type=float,default=8);ap.add_argument('--warmup',type=float,default=.8);ap.add_argument('--pattern',choices=['seq','hello'],default='seq')
    ap.add_argument('--probe-serial', help='USB/HID 序列号，两条接口必须匹配')
    ap.add_argument('--com', help='RTT 转发串口；有探针序列号时按该序列号自动选择')
    ap.add_argument('--chunk', type=int, default=2048, help='单次 RTT 搬运字节数')
    ap.add_argument('--target', choices=['swd','arm-jtag','jtag'], default='swd')
    ap.add_argument('--variable-address', type=lambda value:int(value,0), action='append')
    ap.add_argument('--variable-type', choices=['u32', 'i32', 'f32'], action='append',
                    help='采样变量类型，可重复，顺序与地址一致；默认全部 u32')
    a=ap.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    if not 2<=a.period<=1e6 or not 1<=a.variables<=8 or a.seconds<=0 or not 64<=a.chunk<=4096:raise ValueError('Invalid capture plan')
    if a.kind=='serial':
        try:
            with serial.Serial('COM34',115200,timeout=.1,write_timeout=2) as s:
                s.reset_input_buffer();s.write(b'PING\n');end=time.monotonic()+2;raw=b''
                while time.monotonic()<end and b'PONG' not in raw:raw+=s.read(1024)
                result={'passed':b'PONG' in raw,'reply':raw.decode(errors='replace')}
        except Exception as error:result={'passed':False,'error':repr(error)}
        (a.out/'result.json').write_text(json.dumps(result,indent=2));print(result);return 0 if result['passed'] else 1
    addresses = a.variable_address or [a.address + index * 4 for index in range(a.variables)]
    if not 1 <= len(addresses) <= 8 or len(set(addresses)) != len(addresses):
        raise ValueError('需要 1 至 8 个不同的 DWORD 地址')
    types = a.variable_type or ['u32'] * len(addresses)
    if len(types) != len(addresses):
        raise ValueError('采样变量类型数与地址数不一致')
    a.variables = len(addresses)
    def matches_serial(value):
        return not a.probe_serial or (value or '').casefold() == a.probe_serial.casefold()
    devices=[d for d in hid.enumerate(0x0d28,0x0204) if d.get('usage_page')==0xff00 and matches_serial(d.get('serial_number'))];assert len(devices)==1
    h=hid.device();h.open_path(devices[0]['path']);h.set_nonblocking(1)
    def call(cmd,data):
        while h.read(64):pass
        req=bytes([1,1+len(data),cmd])+bytes(data);h.write(req+bytes(64-len(req)));end=time.monotonic()+3
        while time.monotonic()<end:
            r=bytes(h.read(64,timeout_ms=100))
            if len(r)>3 and r[0]==2 and r[2]==cmd:return r
        raise TimeoutError('HID response missing; command was not replayed')
    raw=bytearray();result={'kind':a.kind,'requestedClockHz':a.speed,'requestedPeriodUs':a.period,'flags':a.flags if a.kind=='scope' else None,'warmupSeconds':a.warmup,'pattern':a.pattern,'serial':devices[0].get('serial_number')};dev=None;port=None;started=False;reader=None;bulk_reader=None;reading=threading.Event();small_reads=threading.Event();tail_ready=threading.Event();read_errors=[]
    original_target = None
    original_config = None
    try:
        target = {'swd':0, 'jtag':1, 'arm-jtag':2}[a.target]
        original=call(0x31,[2])
        original_flags=struct.unpack_from('<I',original,4)[0]
        if original_flags & 1 or struct.unpack_from('<I',call(0x32,[2]),4)[0] & 0xc1:
            raise RuntimeError('探针仍有活动或未完成的流会话')
        original_target = (original_flags >> 17) & 3
        if target:
            config = call(1, [])[3:10]
            if len(config) != 7:
                raise RuntimeError('探针引脚配置不完整')
            if config[0] != 1:
                original_config = config
                updated = bytes([1]) + config[1:]
                call(2, updated)
                if call(1, [])[3:10] != updated:
                    raise RuntimeError('探针未确认 SWD + JTAG 引脚模式')
        reply=call(0x31,[10,target]);result['targetReply']=reply.hex();assert reply[3]==0,'target type: '+reply.hex()
        if a.kind=='rtt':
            com = a.com or 'COM43'
            if a.probe_serial:
                from serial.tools.list_ports import comports
                matching = [item.device for item in comports() if
                            (item.vid, item.pid) == (0x0d28, 0x0204) and matches_serial(item.serial_number)]
                if len(matching) != 1 or a.com and matching[0].casefold() != a.com.casefold():
                    raise RuntimeError('COM 与探针序列号没有唯一匹配')
                com = matching[0]
            result['com'] = com
            port=serial.Serial(com,115200,timeout=.05,write_timeout=2);port.reset_input_buffer()
            assert call(0x31,[7,*struct.pack('<IHB',0 if a.target=='jtag' else a.speed,a.chunk,0),255])[3]==0
        else:
            usb_devices = usb.core.find(find_all=True,idVendor=0x0d28,idProduct=0x0204,backend=libusb_package.get_libusb1_backend())
            matches = [candidate for candidate in usb_devices if matches_serial(candidate.serial_number)]
            if len(matches) != 1:
                raise RuntimeError('USB 序列号没有唯一匹配的探针')
            dev=matches[0];usb.util.claim_interface(dev,0)
            # Consume prior queued packets before CLEAR_FEATURE or CONFIG can
            # discard a native request whose completion frees a probe buffer.
            drained=0
            while drained<64:
                try:dev.read(0x83,512,timeout=30);drained+=1
                except usb.core.USBTimeoutError:break
            result['stalePacketsDrained']=drained
            if a.speed and a.target != 'jtag':
                reply=call(0x32,[3,*struct.pack('<I',a.speed)]);result['clockReply']=reply.hex();assert reply[3]==0 or reply[3]==156 and len(reply)>=52 and struct.unpack_from('<I',reply,8)[0]==a.speed,'scope clock: '+reply.hex()
            fractional=not a.period.is_integer();period=round(a.period*24) if fractional else int(a.period)
            cfg=bytes([10 if fractional else 7])+struct.pack('<IBB',period,a.flags,a.variables)
            scalar_code = {'u32': 4, 'i32': 5, 'f32': 6}
            for address, scalar in zip(addresses, types):
                cfg += struct.pack('<IBB', address, 4, scalar_code[scalar])
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
            values=dict(zip(['magic','tick','hz','produced','skipped','usb','errors','yields','tx','bytes','periodTicks','flags'],w))
            if r[1]>=54 and len(r)>=56:values['invalidSamples']=struct.unpack_from('<I',r,52)[0]
            return values
        def wait_scope_stopped():
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                response=call(0x32,[2]);flags=struct.unpack_from('<I',response,4)[0]
                if flags&0x81:raise RuntimeError('Scope STOP was aborted or still sampling')
                if not flags&0x40:return
                time.sleep(.01)
            raise TimeoutError('Scope STOP did not finish')
        if dev:
            wire=512/(496//(a.variables*4))*1e6/a.period
            fast=wire*.030>3*4096;depth=max(6,min(16,math.ceil(wire*.080/8192))) if fast else 3;chunk=8192 if fast else 4096
            bulk_reader=BulkReader(dev,raw,depth,chunk);result['readAhead']={'depth':depth,'bytes':chunk,'ordered':True,'backend':'libusb async; production configureReadAhead formula'}
        else:reading.set();reader=threading.Thread(target=receive);reader.start()
        # The request may reach the probe even if its reply is lost.
        started=True
        if port:
            reply=call(0x31,[1,*struct.pack('<IIB',a.address,0x1000,0)]);assert reply[3] in (0,156),reply.hex()
        else:
            reply=call(0x32,[1]);assert reply[3] in (0,156),reply.hex()
        time.sleep(a.warmup)
        if port:
            active_flags, live_address, *_ = struct.unpack_from('<12I',call(0x31,[2]),4)
            result['rttStart'] = {'running': bool(active_flags & 1), 'cbAddr': live_address,
                                  'channel': (active_flags >> 8) & 0xff, 'targetType': (active_flags >> 17) & 3}
            if not result['rttStart']['running'] or (a.address and live_address != a.address):
                raise RuntimeError('RTT 启动未确认所选控制块')
        before=metrics() if dev else None;first_bytes=len(raw);start=time.perf_counter()
        if dev:
            result['metricsBefore'] = before
            deadline = start + a.seconds
            while deadline - time.perf_counter() > 5:
                time.sleep(5)
                status = metrics()
                if not status['flags'] & 256 or bulk_reader.errors:
                    raise RuntimeError(f'原生 J-Scope 窗口中断：{bulk_reader.errors}')
            time.sleep(max(0, deadline - time.perf_counter()))
        else:
            time.sleep(a.seconds)
        seconds=time.perf_counter()-start;active_bytes=len(raw)-first_bytes
        if dev:
            after=metrics();delta=lambda k:(after[k]-before[k])&0xffffffff;dt=delta('tick')/24000000
            result.update(metricsBefore=before,metricsAfter=after,stableSeconds=dt,stable={**{k:delta(k) for k in ['produced','skipped','usb','errors','yields']},'seconds':dt,'Hz':delta('produced')/dt,'runningBefore':bool(before['flags']&256),'runningAfter':bool(after['flags']&256)})
        stop_tail=bool(dev and (struct.unpack_from('<I',call(0x32,[2]),4)[0]&0x20))
        if stop_tail:
            bulk_reader.begin_drain()
        elif bulk_reader:
            bulk_reader.finite_tail();time.sleep(.02)
        reply=call(0x31 if port else 0x32,[0,1] if stop_tail else [0]);result['stopReply']=reply.hex()
        if stop_tail:
            if reply[3] not in (0,156):raise RuntimeError('Scope rejected tail STOP')
            remaining=bulk_reader.wait_eof()
            wait_scope_stopped()
            if remaining:
                retired=call(0x32,[13,remaining])
                if retired[3]!=0:raise RuntimeError('Scope rejected pending read retirement')
            bulk_reader.wait_retired()
            wait_scope_stopped()
            result['tailDrain']={'retiredReads':remaining}
        if port:
            deadline=time.monotonic()+3
            words = struct.unpack_from('<12I',call(0x31,[2]),4)
            while words[0] & 1:
                if time.monotonic()>=deadline:raise TimeoutError('RTT STOP did not complete')
                time.sleep(.02)
                words = struct.unpack_from('<12I',call(0x31,[2]),4)
            moved_at_stop = words[3]
            deadline = time.monotonic() + 4
            while len(raw) < moved_at_stop and not read_errors and time.monotonic() < deadline:
                time.sleep(.02)
        started=False
        if not port: time.sleep(.3)
        if bulk_reader:bulk_reader.stop();read_errors.extend(bulk_reader.errors);bulk_reader=None
        if reader:reading.clear();reader.join(2);assert not reader.is_alive(),'Receiver did not stop'
        status=call(0x31 if port else 0x32,[2]);result['statusReply']=status.hex()
        if dev:
            result['finalMetrics']=metrics();result['confirmedFinalMetrics']=metrics()
        elif port:
            words = struct.unpack_from('<12I',status,4)
            result['rttStop'] = {'moved': words[3], 'readErrors': words[5] & 0xffff,
                                 'writeErrors': words[5] >> 16, 'tailBytes': len(raw) - words[3],
                                 'stableMoved': moved_at_stop == words[3],
                                 'running': bool(words[0] & 1), 'targetType': (words[0] >> 17) & 3,
                                 'cbAddr': words[1], 'chunkBytes': words[11] & 0xffff,
                                 'swdMhz': words[11] >> 24}
        result.update(bytes=len(raw),seconds=seconds,activeBytes=active_bytes,MBps=active_bytes/seconds/1e6,sha256=hashlib.sha256(raw).hexdigest(),receiveErrors=read_errors)
        if port:
            proof = check_payload(raw, a.pattern)
            if a.pattern == 'seq':
                records, gaps, previous = 0, 0, None
                for match in re.finditer(rb'seq (\d{7})\r\n', raw):
                    value = int(match[1])
                    if previous is not None and value != (previous + 1) % 10000000:
                        gaps += 1
                    records += 1
                    previous = value
                result.update(records=records, gaps=gaps, passed=records > 1000 and proof['exact'])
            else:
                pattern=b'hello world!\n';first=raw.find(pattern);payload=raw[first:];complete=len(payload)//len(pattern)*len(pattern);bad=sum(x!=pattern[i%len(pattern)] for i,x in enumerate(payload[:complete]));result.update(patternBadBytes=bad,passed=complete>13000 and bad==0)
            result['payload'] = proof
        else:result['passed']=len(raw)>512 and result['stable']['errors']==0 and result['stable']['runningBefore'] and result['stable']['runningAfter']
        if port:
            result['passed'] = (result['passed'] and result['rttStop']['stableMoved'] and
                                all(result['rttStop'][key] == 0 for key in ('readErrors','writeErrors','tailBytes')))
        result['passed']=result['passed'] and not read_errors
        (a.out/'capture.bin').write_bytes(raw)
    except BaseException as error:result['error']=repr(error)
    finally:
        if started:
            try:
                command = 0x31 if port else 0x32
                active = 1 if port else 0x41
                call(command,[0])
                deadline = time.monotonic() + 4
                while struct.unpack_from('<I',call(command,[2]),4)[0] & active:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('异常后 STOP 未进入空闲态')
                    time.sleep(.05)
                result['emergencyStopConfirmed'] = True
            except Exception as error:result['stopError']=repr(error)
        reading.clear()
        if reader:reader.join(2)
        if bulk_reader:bulk_reader.stop()
        if port:port.close()
        if dev is not None:
            try:usb.util.release_interface(dev,0)
            finally:usb.util.dispose_resources(dev)
        if original_target is not None:
            try:
                restored = call(0x31,[10,original_target])
                result['targetRestored'] = restored[3] == 0 and ((struct.unpack_from('<I',restored,4)[0] >> 17) & 3) == original_target
                if original_config is not None:
                    call(2, original_config)
                    result['targetRestored'] &= call(1, [])[3:10] == original_config
            except Exception as error:
                result['restoreError'] = repr(error)
                result['targetRestored'] = False
            result['passed'] = result.get('passed', False) and result['targetRestored']
        h.close();(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
    return 0 if result.get('passed') else 1
if __name__=='__main__':raise SystemExit(main())
