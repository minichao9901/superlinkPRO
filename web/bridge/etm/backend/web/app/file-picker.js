function fullscreenDocument(win){
  try{if(win.top?.document?.fullscreenElement)return win.top.document;}catch{}
  return win.document;
}
/** Restore once, immediately after selection, before any file IO or upload.
 * Chromium drops fullscreen for its native picker. The modern picker renews
 * user activation on selection; cancellation/legacy browsers may refuse it.
 */
export function rememberFullscreen(win=window,onRestoreError=()=>{}){
  const doc=fullscreenDocument(win),element=doc.fullscreenElement;
  return async()=>{
    if(!element||doc.fullscreenElement||element.isConnected===false)return;
    try{await element.requestFullscreen();}catch(error){onRestoreError(error);}
  };
}
export function bindFilePicker(button,input,{consume,error,blocked=()=>false,types,id,win=window}){
  let restore=async()=>{},choosing=false;
  const selected=async file=>{
    await restore();restore=async()=>{};
    if(!file)return;
    if(blocked())throw Error('请等待当前操作完成后再载入文件');
    await consume(file);
  };
  input.onchange=async()=>{try{await selected(input.files?.[0]);}catch(e){error(e);}finally{input.value='';}};
  input.addEventListener('cancel',async()=>{await restore();restore=async()=>{};});
  button.onclick=async()=>{
    if(choosing||blocked())return;
    restore=rememberFullscreen(win,()=>error(Error('浏览器未允许自动恢复全屏，请点击「全屏」恢复')));
    choosing=true;
    try{
      // Keep the existing input path outside fullscreen and on older browsers.
      if(fullscreenDocument(win).fullscreenElement&&typeof win.showOpenFilePicker==='function'){
        let handles;
        try{handles=await win.showOpenFilePicker({id,types,multiple:false});}
        catch(e){
          if(e.name==='AbortError'){await selected(null);return;}
          if(['SecurityError','NotSupportedError'].includes(e.name)){input.click();return;}
          throw e;
        }
        // getFile() may do IO: restore while picker-provided activation is fresh.
        await restore();restore=async()=>{};
        await selected(await handles[0].getFile());
      }else input.click();
    }catch(e){error(e);}finally{choosing=false;}
  };
}
