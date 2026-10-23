// pdfjs-dist 单例加载（Node 端使用 legacy 构建，免独立 worker）
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

let pdfjs;
let assetDirs;

/**
 * pdfjs 的 CMap / 标准字体资源目录。
 *
 * 中文 PDF（尤其 HTML→PDF 导出的简中论文、扫描版式文档）普遍用 CID 字体 + CMap 编码
 * （UniGB-UCS2-H、GBK-EUC-H 之类）。不提供 `cMapUrl` 时 pdfjs 会报
 * 「Ensure that the `cMapUrl` API parameter is provided」→ 字体翻译失败 → 中文全部丢失、
 * 只剩英文和公式。`standardFontDataUrl` 同理，用于缺字体时的系统字体替换。
 *
 * Node 端 pdfjs 用 fs.readFile 读取，所以这里给的是本地目录路径（且必须以 `/` 结尾）。
 */
function pdfjsAssetDirs() {
  if (assetDirs) return assetDirs;
  const require = createRequire(import.meta.url);
  const root = path.dirname(require.resolve('pdfjs-dist/package.json'));
  const pick = (name) => {
    const dir = path.join(root, name);
    return fs.existsSync(dir) ? dir + path.sep : undefined;
  };
  assetDirs = {
    cMapUrl: pick('cmaps'),
    standardFontDataUrl: pick('standard_fonts'),
  };
  return assetDirs;
}

export async function loadPdfjs() {
  if (!pdfjs) {
    const require = createRequire(import.meta.url);
    const workerPath = require.resolve('pdfjs-dist/legacy/build/pdf.worker.mjs');
    pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs');
    // Node 端 fake worker 通过 import(workerSrc) 加载，必须是 file:// URL
    pdfjs.GlobalWorkerOptions.workerSrc = pathToFileURL(workerPath).href;
  }
  return pdfjs;
}

/**
 * 从 PDF Buffer 创建文档对象。
 */
export async function getPdfDocument(buffer) {
  const lib = await loadPdfjs();
  // Buffer 是 Uint8Array 的子类，pdfjs v4 明确要求纯 Uint8Array，故始终拷贝一份
  const data = new Uint8Array(buffer);
  const loadingTask = lib.getDocument({
    data,
    useSystemFonts: true,
    isEvalSupported: false,
    verbosity: 0,
    ...pdfjsAssetDirs(), // CJK（CID/CMap）字体必需，否则中文会整段丢失
    cMapPacked: true,
  });
  return loadingTask.promise;
}
