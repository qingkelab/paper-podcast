// node:path 的浏览器垫片（posix 子集，仅覆盖 src/config.js 用到的函数）。
function normalize(p) {
  const parts = String(p).split('/');
  const out = [];
  for (const part of parts) {
    if (!part || part === '.') continue;
    if (part === '..') out.pop();
    else out.push(part);
  }
  return (String(p).startsWith('/') ? '/' : '') + out.join('/') || '/';
}
export function join(...args) {
  return normalize(args.filter(Boolean).join('/'));
}
export function resolve(...args) {
  return normalize(args.filter(Boolean).join('/'));
}
export function dirname(p) {
  const s = String(p);
  const i = s.lastIndexOf('/');
  return i <= 0 ? '/' : s.slice(0, i);
}
export function basename(p, ext = '') {
  const s = String(p);
  const b = s.slice(s.lastIndexOf('/') + 1);
  return ext && b.endsWith(ext) ? b.slice(0, -ext.length) : b;
}
export default { join, resolve, dirname, basename };
