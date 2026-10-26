"""Offline equivalence acceptance: exact call paths, CLI and real Flask download.
Run with --records pointing to the four retained TCP/UDP records. No hardware I/O.
"""
import argparse
import bisect
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'etm/tools'))
from trace_cli import main as cli
from trace_store import TraceStore
from export_network_echo_flames import locate_stage,make_frames,svg_document,font_measurer

NS={'s':'http://www.w3.org/2000/svg'}
def geometry(data):
    root=ET.fromstring(data)
    groups=root.findall('.//s:g[@data-frame]',NS)
    rows=[]
    for g in groups:
        r=g.find('s:rect',NS);t=g.find('s:text',NS)
        assert float(t.attrib['textLength'])+12<=float(r.attrib['width'])+.002
        rows.append((dict(g.attrib),dict(r.attrib),t.text,dict(t.attrib)))
    assert '多段调用（放大查看）' not in data.decode('utf8')
    return rows

def run(args):
    spec=importlib.util.spec_from_file_location('flame_acceptance_server',ROOT/'etm/server.py')
    server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)
    server.RECORDS=args.records.resolve();client=server.app.test_client()
    report={'schema':'flame-svg-acceptance/1','hardwareAccessed':False,'cases':[]}
    with tempfile.TemporaryDirectory(prefix='flame-acceptance-') as tmp:
        for protocol in ('tcp','udp'):
            for phase in ('initialization','echo'):
                name=protocol+('-initialization' if phase=='initialization' else '-two-flows')
                folder=server.RECORDS/name
                with contextlib.closing(TraceStore(folder/'store')) as store:
                    low,high,boundary=locate_stage(store,protocol,phase)
                    frames,counts,depth=make_frames(store.events(low,high))
                    reference,_=svg_document('reference',low,high,frames,depth,font_measurer())
                target=Path(tmp)/(name+'.svg');output=io.StringIO()
                with contextlib.redirect_stdout(output):
                    code=cli(['export','--record',str(folder),'--format','flame-svg','--from-seq',str(low),'--to-seq',str(high),'--out',str(target)])
                if code:raise RuntimeError(output.getvalue())
                with client.get('/api/download/'+name+'/flame-svg?fromSeq='+str(low)+'&toSeq='+str(high),buffered=True) as response:
                    assert response.status_code==200,response.data
                    assert response.mimetype=='image/svg+xml'
                    web=response.data
                # Independently reconstruct each original path from the emitted XML,
                # rather than relying solely on the generator's own active stack.
                groups=ET.fromstring(web).findall('.//s:g[@data-frame]',NS)
                levels={}
                for g in groups:
                    levels.setdefault(int(g.attrib['data-depth']),[]).append((int(g.attrib['data-start']),int(g.attrib['data-end']),g.find('s:text',NS).text))
                starts={d:[x[0] for x in rows] for d,rows in levels.items()}
                checked=0
                with contextlib.closing(TraceStore(folder/'store')) as original:
                    for event in original.events(low,high):
                        if event['kind']!='instruction':continue
                        path=[]
                        for d,rows in sorted(levels.items()):
                            i=bisect.bisect_right(starts[d],event['seq'])-1
                            if i>=0 and rows[i][0]<=event['seq']<=rows[i][1]:path.append(rows[i][2])
                        assert path==event['callPath'],(event['seq'],path,event['callPath'])
                        checked+=1
                assert checked==counts['instructions']
                command=target.read_bytes()
                assert command==web,'Web and CLI must produce identical SVG bytes'
                assert geometry(web)==geometry(reference.encode()),'Every frame must match exact raw call paths'
                # Confirm query validation and useful failure, with no leftover temp download.
                with client.get('/api/download/'+name+'/flame-svg?fromSeq='+str(low)) as bad:
                    assert bad.status_code==400 and bad.json.get('error')
                assert not list(folder.glob('export-*.svg'))
                item=dict(record=name,phase=phase,fromSeq=low,toSeq=high,depth=depth,frames=len(frames),
                          instructionPathsChecked=counts['instructions'],sha256=hashlib.sha256(web).hexdigest(),
                          webCliBytesEqual=True,referenceGeometryEqual=True,xmlPathsIndependentlyChecked=checked,allNamesFit=True,boundary=boundary)
                report['cases'].append(item);print(json.dumps(item,ensure_ascii=False),flush=True)
        # Aggregation shares the same tree, weighting and subtree identity across interfaces.
        name='tcp-two-flows';folder=server.RECORDS/name;low,high=1714288,1718201
        for weight in ('records','cycles'):
            output=io.StringIO();target=Path(tmp)/(weight+'.svg')
            argv=['export','--record',str(folder),'--format','flame-svg','--flame-mode','aggregate','--flame-weight',weight,
                  '--from-seq',str(low),'--to-seq',str(high),'--out',str(target)]
            with contextlib.redirect_stdout(output):code=cli(argv)
            with client.get('/api/download/'+name+'/flame-svg?mode=aggregate&weight='+weight+'&fromSeq='+str(low)+'&toSeq='+str(high),buffered=True) as r:
                if code==0:
                    assert r.status_code==200,r.data
                    assert target.read_bytes()==r.data
                    geometry(r.data)
                else:
                    assert weight=='cycles' and r.status_code==400 and '周期' in r.json['error']
            report['aggregate-'+weight]='identical' if code==0 else 'explicitly unavailable: no cycle data'
    report['passed']=True
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--records',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    run(p.parse_args())
