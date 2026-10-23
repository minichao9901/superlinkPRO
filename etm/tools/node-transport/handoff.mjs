/** A persistent manual Trace configuration must survive unrelated CLI calls. */
export async function finishTargetCommand(request,{target,dbg,hasSuspended}){
  if(request.action==='target'&&request.op==='close'&&!target.saved.size&&hasSuspended()){
    await target.disconnect();await dbg.connect();
  }
}
