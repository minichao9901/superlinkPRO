"""Source context and compile-time annotations from the recording's own ELF.

No preprocessor subprocess, host eval, guessed header pairing or runtime values.
Unsupported/ambiguous definitions stay unresolved. This does not rebind DWARF.
"""
import bisect
from collections import defaultdict
from dataclasses import dataclass
import io
import re
import struct
from pathlib import Path, PurePosixPath

TOKEN=re.compile(r'//[^\n]*|/\*.*?\*/|(?:u8|[LuU])?"(?:\\.|[^"\\])*"|(?:[LuU])?\'(?:\\.|[^\'\\])*\'|(?:[A-Za-z_]\w*)|(?:0[xX][0-9a-fA-F]+|\d+)[uUlL]*|>>=|<<=|##|->|\+\+|--|&&|\|\||==|!=|<=|>=|<<|>>|\S',re.S)
IDENT=re.compile(r'^[A-Za-z_]\w*$')


def tokens(text):
    return [m for m in TOKEN.finditer(text) if not m.group().startswith(('/*','//'))]


def inline_constants(text,values):
    """Annotate whole identifier tokens only; preserve comments and literals."""
    parts=[];cursor=0
    for token in tokens(text):
        value=values.get(token.group())
        if value is None:continue
        parts.extend((text[cursor:token.end()],'/**'+value+'*/'));cursor=token.end()
    return ''.join(parts)+text[cursor:]


def statements(text):
    """Bounded lexical C statement contexts, with explicit ambiguous-line fallback."""
    items=tokens(text);spans=[];begin=0;paren=bracket=initializer=0
    def end(i):
        nonlocal begin
        if begin>i:return
        a,b=items[begin].start(),items[i].end();first=text.count('\n',0,a)+1;last=text.count('\n',0,b)+1
        prefix=text[text.rfind('\n',0,a)+1:a]
        spans.append((first,last,(prefix if not prefix.strip() else '')+text[a:b],a,b));begin=i+1
    for i,t in enumerate(items):
        word=t.group()
        # Preprocessor directives are not C statements or evidence of execution.
        if word=='#' and not text[text.rfind('\n',0,t.start())+1:t.start()].strip():
            if begin<i:end(i-1)
            endline=text.find('\n',t.end())
            while endline>=0 and text[:endline].endswith('\\'):endline=text.find('\n',endline+1)
            while i+1<len(items) and (endline<0 or items[i+1].start()<endline):
                i+=1
            begin=max(begin,i+1)
            continue
        if i<begin:continue
        if word=='(':paren+=1
        elif word==')':
            paren=max(0,paren-1)
            if not paren and not initializer and items[begin].group() in ('if','for','while','switch'):
                end(i)
        elif word=='[':bracket+=1
        elif word==']':bracket=max(0,bracket-1)
        elif word=='{' and not paren:
            assignment=any(x.group()=='=' for x in items[begin:i])
            if initializer or assignment:initializer+=1
            else:end(i)
        elif word=='}' and not paren:
            if initializer:initializer-=1
            else:end(i)
        elif word==';' and not paren and not bracket and not initializer:end(i)
        elif word==':' and not paren and not initializer and (items[begin].group() in ('case','default') or i==begin+1):end(i)
    if begin<len(items):end(len(items)-1)
    byline=defaultdict(list)
    for s in spans:
        if s[1]-s[0]>200:continue
        for line in range(s[0],s[1]+1):byline[line].append(s)
    return byline


@dataclass(frozen=True)
class Integer:
    value:int
    bits:int=32
    unsigned:bool=False


def integer(value,bits=32,unsigned=False,wrap=False):
    if unsigned:return Integer(value&((1<<bits)-1),bits,True)
    if wrap:
        value&=(1<<bits)-1
        if value&(1<<(bits-1)):value-=1<<bits
    if not -(1<<(bits-1))<=value<(1<<(bits-1)):raise ValueError('目标C有符号溢出')
    return Integer(value,bits,False)


