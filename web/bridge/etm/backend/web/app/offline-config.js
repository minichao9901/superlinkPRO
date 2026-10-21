const V3=['idr','ctrl','ccer','traceid'];
const V4=['idr0','idr1','idr2','idr8','idr9','idr10','idr11','idr12','idr13','config','traceid'];
function object(value){return value&&typeof value==='object'&&!Array.isArray(value);}
function uint(value,name,max=0xffffffff){
  if(typeof value==='string'&&/^(?:0x[\da-f]+|\d+)$/i.test(value.trim()))value=Number(value.trim());
  if(!Number.isInteger(value)||value<0||value>max)throw Error(name+' 必须是 0～'+max+' 的整数');
  return value;
}
/** Only a saved snapshot or explicitly entered registers may select a decoder. */
export function offlineSettings(value,defaults={}){
  if(!object(value))throw Error('采集配置必须是 JSON 对象');
  const session=object(value.config),raw=session?value.config:value;
  let protocol=raw.protocol;
  if(!protocol){
    if(raw.idr1!==undefined&&((uint(raw.idr1,'idr1')>>>8)&15)===4)protocol='ETMv4';
    else if(raw.idr!==undefined&&((uint(raw.idr,'idr')>>>8)&15)===2)protocol='ETMv3';
    else throw Error('无法识别 ETM 协议，请提供采集时的完整寄存器配置');
  }
  if(!['ETMv3','ETMv4'].includes(protocol))throw Error('不支持的 ETM 协议：'+protocol);
  const config={protocol};
  for(const key of protocol==='ETMv3'?V3:V4){
    if(raw[key]===undefined)throw Error('采集配置缺少寄存器：'+key);
    config[key]=uint(raw[key],key);
  }
  if(((config[protocol==='ETMv3'?'idr':'idr1']>>>8)&15)!==(protocol==='ETMv3'?2:4))throw Error('ETM 协议与架构寄存器不一致');
  if(config.traceid<1||config.traceid>111)throw Error('Trace ID 范围为 1～111');
  const physical=session?value:{},clock=uint(physical.clock??defaults.clock??0,'CLK',15);
  const data=physical.data??defaults.data??[1,2,3,4];
  if(!Array.isArray(data)||data.length!==4)throw Error('需要四路 D0～D3 通道');
  const channels=[clock,...data.map((v,i)=>uint(v,'D'+i,15))];
  if(new Set(channels).size!==5)throw Error('CLK 与 D0～D3 必须是五个不同通道');
  const fraction=physical.fraction??defaults.fraction??0.25,glitchNs=physical.glitchNs??defaults.glitchNs??0;
  if(typeof fraction!=='number'||!Number.isFinite(fraction)||fraction<=0||fraction>=1)throw Error('离线取样位置必须介于 0 和 1 之间');
  if(typeof glitchNs!=='number'||!Number.isFinite(glitchNs)||glitchNs<0||glitchNs>100)throw Error('离线滤波宽度须为 0～100 ns');
  // elfId is a random upload/session ID, not a firmware identity.
  const elfSha256=session?(value.elfSha256??value.elf?.sha256??null):null;
  if(elfSha256!==null&&(typeof elfSha256!=='string'||!/^[\da-f]{64}$/i.test(elfSha256)))throw Error('ELF SHA256 格式无效');
  return {config,clock,data:channels.slice(1),fraction,glitchNs,elfSha256:elfSha256?.toLowerCase()??null};
}
