// node:url 的浏览器垫片（仅 fileURLToPath，给 src/config.js 用）。
export function fileURLToPath(u) {
  return decodeURIComponent(new URL(u).pathname);
}
