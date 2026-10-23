/** Standard WebUSB surface plus deterministic lifetime for the CLI host. */
export function navigatorUsb(native){
  const listeners=new Map();
  return {
    getDevices:()=>native.getDevices(),
    requestDevice:options=>native.requestDevice(options),
    addEventListener(type,listener){
      if(!listeners.has(type))listeners.set(type,new Set());
      listeners.get(type).add(listener);native.addEventListener(type,listener);
    },
    removeEventListener(type,listener){listeners.get(type)?.delete(listener);native.removeEventListener(type,listener);},
    async dispose(){
      for(const [type,callbacks]of listeners)for(const callback of callbacks)native.removeEventListener(type,callback);
      listeners.clear();
      // usb 3.2.1 serializes native hotplug registrations/removals here. Waiting
      // avoids retaining its event thread after all probe handles are closed.
      await native.nativeEmitterQueue;
    },
  };
}
