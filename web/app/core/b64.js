/** base64 ↔ 字节（桥的 WebSocket 里传二进制用；分块避免 apply 参数上限）。 */

export function toB64(bytes){
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let s = '';
  for (let i = 0; i < b.length; i += 8192) s += String.fromCharCode.apply(null, b.subarray(i, i + 8192));
  return btoa(s);
}

export function fromB64(str){
  if (!str) return new Uint8Array(0);
  const bin = atob(str);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
