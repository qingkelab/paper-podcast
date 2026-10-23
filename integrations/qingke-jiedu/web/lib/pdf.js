/**
 * 浏览器端 PDF 渲染与正文抽取：pdfjs-dist（CDN ESM）→ canvas → PNG Blob。
 * 中文 PDF 依赖 cmaps / standard_fonts（CID 字体 + CMap 编码），一并从 CDN 加载。
 */
const PDFJS_BASE = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@6.2.108';

let pdfjsPromise = null;
function loadPdfjs() {
  if (!pdfjsPromise) {
    pdfjsPromise = import(/* @vite-ignore */ `${PDFJS_BASE}/build/pdf.min.mjs`).then((pdfjs) => {
      pdfjs.GlobalWorkerOptions.workerSrc = `${PDFJS_BASE}/build/pdf.worker.min.mjs`;
      return pdfjs;
    });
  }
  return pdfjsPromise;
}

function canvasToBlob(canvas) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('图片编码失败'))), 'image/png');
  });
}

/**
 * @param {ArrayBuffer|Uint8Array} data
 * @returns {Promise<{images:Array<{filename:string,label:string,width:number,height:number,blob:Blob}>,
 *   text:string, textLines:string, title:string, author:string, creationDate:string, pageCount:number}>}
 */
export async function pdfToImages(data, { maxPages = 30, scale = 2 } = {}) {
  const pdfjs = await loadPdfjs();
  const doc = await pdfjs.getDocument({
    data,
    cMapUrl: `${PDFJS_BASE}/cmaps/`,
    cMapPacked: true,
    standardFontDataUrl: `${PDFJS_BASE}/standard_fonts/`,
  }).promise;

  // 元数据（标题 / 作者 / 创建时间），失败不阻断
  let title = '';
  let author = '';
  let creationDate = '';
  try {
    const meta = await doc.getMetadata();
    title = String(meta?.info?.Title || '').trim();
    author = String(meta?.info?.Author || '').trim();
    creationDate = String(meta?.info?.CreationDate || '').trim();
  } catch {
    /* 无元数据 */
  }

  const pageCount = doc.numPages;
  const n = Math.min(pageCount, maxPages);
  const images = [];
  const textParts = [];
  const lineParts = [];

  for (let i = 1; i <= n; i++) {
    const page = await doc.getPage(i);
    const viewport = page.getViewport({ scale });
    const canvas = document.createElement('canvas');
    canvas.width = Math.floor(viewport.width);
    canvas.height = Math.floor(viewport.height);
    await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
    const blob = await canvasToBlob(canvas);
    images.push({
      filename: `${String(i).padStart(3, '0')}.png`,
      label: `第 ${i} 页`,
      width: canvas.width,
      height: canvas.height,
      blob,
    });

    try {
      const tc = await page.getTextContent();
      let pageText = '';
      let pageLines = '';
      for (const item of tc.items) {
        const s = item.str || '';
        pageText += s;
        pageLines += s;
        if (item.hasEOL) {
          pageText += ' ';
          pageLines += '\n';
        }
      }
      textParts.push(pageText);
      lineParts.push(pageLines);
    } catch {
      /* 该页无文本层 */
    }
    page.cleanup();
  }

  // 超出 maxPages 的页只抽文本不转图（深度解读 PDF 回退路径需要全文）
  for (let i = n + 1; i <= pageCount; i++) {
    try {
      const page = await doc.getPage(i);
      const tc = await page.getTextContent();
      let pageText = '';
      let pageLines = '';
      for (const item of tc.items) {
        pageText += item.str || '';
        pageLines += item.str || '';
        if (item.hasEOL) {
          pageText += ' ';
          pageLines += '\n';
        }
      }
      textParts.push(pageText);
      lineParts.push(pageLines);
      page.cleanup();
    } catch {
      /* 忽略 */
    }
  }

  return {
    images,
    text: textParts.join('\n').replace(/[ \t]+/g, ' ').trim(),
    textLines: lineParts.join('\n').trim(),
    title,
    author,
    creationDate,
    pageCount,
  };
}
