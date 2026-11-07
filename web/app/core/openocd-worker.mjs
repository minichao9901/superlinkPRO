import createOpenOcd from './openocd-runtime/openocd-core.mjs';
import { hpmOpenOcdFiles } from './openocd-hpm-profile.js';

const usb = navigator.usb;
let selection;
Object.defineProperty(navigator, 'usb', { value:{
  async getDevices(){
    log('WebUSB：枚举已授权设备');
    const devices = await usb.getDevices();
    const matching = devices.filter(d => d.vendorId === selection.vendorId &&
      d.productId === selection.productId && d.serialNumber === selection.serialNumber);
    log('WebUSB：匹配 ' + matching.length + ' 个设备');
    return matching;
  },
}});
globalThis.WebSocket = class {
  constructor(){ throw new Error('OpenOCD Worker 不提供 TCP 服务'); }
};

let core, next, startup, pending, closing, streamLogs, lines = [];
const reply = (request, body) => self.postMessage({ id:request.id, ...body });
function log(line){
  const request = startup || pending || closing;
  if (request && streamLogs) reply(request, { log:line });
  lines.push(line);
  if (lines.length > 64) lines.shift();
}

async function open(request){
  startup = request;
  streamLogs = request.streamLogs === true;
  const files = await hpmOpenOcdFiles(new Map(request.files), request.entry);
  selection = request.device;
  core = await createOpenOcd({
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
      reply(pending, { code, value, lines });
      pending = null;
      lines = [];
    },
  });
  const directory = '/cfg/hpm/boards/openocd';
  for (const [name, text] of [...files, ['interface/cmsis-dap.cfg', 'adapter driver cmsis-dap\n']]){
    const path = directory + '/' + name;
    core.FS.mkdirTree(path.slice(0, path.lastIndexOf('/')));
    core.FS.writeFile(path, text);
  }
  const args = ['openocd', '-s', directory, '-c', 'noinit',
    '-c', 'gdb port disabled; tcl port disabled; telnet port disabled',
    '-c', 'set HPM_SDK_BASE /cfg/hpm; set PROBE cmsis_dap',
    '-f', directory + '/hpm6800_all_in_one.cfg',
    '-c', 'reset_config none; foreach event {reset-init gdb-attach reset-deassert-pre} {hpm6880.cpu0 configure -event $event {}}'];
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

self.onmessage = ({ data:request }) => {
  if (request.type === 'open'){
    open(request).catch(error => reply(startup || pending || closing || request, { error:error.message, lines }));
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
    const resume = next;
    next = null;
    if (request.type === 'close'){
      closing = request;
      resume(null);
    } else {
      pending = request;
      resume(request.command);
    }
  }
};
