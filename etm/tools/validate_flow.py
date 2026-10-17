"""核对可证明的线性后继和立即数分支；不猜寄存器/内存间接跳转。"""
import re


def validate_flow(events):
    previous=None;checks=violations=0;examples=[]
    for event in events:
        if event['kind']!='instruction':
            previous=None
            continue
        expected=None
        if previous and previous.get('executed') is not None:
            if previous.get('flow')=='linear' or previous['executed'] is False:
                expected=previous['pc']+previous['size']
            elif previous.get('flow') in ('call','branch'):
                operands=previous.get('operands','').strip()
                mnemonic=previous.get('mnemonic','').split('.')[0]
                # ldr pc,[sp],#4 contains an immediate OFFSET, not a branch target.
                if (mnemonic.startswith('b') and operands.startswith('#')) or mnemonic in ('cbz','cbnz'):
                    values=re.findall(r'#(0x[0-9a-f]+|[0-9]+)',operands,re.I)
                    if values:expected=int(values[-1],0)&~1
        if expected is not None:
            checks+=1
            if event['pc']!=expected:
                violations+=1
                if len(examples)<20:examples.append(dict(previousSeq=previous['seq'],seq=event['seq'],
                    expected=expected,actual=event['pc'],instruction=previous['mnemonic']+' '+previous.get('operands','')))
        previous=event
    return dict(checks=checks,violations=violations,examples=examples,
                basis='known linear and direct-branch successors only; boundaries and unknown predicates break proofs')
