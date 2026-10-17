/**
 * 开发用静态服务：**明确不发缓存**。
 *   node tools/dev/serve-nocache.mjs 8899      （等价：make serve-dev）
 *
 * 为什么不用 `python -m http.server`：
 *   它不发送 `Cache-Control`，浏览器于是启用**启发式缓存**（按 Last-Modified 估算一个"新鲜期"），
 *   于是"改完代码 → 刷新 → 还是老的"，用户按 Ctrl+F5 都不一定管用（ES 模块的缓存尤其顽固）。
 *   本服务对每个响应都发 `no-store, no-cache, must-revalidate` —— 刷新即最新。
 *
 * 只做静态文件 + 目录默认 index.html；不做目录列表（除 index.html 外一律 404）。
 */
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize, sep } from 'node:path';

const ROOT = process.cwd();
const PORT = Number(process.argv[2] || 8899);
const HOST = process.argv[3] || '127.0.0.1';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.md': 'text/plain; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.map': 'application/json',
  '.wasm': 'application/wasm',
};

const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  let path = decodeURIComponent(url.pathname);
  if (path.endsWith('/')) path += 'index.html';
  const file = normalize(join(ROOT, path));
  // 防目录穿越：解析后的路径必须仍在仓库根下
  if (file !== ROOT && !file.startsWith(ROOT + sep)){ res.writeHead(403).end('403'); return; }
  try {
    const st = await stat(file);
    if (st.isDirectory()){ res.writeHead(404).end('404（目录：请访问里面的 index.html）'); return; }
    const body = await readFile(file);
    res.writeHead(200, {
      'Content-Type': MIME[extname(file).toLowerCase()] || 'application/octet-stream',
      'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
      'Pragma': 'no-cache',
      'Expires': '0',
      'Content-Length': body.length,
    });
    res.end(body);
  } catch {
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }).end('404 ' + path);
  }
});

server.listen(PORT, HOST, () => {
  console.log(`静态服务（不发缓存）: http://${HOST}:${PORT}/index.html`);
  console.log(`根目录: ${ROOT}`);
  console.log('改完代码直接刷新即可（不需要 Ctrl+F5）。Ctrl+C 停止。');
});
