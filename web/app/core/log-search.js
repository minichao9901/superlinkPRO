/** Bounded, read-only log snapshots. Never alter receive buffers or recorders. */
export const LOG_SEARCH_BYTES = 128 * 1024;

export function logSnapshot(records, {hex=false, limit=LOG_SEARCH_BYTES}={}){
  const tail=[]; let bytes=0, clipped=false;
  for(let i=records.length-1;i>=0;i--){
    const b=records[i].b;
    const remaining=limit-bytes;
    if(b.length>remaining){tail.push(b.subarray(b.length-remaining));bytes+=remaining;clipped=true;break;}
    tail.push(b);bytes+=b.length;
    if(bytes===limit){clipped=i>0;break;}
  }
  tail.reverse();
  if(hex){
    const data=new Uint8Array(bytes);let offset=0;
    for(const b of tail){data.set(b,offset);offset+=b.length;}
    const lines=[];
    for(let i=0;i<data.length;i+=16)lines.push(Array.from(data.subarray(i,i+16),v=>v.toString(16).padStart(2,'0').toUpperCase()).join(' '));
    return {text:lines.join('\n'),bytes,clipped};
  }
  const decoder=new TextDecoder();let text='';
  for(const b of tail)text+=decoder.decode(b,{stream:true});
  text+=decoder.decode();
  // Render plain receive text, not terminal cursor/control actions.
  text=text.replace(/\x1b\][\s\S]*?(?:\x07|\x1b\\)/g,'')
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,'').replace(/\r\n?/g,'\n')
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g,'');
  return {text,bytes,clipped};
}

export function searchLog(text, query, {matchCase=false,filter=false,maxHighlights=1000}={}){
  if(!query)return {text,matches:[],total:0};
  const pattern=query.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  if(filter){
    const lineMatch=new RegExp(pattern,matchCase?'':'i');
    text=text.split('\n').filter(line=>lineMatch.test(line)).join('\n');
  }
  const expression=new RegExp(pattern,matchCase?'g':'gi');
  const matches=[];let total=0;
  for(const match of text.matchAll(expression)){
    total++;
    if(matches.length<maxHighlights)matches.push([match.index,match.index+match[0].length]);
  }
  return {text,matches,total};
}
