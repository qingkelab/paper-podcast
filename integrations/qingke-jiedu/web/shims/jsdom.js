/**
 * jsdom 的浏览器垫片：用原生 DOMParser 提供 `new JSDOM(html).window.document`。
 * 只覆盖 src/deepread/chunker.js 与 src/arxivHtml.js 用到的最小面。
 */
export class JSDOM {
  constructor(html = '') {
    const document = new DOMParser().parseFromString(String(html), 'text/html');
    this.window = { document };
  }
}
