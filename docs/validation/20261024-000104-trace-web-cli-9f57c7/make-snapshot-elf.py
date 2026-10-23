from pathlib import Path
import struct,json,hashlib
root=Path('C:/Users/Administrator/.codex/validation/20261024-000104-trace-web-cli-9f57c7')
raw=(root/'original-flash-1.bin').read_bytes().rstrip(b'\xff');raw+=b'\xff'*((-len(raw))%4)
strings=b'\x00observed_memfill\x00observed_reset\x00'
syms=bytes(16)+struct.pack('<IIIBBH',1,0x0800b44d,16,0x12,0,1)+struct.pack('<IIIBBH',18,0x0800b335,128,0x12,0,1)
names=b'\x00.text\x00.symtab\x00.strtab\x00.shstrtab\x00'
body=bytearray(256);body.extend(raw)
def add(data):
 off=len(body);body.extend(data);body.extend(b'\x00'*((-len(body))%4));return off
symoff=add(syms);stroff=add(strings);nameoff=add(names);shoff=len(body)
body.extend(bytes(40))
for args in [(1,1,6,0x08000000,256,len(raw),0,0,4,0),(7,2,0,0,symoff,len(syms),3,1,4,16),(15,3,0,0,stroff,len(strings),0,0,1,0),(23,3,0,0,nameoff,len(names),0,0,1,0)]:body.extend(struct.pack('<IIIIIIIIII',*args))
ident=b'\x7fELF\x01\x01\x01'+bytes(9)
body[:52]=struct.pack('<16sHHIIIIIHHHHHH',ident,2,40,1,0x0800b335,52,shoff,0x05000000,52,32,1,40,5,4)
body[52:84]=struct.pack('<IIIIIIII',1,256,0x08000000,0x08000000,len(raw),len(raw),5,4)
p=root/'observed-flash-snapshot.elf'
with p.open('xb') as f:f.write(body)
report=dict(path=str(p),flashBytes=len(raw),sha256=hashlib.sha256(body).hexdigest(),originalDebugSymbols=False,note='从两次一致的Flash回读构造ELF容器，只标记观察到的指令地址，不冒充原工程ELF/DWARF。未写入板子Flash。')
(Path('E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7')/'snapshot-elf.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(report,ensure_ascii=False))
