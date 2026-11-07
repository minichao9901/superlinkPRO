export function captureOpenOcdConfig(request, { timeoutMs = 2000, loadTimeoutMs = 15000 } = {}){
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('./openocd-jim-wasm-worker.mjs', import.meta.url), { type:'module' });
    const finish = (settle, value) => {
      clearTimeout(timer);
      worker.terminate();
      settle(value);
    };
    let timer = setTimeout(() => finish(reject, new Error('Jim Tcl worker did not load')), loadTimeoutMs);
    worker.onmessage = ({ data }) => {
      if (data.ready){
        clearTimeout(timer);
        timer = setTimeout(() => finish(reject, new Error('CFG evaluation timed out')), timeoutMs);
        worker.postMessage(request);
      } else if (data.error) finish(reject, new Error(data.error));
      else finish(resolve, data);
    };
    worker.onerror = event => finish(reject, new Error(event.message || 'Jim Tcl worker failed'));
  });
}
