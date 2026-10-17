"""已授权的 SPI 真机验证：烧录本项目固件或恢复本次完整 Flash 备份。"""
import argparse
import hashlib
import json
import time
from pathlib import Path
from pyocd.core.helpers import ConnectHelper
from pyocd.flash.file_programmer import FileProgrammer
from pyocd.core.target import Target

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('profile',choices=('70','140','140-r8','280','280-r5','280-r6','280-r8','restore'))
args=parser.parse_args()
path=(ROOT/'docs/validation/2026-10-11-before-spi-flash-128k.bin' if args.profile=='restore'
      else ROOT/f'firmware/stm32h7b0_spi_trace/fw-{args.profile}.bin')
if args.profile=='70' and (ROOT/'firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.bin').exists():
    path=ROOT/'firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.bin'
data=path.read_bytes()
if not 0<len(data)<=131072:raise ValueError('Flash 长度超限')
print('Connecting to the trace target...',flush=True)
with ConnectHelper.session_with_chosen_probe(target_override='stm32h7b0xx',options={
    'frequency':1000000,'connect_mode':'attach','auto_unlock':False,'resume_on_disconnect':False}) as session:
    target=session.target
    print('Probe session open; checking target...',flush=True)
    target.halt()
    device=target.read32(0x5c001000)
    if device&4095!=0x480:raise ValueError('目标不匹配')
    # Avoid leaving an enabled trace FIFO across firmware/RCC clock changes.
    target.write32(0x5c001004,target.read32(0x5c001004)|0x700000)
    target.write32(0xe000edfc,target.read32(0xe000edfc)|0x1000000)
    for base in (0xe0041000,0x5c013000,0x5c014000,0x5c015000):target.write32(base+0xfb0,0xc5acce55)
    target.write32(0xe0041310,0)
    def wait_register(address,mask,value):
        until=time.monotonic()+1
        while target.read32(address)&mask!=value:
            if time.monotonic()>until:raise ValueError('CoreSight 未就绪，拒绝在未完成刷新时烧录')
            time.sleep(.01)
    target.write32(0xe0041004,0);wait_register(0xe004100c,1,1)
    if target.read32(0x5c014020)&1:
        target.write32(0x5c015304,3)
        target.write32(0x5c014304,target.read32(0x5c014304)|0x1040)
        wait_register(0x5c014304,0x40,0);wait_register(0x5c01400c,4,4)
    target.write32(0x5c014020,0);wait_register(0x5c01400c,4,4)
    if not target.read32(0x5c015300)&2:
        target.write32(0x5c015304,target.read32(0x5c015304)|0x1040)
        wait_register(0x5c015300,2,2)
    FileProgrammer(session,chip_erase='sector').program(str(path),base_address=0x08000000)
    target.reset_and_halt(reset_type=Target.ResetType.SYSRESETREQ)
    actual=bytes(target.read_memory_block8(0x08000000,len(data)))
    if actual!=data:raise ValueError('Flash 逐字节校验失败')
    report=dict(profile=args.profile,bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),verified=True)
    (ROOT/f'docs/validation/2026-10-11-spi-flash-{args.profile}.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)
    target.resume()
