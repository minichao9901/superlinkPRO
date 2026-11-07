import createJim from './openocd-jim.mjs';

const jim = await createJim();
const create = jim.cwrap('cfg_create', 'number', ['string', 'string']);
const setVariable = jim.cwrap('cfg_set', 'number', ['string', 'string']);
const run = jim.cwrap('cfg_run', 'number', ['string']);
const result = jim.cwrap('cfg_result', 'string', []);
const value = jim.cwrap('cfg_arg', 'string', ['number', 'number']);
const encoder = new TextEncoder();

function mount(files){
  if (files.length > 4096) throw new Error('CFG workspace exceeds 4096 files');
  let bytes = 0;
  const seen = new Set();
  for (const [path, body] of files){
    if (!/\.(cfg|tcl)$/i.test(path) || /[\u0000-\u001f:\\]/.test(path) ||
        path.split('/').some(part => !part || part === '.' || part === '..') || seen.has(path))
      throw new Error(`Invalid workspace path: ${path}`);
    seen.add(path);
    const content = encoder.encode(body.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n'));
    bytes += content.length;
    if (bytes > 8 * 1024 * 1024) throw new Error('CFG workspace exceeds 8 MiB');
    const absolute = '/cfg/' + path;
    jim.FS.mkdirTree(absolute.slice(0, absolute.lastIndexOf('/')));
    jim.FS.writeFile(absolute, content);
  }
}

// One capture per worker. The page can terminate it even during a Tcl loop.
self.addEventListener('message', ({ data }) => {
  try {
    const { files, entry, transport = 'swd', searchRoot = '', variables = {} } = data;
    mount(files);
    if (!['swd', 'jtag'].includes(transport) || !create(transport, searchRoot))
      throw new Error('Invalid capture transport or search root');
    for (const [name, content] of Object.entries(variables)){
      if (!/^[a-z_][a-z_0-9]*$/i.test(name) || setVariable(name, String(content)))
        throw new Error(`Invalid CFG variable: ${name}`);
    }
    const code = run(entry);
    const commands = Array.from({ length:jim._cfg_count() }, (_, row) =>
      Array.from({ length:jim._cfg_arity(row) }, (_, col) => value(row, col)));
    self.postMessage({ code, result:result(), commands });
  } catch (error){
    self.postMessage({ error:error.message });
  }
}, { once:true });
self.postMessage({ ready:true });
