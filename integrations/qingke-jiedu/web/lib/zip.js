/** JSZip（CDN UMD 全局）打包下载。 */
export async function zipBlobs(files) {
  if (!window.JSZip) throw new Error('JSZip 未加载');
  const zip = new window.JSZip();
  for (const f of files) zip.file(f.name, f.blob);
  return zip.generateAsync({ type: 'blob' });
}

/** 触发浏览器下载一个 Blob。 */
export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

/** 文本存成文件下载（Markdown 等）。 */
export function downloadText(text, filename, type = 'text/markdown') {
  downloadBlob(new Blob([text], { type: `${type};charset=utf-8` }), filename);
}
