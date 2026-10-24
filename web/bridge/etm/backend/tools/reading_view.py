"""Reuse the original compact two-pane reader; folding stays plain comments."""
from source_export import _html_document


def document(bounds=None):
    page=_html_document('__READING_PAYLOAD__',bounds)
    page=page.replace('连续相同源码位置合并，循环顺序保留。',
                      '宏值紧随名称注释；重复执行以注释省略中间次数。完整映射与质量见配套清单。')
    # Resolve a hidden raw sequence without adding panels or changing the UI.
    page=page.replace('function choose(index){selected=index;const r=data.rows[index];',
        """function resolve(r,seq){if(!r.repeat||seq==null)return r;const p=r.repeat,shift=Math.floor((seq-p.baseSeq)/p.stride)*p.stride,local=seq-shift,part=p.pattern.find(x=>x.seq<=local&&local<=x.endSeq);return part?{...part,seq:part.seq+shift,endSeq:part.endSeq+shift}:r;}
function choose(index,seq=null){selected=index;const r=resolve(data.rows[index],seq);""")
    return page.replace('paint();choose(i);','paint();choose(i,seq);')
