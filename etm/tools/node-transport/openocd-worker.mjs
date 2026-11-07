import { readFile } from 'node:fs/promises';
import { OpenOcdTargetSession } from '../../../web/app/core/openocd-session.js';
import { createOpenOcdDispatcher } from '../../../web/app/core/openocd-worker.mjs';

// Keep the native WebUSB device in its original realm. Use the same Wasm
// dispatcher as Chrome, and retain the lease until the native USB call drains.
export async function installNativeOpenOcd(usb){
  const wasmBinary = await readFile(new URL('../../../web/app/core/openocd-runtime/openocd-core.wasm', import.meta.url));
  let active = false;
  let selection = null;
  Object.defineProperty(globalThis, 'navigator', { configurable:true, value:{ usb:{
    async getDevices(){
      if (!selection) return [];
      const devices = await usb.getDevices();
      return devices.filter(device => device.vendorId === selection.vendorId &&
        device.productId === selection.productId && device.serialNumber === selection.serialNumber);
    },
  } }});
  globalThis.WebSocket = class {
    constructor(){ throw Error('OpenOCD 不提供 TCP 服务'); }
  };
  OpenOcdTargetSession.createWorker = () => {
    if (active) throw Error('前一个 OpenOCD Wasm 会话仍在释放 USB 接口');
    active = true;
    let terminated = false, closeRequested = false;
    const worker = {
      postMessage(data){ queueMicrotask(() => dispatch(data)); },
      terminate(){ terminated = true; },
    };
    const dispatch = createOpenOcdDispatcher({
      wasmBinary,
      post:data => queueMicrotask(() => {
        if (!terminated) worker.onmessage?.({ data });
        else if (active && !closeRequested && typeof data.log !== 'string'){
          closeRequested = true;
          dispatch({ type:'close' });
        }
      }),
      select:device => { selection = device; },
      onExit(){ active = false; selection = null; },
    });
    return worker;
  };
}
