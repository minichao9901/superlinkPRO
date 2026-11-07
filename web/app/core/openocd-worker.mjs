import createOpenOcd from './openocd-runtime/openocd-core.mjs';
import { hpmOpenOcdFiles } from './openocd-hpm-profile.js';

export function createOpenOcdDispatcher({ post, select, wasmBinary, onExit }){
  const memoryFile = '/workbench-memory.bin';
  let core, next, startup, pending, closing, streamLogs, importedDevice, lines = [];
  const reply = (request, body) => post({ id:request.id, ...body });
  function log(line){
    const request = startup || pending || closing;
    if (request && streamLogs) reply(request, { log:line });
    lines.push(line);
    if (lines.length > 64) lines.shift();
  }

  async function open(request){
    startup = request;
    streamLogs = request.streamLogs === true;
    const raw = request.raw;
    const generated = typeof request.script === 'string';
    const files = raw ? raw.files : generated ? [['workbench.cfg', request.script]] : [
      ...await hpmOpenOcdFiles(new Map(request.files), request.entry),
      ['interface/cmsis-dap.cfg', 'adapter driver cmsis-dap\n'],
    ];
    // Loading Tcl must not open USB. A checked configuration is attached only
    // when the workbench explicitly issues init for the selected target.
    importedDevice = raw ? request.device : null;
    select(raw ? null : request.device);
    core = await createOpenOcd({
      wasmBinary,
      noInitialRun:true,
      print:log,
      printErr:log,
      nextCommand(){
        return new Promise(resolve => {
          next = resolve;
          if (startup){
            reply(startup, { lines });
            startup = null;
            lines = [];
          }
        });
      },
      commandResult(code, value){
        try {
          const bytes = !code && pending.type === 'memoryImage' && !pending.input
            ? core.FS.readFile(memoryFile) : undefined;
          reply(pending, { code, value, lines, bytes });
        } catch (error){ reply(pending, { error:error.message, lines }); }
        if (pending.type === 'memoryImage' && core.FS.analyzePath(memoryFile).exists)
          core.FS.unlink(memoryFile);
        pending = null;
        lines = [];
      },
    });
    const directory = raw ? '/cfg/import' : generated ? '/cfg' : '/cfg/hpm/boards/openocd';
    for (const [name, text] of files){
      if (raw && (name.split('/').some(part => !part || part === '.' || part === '..') || /[\\:\u0000-\u001f]/.test(name)))
        throw Error('CFG 路径必须位于所选目录');
      const path = directory + '/' + name;
      core.FS.mkdirTree(path.slice(0, path.lastIndexOf('/')));
      core.FS.writeFile(path, text);
    }
    const args = ['openocd', '-s', directory, '-c', 'noinit',
      '-c', 'gdb port disabled; tcl port disabled; telnet port disabled'];
    if (raw) args.push('-c', 'adapter driver cmsis-dap; adapter speed 1000',
      '-f', directory + '/' + raw.entry, '-c', 'noinit; gdb port disabled; tcl port disabled; telnet port disabled');
    else if (generated) args.push('-f', directory + '/workbench.cfg');
    else args.push(
      '-c', 'set HPM_SDK_BASE /cfg/hpm; set PROBE cmsis_dap',
      '-f', directory + '/hpm6800_all_in_one.cfg',
      '-c', 'reset_config none; foreach event {reset-init gdb-attach reset-deassert-pre} {hpm6880.cpu0 configure -event $event {}}');
    const argv = core._malloc((args.length + 1) * 4);
    const strings = args.map(arg => core.stringToNewUTF8(arg));
    for (const [i, ptr] of strings.entries()) core.setValue(argv + i * 4, ptr, 'i32');
    core.setValue(argv + args.length * 4, 0, 'i32');
    try {
      const code = await core.ccall('main', 'number', ['number', 'number'], [args.length, argv], { async:true });
      if (closing) reply(closing, code === 0 ? { lines } : { error:'OpenOCD 断开失败', lines });
      else throw new Error('OpenOCD 会话意外结束，状态 ' + code);
    } finally {
      for (const ptr of strings) core._free(ptr);
      core._free(argv);
    }
  }

  return request => {
    if (request.type === 'open'){
      open(request).catch(error => reply(startup || pending || closing || request, { error:error.message, lines })).finally(onExit);
    } else if (!next){
      reply(request, { error:'OpenOCD 会话未就绪' });
    } else if (request.type === 'flashFile'){
      try {
        if (request.input instanceof Uint8Array){
          core.FS.writeFile('/flash-input.bin', request.input);
          reply(request, {});
        } else if (request.read === true){
          const bytes = core.FS.readFile('/flash-readback.bin');
          core.FS.unlink('/flash-readback.bin');
          reply(request, { bytes });
        } else {
          core.FS.unlink('/flash-input.bin');
          reply(request, {});
        }
      } catch (error){ reply(request, { error:error.message }); }
    } else {
      let command = request.command;
      if (command === 'init' && importedDevice) select(importedDevice);
      if (request.type === 'memoryImage'){
        const { address, length, input } = request;
        if (!Number.isInteger(address) || address < 0 || address % 4 || !Number.isInteger(length) ||
            length < 4 || length % 4 || length > 0x40000 || address + length > 0x100000000 ||
            input != null && (!(input instanceof Uint8Array) || input.length !== length)){
          reply(request, { error:'OpenOCD 二进制内存范围无效' });
          return;
        }
        try { if (input) core.FS.writeFile(memoryFile, input); }
        catch (error){ reply(request, { error:error.message }); return; }
        const operation = input ? `load_image ${memoryFile} ${address} bin` : `dump_image ${memoryFile} ${address} ${length}`;
        // Image commands lack a phys flag. Switch translation for this one
        // command while keeping the selected memory access method intact.
        command = 'set workbench_mode [riscv virt2phys_mode]; riscv virt2phys_mode off; ' +
          `set workbench_rc [catch {${operation}} workbench_result]; ` +
          'riscv virt2phys_mode $workbench_mode; if {$workbench_rc} {error $workbench_result}; set workbench_result';
      }
      const resume = next;
      next = null;
      if (request.type === 'close'){
        closing = request;
        resume(null);
      } else {
        pending = request;
        resume(command);
      }
    }
  };
}

if (typeof WorkerGlobalScope !== 'undefined' && self instanceof WorkerGlobalScope){
  const usb = navigator.usb;
  let selection;
  Object.defineProperty(navigator, 'usb', { value:{
    async getDevices(){
      if (!selection) return [];
      const devices = await usb.getDevices();
      return devices.filter(d => d.vendorId === selection.vendorId &&
        d.productId === selection.productId && d.serialNumber === selection.serialNumber);
    },
  }});
  globalThis.WebSocket = class {
    constructor(){ throw Error('OpenOCD Worker 不提供 TCP 服务'); }
  };
  const dispatch = createOpenOcdDispatcher({
    post:message => self.postMessage(message),
    select:device => { selection = device; },
  });
  self.onmessage = ({ data }) => dispatch(data);
}