class CExpression:
    precedence={'||':1,'&&':2,'|':3,'^':4,'&':5,'==':6,'!=':6,'<':7,'<=':7,'>':7,'>=':7,'<<':8,'>>':8,'+':9,'-':9,'*':10,'/':10,'%':10}
    casts={'uint8_t':(8,True),'uint16_t':(16,True),'uint32_t':(32,True),'uint64_t':(64,True),
           'int8_t':(8,False),'int16_t':(16,False),'int32_t':(32,False),'int64_t':(64,False),
           'unsigned':(32,True),'int':(32,False),'long':(32,False)}
    def __init__(self,text,resolve):self.items=[x.group() for x in tokens(text)];self.pos=0;self.resolve=resolve
    def take(self):
        if self.pos>=len(self.items):raise ValueError('不完整常量表达式')
        value=self.items[self.pos];self.pos+=1;return value
    def peek(self):return self.items[self.pos] if self.pos<len(self.items) else ''
    @staticmethod
    def promote(v):return integer(v.value) if v.bits<32 else v
    def unary(self):
        t=self.take()
        if t in ('+','-','~','!'):
            v=self.promote(self.unary())
            if t=='!':return integer(int(not v.value))
            return integer(v.value if t=='+' else -v.value if t=='-' else ~v.value,v.bits,v.unsigned,wrap=t=='~')
        if t=='(':
            if self.peek() in self.casts and self.pos+1<len(self.items) and self.items[self.pos+1]==')':
                bits,unsigned=self.casts[self.take()];self.take();v=self.unary()
                if not unsigned and not -(1<<(bits-1))<=v.value<(1<<(bits-1)):raise ValueError('实现定义的有符号转换')
                return integer(v.value,bits,unsigned)
            v=self.expression()
            if self.take()!=')':raise ValueError('常量括号不匹配')
            return v
        if re.fullmatch(r'(?:0[xX][\da-fA-F]+|\d+)[uUlL]*',t):
            base=re.sub('[uUlL]+$','',t);suffix=t[len(base):].lower()
            if suffix not in ('','u','l','ul','lu','ll','ull','llu'):raise ValueError('无效C整数后缀')
            value=int(base,16 if base.lower().startswith('0x') else 8 if len(base)>1 and base[0]=='0' else 10)
            bits=64 if suffix.count('l')==2 else 32;unsigned='u' in suffix
            if not unsigned and value>=(1<<(bits-1)):
                if base.startswith(('0x','0X','0')) and value<(1<<bits):unsigned=True
                elif bits==32:bits=64
            return integer(value,bits,unsigned)
        if IDENT.match(t):return self.resolve(t)
        raise ValueError('不支持的常量表达式')
    def expression(self,minimum=1):
        left=self.unary()
        while self.precedence.get(self.peek(),0)>=minimum:
            op=self.take();right=self.expression(self.precedence[op]+1);a=self.promote(left);b=self.promote(right)
            bits=max(a.bits,b.bits);unsigned=(a.unsigned if a.bits>b.bits else b.unsigned if b.bits>a.bits else a.unsigned or b.unsigned)
            x=integer(a.value,bits,unsigned).value;y=integer(b.value,bits,unsigned).value
            if op in ('<<','>>'):
                x=a.value;bits=a.bits;unsigned=a.unsigned;y=b.value
                if not 0<=y<bits or x<0:raise ValueError('未定义或实现定义的移位')
                value=x<<y if op=='<<' else x>>y
            elif op in ('/','%'):
                if not y:raise ValueError('常量除零')
                q=(abs(x)//abs(y))*(-1 if (x<0)!=(y<0) else 1);value=q if op=='/' else x-q*y
            elif op in ('&&','||','==','!=','<','<=','>','>='):
                value={'&&':bool(x) and bool(y),'||':bool(x) or bool(y),'==':x==y,'!=':x!=y,'<':x<y,'<=':x<=y,'>':x>y,'>=':x>=y}[op];left=integer(int(value));continue
            elif op=='+':value=x+y
            elif op=='-':value=x-y
            elif op=='*':value=x*y
            elif op=='&':value=x&y
            elif op=='|':value=x|y
            else:value=x^y
            left=integer(value,bits,unsigned,wrap=op in ('&','|','^'))
        return left
    def value(self):
        if len(self.items)>1024:raise ValueError('常量表达式过长')
        v=self.expression()
        if self.pos!=len(self.items):raise ValueError('不支持的常量语法')
        return v


def file_table(cu,program):
    decode=lambda x:x.decode('utf8','replace') if isinstance(x,bytes) else str(x)
    directory=decode(cu.get_top_DIE().attributes.get('DW_AT_comp_dir').value) if cu.get_top_DIE().attributes.get('DW_AT_comp_dir') else ''
    files={}
    for i,e in enumerate(program['file_entry'],1):
        name=decode(e.name);parent=decode(program['include_directory'][e.dir_index-1]) if e.dir_index else directory
        if e.dir_index and not Path(parent).is_absolute():parent=str(Path(directory)/parent)
        files[i]=(Path(parent)/name).as_posix()
    return files


class MacroData:
    def __init__(self,path,queries):
        from elftools.elf.elffile import ELFFile
        self.elf=ELFFile(io.BytesIO(Path(path).read_bytes()));self.dwarf=self.elf.get_dwarf_info()
        self.section=self.elf.get_section_by_name('.debug_macro');strings=self.elf.get_section_by_name('.debug_str');self.strings=strings.data() if strings else b''
        self.raw=self.section.data() if self.section else b'';self.results={};self.enums={};self.enumAmbiguity={};self.intervals=[];self.issues=[];self.cus={};self.units={};self.expandedUnits={};self.needed=set();self.lineFiles={}
        for cu in self.dwarf.iter_CUs():
            offset=cu.cu_offset;top=cu.get_top_DIE();program=self.dwarf.line_program_for_CU(cu)
            if not program:continue
            files=file_table(cu,program);self.cus[offset]=(cu,files)
            stmt=top.attributes.get('DW_AT_stmt_list')
            if stmt:self.lineFiles[stmt.value]=files
            previous=None
            for row in program.get_entries():
                state=row.state
                if state is None:continue
                if previous is not None and previous.address<state.address:self.intervals.append((previous.address,state.address,offset))
                previous=None if state.end_sequence else state
        self.intervals.sort();self.starts=[r[0] for r in self.intervals];self.maxEnds=[];high=0
        for _,end,_ in self.intervals:high=max(high,end);self.maxEnds.append(high)
        bycu=defaultdict(dict)
        for pc,file,line,names in queries:
            cu=self.unit(pc)
            if cu is not None:bycu[cu].setdefault((file.replace('\\','/'),line),set()).update(names)
        roots={}
        for offset,wanted in bycu.items():
            cu,files=self.cus[offset];attrs=cu.get_top_DIE().attributes;attr=attrs.get('DW_AT_macros') or attrs.get('DW_AT_GNU_macros')
            values=defaultdict(set);variables=set()
            for die in cu.iter_DIEs():
                if die.tag in ('DW_TAG_variable','DW_TAG_formal_parameter') and 'DW_AT_name' in die.attributes:
                    variables.add(die.attributes['DW_AT_name'].value.decode('utf8','replace'))
                if die.tag=='DW_TAG_enumerator' and 'DW_AT_name'in die.attributes and 'DW_AT_const_value'in die.attributes:
                    name=die.attributes['DW_AT_name'].value.decode('utf8','replace');values[name].add(die.attributes['DW_AT_const_value'].value)
            self.enums[offset]={k:next(iter(v)) for k,v in values.items() if len(v)==1 and k not in variables}
            self.enumAmbiguity[offset]={k for k,v in values.items() if len(v)!=1 or k in variables}
            if not attr or not self.raw:continue
            roots[offset]=attr.value
            self.needed.update(set().union(*wanted.values()))
        # Shared imported macro units may have enormous expanded multiplicity.
        # Parse each physical unit once and keep only the transitive dependency
        # set referenced by the matched source. File/line transitions still replay.
        todo=list(roots.values());seen=set();definitions=defaultdict(set)
        while todo:
            offset=todo.pop()
            if offset in seen:continue
            seen.add(offset)
            try:
                for op,line,text in self.parse_unit(offset):
                    if op=='import':todo.append(text)
                    elif op=='define':
                        name=re.match(r'(\w+)',text)
                        if name:definitions[name[1]].add(text[len(name[1]):])
            except (ValueError,IndexError,struct.error,UnicodeError) as e:self.issues.append(str(e))
        todo=list(self.needed)
        while todo:
            bodies=definitions.get(todo.pop(),())
            for dependency in {t.group() for body in bodies for t in tokens(body) if IDENT.match(t.group())}:
                if dependency not in self.needed:self.needed.add(dependency);todo.append(dependency)
        self.knownNames=set(definitions)
        for offset,macroOffset in roots.items():
            try:self.replay(offset,macroOffset,self.cus[offset][1],bycu[offset])
            except (ValueError,IndexError,struct.error,UnicodeError) as e:self.issues.append(str(e))

    def unit(self,pc):
        i=bisect.bisect_right(self.starts,pc)-1;found=None
        while i>=0 and self.maxEnds[i]>pc:
            if self.intervals[i][0]<=pc<self.intervals[i][1]:
                cu=self.intervals[i][2]
                if found is not None and found!=cu:return None
                found=cu
            i-=1
        return found

    def parse_unit(self,offset):
        if offset in self.units:return self.units[offset]
        raw=self.raw;pos=offset
        def fixed(n):
            nonlocal pos
            if pos+n>len(raw):raise ValueError('截断的.debug_macro')
            v=int.from_bytes(raw[pos:pos+n],'little');pos+=n;return v
        def uleb():
            v=shift=0
            while True:
                b=fixed(1);v|=(b&127)<<shift
                if b<128:return v
                shift+=7
                if shift>63:raise ValueError('宏ULEB溢出')
        def string(blob,at):
            end=blob.find(b'\0',at)
            if at<0 or end<0:raise ValueError('宏字符串越界')
            return blob[at:end].decode('utf8'),end+1
        version=fixed(2);flags=fixed(1);size=8 if flags&1 else 4
        if version not in (4,5) or flags&~7:raise ValueError('不支持的宏调试头')
        localFiles=self.lineFiles.get(fixed(size)) if flags&2 else None
        if flags&4:raise ValueError('不支持自定义宏操作表')
        items=[]
        while True:
            op=fixed(1)
            if op==0:break
            if op in (1,2,5,6):
                line=uleb()
                if op in (1,2):text,pos=string(raw,pos)
                else:text,_=string(self.strings,fixed(size))
                items.append(('define' if op in (1,5) else 'undef',line,text))
            elif op==3:
                line=uleb();file=uleb()
                if not localFiles or file not in localFiles:raise ValueError('宏单元缺少匹配行表文件')
                items.append(('enter',line,localFiles[file]))
            elif op==4:items.append(('leave',0,''))
            elif op==7:items.append(('import',0,fixed(size)))
            else:raise ValueError('不支持的宏调试操作 '+str(op))
            if len(items)>500000:raise ValueError('单宏单元超过50万条预算')
        self.units[offset]=tuple(items)
        return self.units[offset]

    def flatten(self,offset,active=()):
        if offset in active or len(active)>=64:raise ValueError('宏导入循环或深度超过64')
        if offset in self.expandedUnits:return self.expandedUnits[offset]
        items=[]
        for op,line,text in self.parse_unit(offset):
            if op=='import':items.extend(self.flatten(text,active+(offset,)))
            elif op in ('define','undef'):
                name=re.match(r'(\w+)',text)
                if name and name[1] in self.needed:items.append((op,line,text))
            else:items.append((op,line,text))
            if len(items)>500000:raise ValueError('相关宏单元超过50万条预算')
        self.expandedUnits[offset]=tuple(items);return self.expandedUnits[offset]

    def directives(self,offset,files):
        yield from self.flatten(offset)

    def replay(self,cu,offset,files,wanted):
        env={};stack=[];byfile=defaultdict(list);answers=defaultdict(list)
        for key,names in wanted.items():byfile[key[0]].append((key[1],names))
        for file in byfile:byfile[file].sort()
        def flush(until):
            if not stack:return
            frame=stack[-1];items=byfile[frame[0]]
            while frame[1]<len(items) and items[frame[1]][0]<until:
                line,names=items[frame[1]];frame[1]+=1
                snapshot={};todo=list(names)
                while todo:
                    name=todo.pop()
                    if name in snapshot:continue
                    if len(snapshot)>512:raise ValueError('宏依赖超过512项')
                    definition=env.get(name);snapshot[name]=definition
                    if definition:
                        for match in tokens(definition[0]):
                            dependency=match.group()
                            if IDENT.match(dependency) and dependency not in snapshot:todo.append(dependency)
                answers[(frame[0],line)].append(snapshot)
        for op,line,text in self.directives(offset,files):
            if op=='enter':flush(line);stack.append([text,0]);continue
            if op=='leave':flush(float('inf'));stack.pop() if stack else None;continue
            flush(line)
            name=re.match(r'([A-Za-z_]\w*)',text)
            if not name:continue
            name=name[1]
            if op=='undef':env.pop(name,None)
            else:env[name]=(text,stack[-1][0] if stack else '<编译参数>',line)
        while stack:flush(float('inf'));stack.pop()
        # Resolve dependencies only when every inclusion occurrence agrees.
        for (file,line),values in answers.items():
            resolved={}
            for name in set().union(*(v.keys() for v in values)):
                choices={v.get(name) for v in values}
                if len(choices)==1 and next(iter(choices)) is not None:resolved[name]=next(iter(choices))
            self.results[(cu,file,line)]=resolved


class SourceReading:
    def __init__(self,sources,elf,config,entries=None):
        self.sources=sources;self.lines={k:t.splitlines() for k,t in sources.items()};self.spans={k:statements(t) for k,t in sources.items()} if config['statements'] else {}
        self.config=config;self.cache={};self.macro=None;self.annotationCount=0;self.unresolved=0
        if config['macroView']!='original':
            # Build queries from the actual DWARF locations, never basename-match.
            queries=[]
            if entries is None:
                from decode_trace import Image
                locations=[(pc,file,line) for pc,(_,file,line) in Image(elf).lines.items()]
            else:locations=[(e.pc,e.file,e.line) for e in entries]
            for pc,file,line in locations:
                if file in sources and line:
                    description=self.context(file,line)
                    names={m.group() for m in tokens(description['text']) if IDENT.match(m.group())}
                    queries.append((pc,file,line,names))
            try:self.macro=MacroData(elf,queries)
            except (ValueError,KeyError,AttributeError) as e:self.macroError=str(e)

    def context(self,file,line):
        content=self.lines.get(file,[])
        if not line or not 0<line<=len(content):return dict(text='',firstLine=line,lastLine=line,mappingGranularity='missing')
        spans=self.spans.get(file,{}).get(line,[])
        if len(spans)==1 and not spans[0][2].lstrip().startswith('#'):
            first,last,text,_,_=spans[0]
            return dict(text=text,firstLine=first,lastLine=last,mappingGranularity='statement-context')
        return dict(text=content[line-1],firstLine=line,lastLine=line,mappingGranularity='line; ambiguous or unsupported statement')

    @staticmethod
    def definition(record):
        text,file,line=record;m=re.match(r'(\w+)(\(([^)]*)\))?(?:\s+(.*))?$',text,re.S)
        if not m:raise ValueError('宏定义未解析')
        args=None if m[2] is None else [p.strip() for p in m[3].split(',')] if m[3] else []
        return args,m[4] or '',file,line

    def expand(self,text,scope,disabled=()):
        if len(disabled)>16:raise ValueError('嵌套宏超过16层')
        parts=[t.group() for t in tokens(text)];out=[];i=0
        while i<len(parts):
            name=parts[i];record=scope.get(name)
            if not record:out.append(name);i+=1;continue
            if name in disabled:raise ValueError('递归宏暂不展开')
            args,body,_,_=self.definition(record)
            if any(t.group() in ('#','##') for t in tokens(body)) or args and any('...' in x for x in args):raise ValueError('字符串化/拼接/可变参数宏暂未展开')
            if args is not None:
                if i+1>=len(parts) or parts[i+1]!='(':out.append(name);i+=1;continue
                j=i+2;depth=0;actual=[];current=[]
                while j<len(parts):
                    t=parts[j]
                    if t==')' and not depth:
                        if current or actual:actual.append(' '.join(current))
                        break
                    if t==',' and not depth:actual.append(' '.join(current));current=[]
                    else:
                        current.append(t)
                        if t=='(':depth+=1
                        elif t==')':depth-=1
                    j+=1
                if j>=len(parts) or len(actual)!=len(args):raise ValueError('宏实参无法唯一解析')
                replacements={key:self.expand(value,scope,disabled) for key,value in zip(args,actual)}
                body=' '.join(replacements.get(t.group(),t.group()) for t in tokens(body));i=j+1
            else:i+=1
            out.append(self.expand(body,scope,disabled+(name,)))
            if sum(map(len,out))>8192:raise ValueError('宏表达式超过展示预算')
        return ' '.join(out)

    def describe(self,entry):
        key=(entry.file,entry.line,self.macro.unit(entry.pc) if self.macro else None)
        if key in self.cache:return dict(self.cache[key])
        row=self.context(entry.file,entry.line);annotations=[];expansions=[];constants={}
        if self.macro and row['text']:
            cu=self.macro.unit(entry.pc);scope=self.macro.results.get((cu,entry.file.replace('\\','/'),entry.line),{});enums=self.macro.enums.get(cu,{})
            visited=set()
            def resolve(name):
                if name in visited or len(visited)>32:raise ValueError('宏依赖循环/深度过大')
                if name in enums:return integer(enums[name])
                if name not in scope:raise ValueError('未解析定义或运行时值未采集')
                args,body,_,_=self.definition(scope[name])
                if args is not None:raise ValueError('函数宏不是独立整数常量')
                visited.add(name)
                try:return CExpression(body,resolve).value()
                finally:visited.remove(name)
            seen=set();parts=tokens(row['text'])
            for i,token in enumerate(parts):
                name=token.group()
                if name in seen or not IDENT.match(name):continue
                seen.add(name)
                if name in enums:
                    annotations.append(f'{name} = {enums[name]}；DWARF枚举，CU 0x{cu:x}')
                    value=integer(enums[name]);constants[name]=f'0x{value.value&((1<<value.bits)-1):X}'
                elif name in scope:
                    try:
                        args,body,file,line=self.definition(scope[name])
                        if args is None:
                            value=resolve(name);annotations.append(f'{name} = {value.value} / 0x{value.value&((1<<value.bits)-1):X}；编译时常量，DWARF宏 {file}:{line}，CU 0x{cu:x}')
                            constants[name]=f'0x{value.value&((1<<value.bits)-1):X}'
                        elif i+1<len(parts) and parts[i+1].group()=='(':
                            # Macro body remains an expression context. Argument
                            # substitution is lexical and never yields runtime values.
                            depth=0;actual=[];current=[];j=i+2
                            while j<len(parts):
                                t=parts[j].group()
                                if t==')' and depth==0:
                                    if current or actual:actual.append(' '.join(current))
                                    break
                                if t==',' and depth==0:actual.append(' '.join(current));current=[]
                                else:
                                    current.append(t)
                                    if t in ('(','[','{'):depth+=1
                                    elif t in (')',']','}'):depth-=1
                                j+=1
                            if j==len(parts) or len(actual)!=len(args):raise ValueError('宏实参无法唯一解析')
                            replacements=dict(zip(args,actual))
                            expanded=self.expand(' '.join(replacements.get(t.group(),t.group()) for t in tokens(body)),scope,(name,))
                            if len(expanded)>8192:raise ValueError('宏表达式超过展示预算')
                            annotations.append(f'{name}({", ".join(args)})；函数宏，DWARF {file}:{line}，CU 0x{cu:x}')
                            expansions.append(f'{name}({", ".join(args)}) => {expanded}；宏替换表达式上下文，运行时取值未采集；DWARF {file}:{line}，CU 0x{cu:x}')
                    except ValueError as e:annotations.append(f'{name}：未解析（{e}）');self.unresolved+=1
                elif name in self.macro.enumAmbiguity.get(cu,set()):
                    annotations.append(f'{name}：未解析（DWARF枚举同名冲突或可能被变量/形参遮蔽）');self.unresolved+=1
                elif name in self.macro.knownNames:
                    annotations.append(f'{name}：未解析（此CU/源码位置没有唯一有效定义）');self.unresolved+=1
        row['annotations']=annotations;row['expansions']=expansions;row['inlineConstants']=constants;self.annotationCount+=len(annotations);self.cache[key]=dict(row);return row

    def summary(self):
        return dict(statementContext=self.config['statements'],macroView=self.config['macroView'],
                    macroSectionPresent=bool(self.macro and self.macro.raw),annotationCount=self.annotationCount,
                    unresolved=self.unresolved,issues=sorted(set(getattr(self.macro,'issues',[]))) if self.macro else ([] if self.config['macroView']=='original' else [getattr(self,'macroError','未提供宏调试信息')]),
                    note='语句/宏展开为源码上下文；不是逐token执行证明，不提供未采集的运行时数值')
