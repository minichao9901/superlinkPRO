/** A mismatched PC is accepted only with matching CPU, FPB and exception-stack evidence.
 * Self-contained for injection into the hardware test browser. */
export async function confirmedM7BreakpointRace(d,address,board){
 const s=d.session;
  if(board!=='h743'||d.sym.funcAt(s.pc)?.name!=='SysTick_Handler')return null;
  const cpuid=(await s.probe._readWord(0xe000ed00).catch(()=>0))>>>0;
  if(((cpuid>>>4)&0xfff)!==0xc27)return null;
  const dfsr=(await s.probe._readWord(0xe000ed30).catch(()=>0))>>>0;
  if(!(dfsr&2)||(dfsr&4)||await s.dwt.haltReason().catch(()=>true))return null;
  if(!s.bps?.some(bp=>((bp&0xfffffffe)>>>0)===address))return null;
  const ctrl=(await s.probe._readWord(0xe0002000).catch(()=>0))>>>0;
  const count=Math.min(s.caps?.numCode||0,16),rev=1+((ctrl>>>28)&15);
  let fpbMatch=false;
  for(let i=0;i<count;i++){
   const comp=(await s.probe._readWord(0xe0002008+4*i).catch(()=>0))>>>0;
   if(!(comp&1))continue;
   const match=rev===2?(comp&0xfffffffe)>>>0:(((comp&0x1ffffffc)|((((comp>>>30)&3)===2)?2:0))>>>0);
   if(match===address){fpbMatch=true;break;}
  }
  if(!fpbMatch)return null;
  const xpsr=(await s.readReg('XPSR'))>>>0;
  if((xpsr&0x1ff)!==15)return null; // current exception must be SysTick
  const sp=(await s.readReg('SP'))>>>0,stack=await s.memRead(sp,32);
  if(stack.length!==32)return null;
  const dv=new DataView(stack.buffer,stack.byteOffset,stack.byteLength),stackedPc=dv.getUint32(24,true),stackedXpsr=dv.getUint32(28,true);
  if((stackedPc&0xfffffffe)!==address||!(stackedXpsr&0x01000000)||(stackedXpsr&0x1ff)!==0)return null;
  return {pc:s.pc>>>0,dfsr,stackedPc:stackedPc>>>0,fpbAddress:address};
 }
