// One Worker owns the target connection. Callers share it through ProbeManager.
export class OpenOcdTargetSession {
  constructor(onLog){
    this.onLog = onLog;
    this.worker = null;
    this.sequence = 0;
    this.queue = Promise.resolve();
  }

  async open(cfg, device){
    if (this.worker) throw new Error('OpenOCD 会话已经打开');
    this.worker = new Worker(new URL('./openocd-worker.mjs', import.meta.url), { type:'module' });
    this.worker.onmessage = ({ data }) => {
      if (data.id !== this.pending?.id) return;
      if (typeof data.log === 'string') {
        this.pending.lastLog = data.log;
        if (this.pending.type === 'open' || this.pending.command === 'init') this.onLog?.(data.log);
        return;
      }
      const error = data.error || (data.code ? data.lines?.join('\n') || data.value || 'OpenOCD 命令失败' : '');
      this.finish(error ? new Error(error) : null, data);
    };
    this.worker.onerror = event => {
      this.worker.terminate();
      this.worker = null;
      this.finish(new Error(event.message || 'OpenOCD Wasm 无法加载'));
    };
    const { vendorId, productId, serialNumber } = device;
    try {
      await this.request('open', { files:cfg.files, entry:cfg.name, streamLogs:true, device:{ vendorId, productId, serialNumber } });
    } catch (error){
      await this.close().catch(() => {});
      throw error;
    }
  }

  request(type, payload, timeoutMs = 15000){
    const task = this.queue.then(() => new Promise((resolve, reject) => {
      if (!this.worker) throw new Error('OpenOCD 会话已关闭');
      const id = ++this.sequence;
      const timer = setTimeout(() => {
        const lastLog = this.pending?.lastLog;
        this.finish(new Error('OpenOCD 操作超时' + (lastLog ? '：' + lastLog : '')));
        this.worker?.terminate();
        this.worker = null;
      }, timeoutMs);
      this.pending = { id, type, command:payload?.command, timer, resolve, reject };
      this.worker.postMessage({ id, type, ...payload });
    }));
    this.queue = task.catch(() => {});
    return task;
  }

  finish(error, result){
    const pending = this.pending;
    this.pending = null;
    if (!pending) return;
    clearTimeout(pending.timer);
    if (error) pending.reject(error);
    else pending.resolve(result);
  }

  async command(command, timeoutMs){
    const response = await this.request('command', { command }, timeoutMs);
    return response.value.trim();
  }

  async close(){
    if (!this.worker) return;
    try { await this.request('close'); }
    finally {
      this.worker?.terminate();
      this.worker = null;
    }
  }
}
