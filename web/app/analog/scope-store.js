/** Bounded raw-code history; no per-sample JS objects or canvas work in USB pump. */
export class AdcScopeStore {
  constructor(capacity=65536){this.capacity=capacity;this.codes=new Uint16Array(capacity);this.reset();}
  reset(){this.total=0;this.length=0;this.rate=0;this.bits=16;}
  append(block){
    this.rate=block.rate;this.bits=block.bits;
    const offset=this.total%this.capacity,n=block.codes.length;
    if(n>=this.capacity){this.codes.set(block.codes.subarray(n-this.capacity));this.total+=n;
      // Preserve ring index even for synthetic oversized blocks.
      const copy=this.codes.slice();for(let i=0;i<this.capacity;i++)this.codes[(this.total-this.capacity+i)%this.capacity]=copy[i];
    }else{const first=Math.min(n,this.capacity-offset);this.codes.set(block.codes.subarray(0,first),offset);this.codes.set(block.codes.subarray(first),0);this.total+=n;}
    this.length=Math.min(this.capacity,this.length+n);
  }
  code(index){return this.codes[index%this.capacity];}
  frame({timeDiv,reference=3.3,trigger='auto',level=1.65,edge='rising',pretrigger=0.25}={}){
    if(!this.length||!this.rate)return null;
    const wanted=Math.max(2,Math.round(timeDiv*10*this.rate));
    const size=Math.min(wanted,this.length,this.capacity),oldest=this.total-this.length;
    let start=this.total-size,triggerIndex=null;
    const threshold=level/reference*(2**this.bits-1),pre=Math.floor(size*pretrigger),post=size-pre;
    for(let i=this.total-post;i>=Math.max(oldest+1,oldest+pre);i--){
      const a=this.code(i-1),b=this.code(i);
      if(edge==='falling'?(a>threshold&&b<=threshold):(a<threshold&&b>=threshold)){start=i-pre;triggerIndex=i;break;}
    }
    if(trigger==='normal'&&triggerIndex===null)return null;
    const codes=new Uint16Array(size);for(let i=0;i<size;i++)codes[i]=this.code(start+i);
    return {codes,start,rate:this.rate,bits:this.bits,triggered:triggerIndex!==null,limited:wanted>size};
  }
  csv(reference=3.3){
    const lines=['index,time_s,code,volts'];
    for(let i=this.total-this.length;i<this.total;i++){const c=this.code(i);lines.push(`${i},${i/this.rate},${c},${c/(2**this.bits-1)*reference}`);}
    return lines.join('\n');
  }
}
/** Pixel min/max envelope retains narrow spikes when thousands of points share a pixel. */
export function envelope(codes,width){
  const out=[];
  for(let x=0;x<Math.min(width,codes.length);x++){
    const first=Math.floor(x*codes.length/Math.min(width,codes.length));
    const end=Math.floor((x+1)*codes.length/Math.min(width,codes.length));
    let min=65535,max=0;for(let i=first;i<end;i++){min=Math.min(min,codes[i]);max=Math.max(max,codes[i]);}
    out.push({x,min,max});
  }return out;
}
