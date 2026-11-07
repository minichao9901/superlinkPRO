import createOpenOcd from './openocd-core.mjs';

// Offline verification only. Abort any accidental attempt to open a browser
// transport before the test's explicit shutdown in the configuration phase.
const attempted = [];
let emptyUsb = false;
Object.defineProperty(navigator, 'usb', {
  get() {
    if (emptyUsb){
      return {
        async getDevices(){
          await new Promise(resolve => setTimeout(resolve, 10));
          attempted.push('simulated WebUSB enumeration');
          return [];
        },
      };
    }
    attempted.push('USB');
    throw new Error('USB is disabled during configuration verification');
  },
});
globalThis.WebSocket = class {
  constructor() {
    attempted.push('socket');
    throw new Error('Network is disabled during configuration verification');
  }
};

const lines = [];
const core = await createOpenOcd({
  noInitialRun:true,
  print:line => lines.push(line),
  printErr:line => lines.push(line),
});

// callMain uses a synchronous entry path. Keep argv alive across the
// Asyncify suspension when the real libusb backend awaits getDevices().
async function runAsync(args){
  const values = ['openocd', ...args];
  const argv = core._malloc((values.length + 1) * 4);
  const strings = [];
  try {
    for (const [i, value] of values.entries()){
      const bytes = core.lengthBytesUTF8(value) + 1;
      const pointer = core._malloc(bytes);
      strings.push(pointer);
      core.stringToUTF8(value, pointer, bytes);
      core.setValue(argv + 4 * i, pointer, 'i32');
    }
    core.setValue(argv + 4 * values.length, 0, 'i32');
    return await core.ccall('main', 'number', ['number', 'number'], [values.length, argv], { async:true });
  } finally {
    for (const pointer of strings) core._free(pointer);
    core._free(argv);
  }
}

self.addEventListener('message', async ({ data:{ files, args, simulateEmptyUsb } }) => {
  try {
    for (const [name, body] of files){
      if (!/\.(cfg|tcl)$/i.test(name) || /[\u0000-\u001f:\\]/.test(name) ||
          name.split('/').some(part => !part || part === '.' || part === '..'))
        throw new Error(`Invalid fixture path ${name}`);
      const path = '/cfg/' + name;
      core.FS.mkdirTree(path.slice(0, path.lastIndexOf('/')));
      core.FS.writeFile(path, body.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n'));
    }
    emptyUsb = simulateEmptyUsb === true;
    const code = emptyUsb ? await runAsync(args) : core.callMain(args);
    self.postMessage({ code, lines, attempted });
  } catch (error){
    self.postMessage({ error:error.message, lines, attempted });
  }
}, { once:true });
self.postMessage({ ready:true });
