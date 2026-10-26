"""Export exact call-stack SVGs from the retained H743 TCP/UDP records (no device I/O).

Network initialization: BSP_Config through trace_hook_ready, before idle polling.
Echo: the first receive callback's enclosing ethernetif_input invocation, including
the actual transmit path, not an arbitrary fixed range or a binned window.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'etm/tools'))
from trace_store import TraceStore


def function_spans(index, name):
    import numpy as np
    if name not in index.function_names:
        raise ValueError('Missing function: ' + name)
    identifier = index.function_names.index(name)
    hits = np.flatnonzero(index.fs_func == identifier)
    return [(int(index.fs_start_seq[i]), int(index.fs_end_seq[i])) for i in hits]


def locate_stage(store, protocol, phase):
    index = store.query_index
    if index is None:
        raise ValueError('This export requires the exact path index in the retained records')
    index.prepare()
    if phase == 'initialization':
        start = function_spans(index, 'BSP_Config')[0][0]
        ready = function_spans(index, 'trace_hook_ready')[0]
        return start, ready[1], {'beginFunction': 'BSP_Config', 'endFunction': 'trace_hook_ready',
            'definition': 'Network/application initialization after Board_Init; excludes subsequent idle polling'}
    import numpy as np
    callback = 'tcp_echoserver_recv' if protocol == 'tcp' else 'udp_echoserver_receive_callback'
    callback_seq = function_spans(index, callback)[0][0]
    pos = int(np.searchsorted(index.ps_start_seq, callback_seq, side='right')) - 1
    path = index.path_of_leaf(int(index.ps_leaf[pos]))
    parent_depth = path.index('ethernetif_input') + 1
    prefix = path[:parent_depth]
    low = high = pos
    while low > 0 and index.path_of_leaf(int(index.ps_leaf[low - 1]))[:parent_depth] == prefix:
        low -= 1
    while high + 1 < len(index.ps_start) and index.path_of_leaf(int(index.ps_leaf[high + 1]))[:parent_depth] == prefix:
        high += 1
    start = int(index.ps_start_seq[low])
    end = int(index.seq_of_insn(int(index.ps_start[high] + index.ps_count[high] - 1)))
    return start, end, {'callback': callback, 'callbackSeq': callback_seq,
        'beginFunction': 'ethernetif_input', 'endFunction': 'ethernetif_input',
        'definition': 'First payload-receive callback and its enclosing ingress/echo-transmit invocation'}


from flame_svg import make_frames, font_measurer, svg_document


def run(args):
    measure = font_measurer()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    report = {'schema': 'network-echo-exact-flames/1', 'cases': [], 'hardwareAccessed': False}
    for protocol in ('tcp', 'udp'):
        for phase in ('initialization', 'echo'):
            record = args.records / (protocol + ('-initialization' if phase == 'initialization' else '-two-flows'))
            with closing(TraceStore(record / 'store')) as store:
                start, end, boundary = locate_stage(store, protocol, phase)
                events = list(store.events(start, end))
                frames, counts, depth = make_frames(events)
                assert len(events) == end - start + 1
                if phase == 'echo':
                    callback = boundary['callback']
                    assert counts['fn:' + callback] > 0
                    send = 'tcp_write' if protocol == 'tcp' else 'udp_send'
                    assert counts['fn:' + send] > 0
                    assert counts['fn:HAL_ETH_Transmit'] > 0
                    before = next(store.events(start-1, start-1))
                    after = next(store.events(end+1, end+1))
                    assert before['function'] == after['function'] == 'main'
                    assert events[0]['function'] == events[-1]['function'] == 'ethernetif_input'
                    assert events[-1]['flow'] == 'return'
                else:
                    assert events[0]['function'] == 'BSP_Config'
                    assert events[-1]['function'] == 'trace_hook_ready'
                    assert counts['fn:lwip_init'] > 0 and counts['fn:Netif_Config'] > 0
                    assert counts['fn:' + protocol + '_echoserver_init'] > 0
                title = protocol.upper() + (' 网络初始化（BSP_Config → trace_hook_ready）' if phase == 'initialization' else ' 第一轮 Echo（收包 → 回显发送 → 返回）')
                svg, dimensions = svg_document(title, start, end, frames, depth, measure)
                destination = out / (protocol + '-' + phase + '-exact-flame.svg')
                destination.write_text(svg, encoding='utf-8')
                case = {'protocol': protocol, 'phase': phase, 'record': record.name,
                    'file': destination.name, 'start': start, 'end': end, 'depth': depth,
                    'frames': len(frames), 'counts': dict(counts), 'boundary': boundary,
                    'dimensions': dimensions, 'generation': store.generation,
                    'svgSha256': hashlib.sha256(svg.encode()).hexdigest(),
                    'instructionPathsChecked': counts['instructions'], 'passed': True}
                report['cases'].append(case)
                print(json.dumps({k:case[k] for k in ('file','start','end','depth','frames','dimensions')},ensure_ascii=False),flush=True)
    (out / 'exact-flames-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    templates=[];options=[]
    for case in report['cases']:
        key=case['protocol']+'-'+case['phase']
        templates.append('<template id="'+key+'">'+(out/case['file']).read_text(encoding='utf8')+'</template>')
        options.append('<option value="'+key+'">'+key+' · '+str(case['depth'])+' 层</option>')
    viewer='''<!doctype html><meta charset="utf-8"><title>执行顺序火焰图</title>
    <style>body{margin:0;background:#0d1117;color:#d0d7de;font:14px system-ui}header{padding:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}button,select{padding:7px;background:#202832;color:inherit;border:1px solid #45505e;border-radius:4px}#note{padding:0 12px 10px;color:#9aa4af}#pane{overflow:auto;border-top:1px solid #45505e;height:calc(100vh - 115px)}svg{display:block;max-width:none}a{color:#58a6ff}</style>
    <header><select id="choice">OPTIONS</select><button id="fit">总览</button><button id="full">完整名称 1:1</button><button id="out">缩小 ×2</button><button id="in">放大 ×2</button><a id="download">打开原始 SVG</a><span id="status"></span></header>
    <div id="note">与网页相同的执行顺序调用栈。总览只隐藏放不下的标签，完整名称模式显示全部；原始 SVG 始终保留完整名称。横轴是执行序号，并非毫秒。</div><div id="pane"></div>TEMPLATES
    <script>
    const pane=document.getElementById('pane'),choice=document.getElementById('choice');let svg,model,width,height,zoom=1;
    const files=FILES;
    function apply(s){zoom=Math.max(.00001,Math.min(4,s));svg.setAttribute('width',width*zoom);svg.setAttribute('viewBox','0 0 '+width*zoom+' '+height);svg.setAttribute('height',height);
     for(const m of model){m.rect.setAttribute('x',m.x*zoom);m.rect.setAttribute('width',Math.max(.1,m.w*zoom));m.text.setAttribute('x',m.tx*zoom);m.text.style.display=m.w*zoom>=m.len+12?'':'none';}
     document.getElementById('status').textContent='横向比例 '+(100*zoom).toFixed(3)+'%';}
    function fit(){apply((pane.clientWidth-4)/width);pane.scrollLeft=0;}
    function load(){svg=document.getElementById(choice.value).content.firstElementChild.cloneNode(true);pane.replaceChildren(svg);width=Number(svg.getAttribute('width'));height=Number(svg.getAttribute('height'));model=[...svg.querySelectorAll('[data-frame]')].map(g=>{const rect=g.querySelector('rect'),text=g.querySelector('text');return {rect,text,x:Number(rect.getAttribute('x')),w:Number(rect.getAttribute('width')),tx:Number(text.getAttribute('x')),len:Number(text.getAttribute('textLength'))};});document.getElementById('download').href=files[choice.value];fit();}
    choice.onchange=load;document.getElementById('fit').onclick=fit;document.getElementById('full').onclick=()=>apply(1);document.getElementById('out').onclick=()=>apply(zoom/2);document.getElementById('in').onclick=()=>apply(zoom*2);window.onresize=fit;load();
    </script>'''
    viewer=viewer.replace('OPTIONS',''.join(options)).replace('TEMPLATES',''.join(templates)).replace('FILES',json.dumps({c['protocol']+'-'+c['phase']:c['file'] for c in report['cases']}))
    (out/'view-exact-flames.html').write_text(viewer,encoding='utf8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args())
