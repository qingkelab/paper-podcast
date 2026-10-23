import puppeteer from 'puppeteer-core';
import fs from 'node:fs/promises';
import { config } from './config.js';

let browserPromise = null;

async function getBrowser() {
  if (!browserPromise) {
    browserPromise = puppeteer
      .launch({
        executablePath: config.chromePath,
        headless: true,
        timeout: 30000, // 启动卡住时直接失败，避免调用方无限等待
        args: [
          '--no-sandbox',
          '--disable-gpu',
          '--disable-dev-shm-usage',
          '--hide-scrollbars',
          '--force-device-scale-factor=1',
        ],
      })
      .catch((err) => {
        browserPromise = null;
        throw err;
      });
  }
  return browserPromise;
}

/**
 * 将网页渲染为全页截图（超长页面按 config.webSegmentHeight 分段）。
 * @returns {Promise<Array<{pageNumber:number, width:number, height:number, buffer:Buffer, label:string}>>}
 */
export async function webToImages(url) {
  const browser = await getBrowser();
  const page = await browser.newPage();
  try {
    await page.setViewport({
      width: config.webViewportWidth,
      height: 900,
      deviceScaleFactor: config.webDeviceScale,
    });
    await page.goto(url, { waitUntil: 'networkidle2', timeout: config.fetchTimeoutMs });
    // 等待字体与懒加载稳定
    await page.evaluate(async () => {
      await new Promise((r) => setTimeout(r, 500));
    });

    const metrics = await page.evaluate(() => {
      const h = Math.max(
        document.body ? document.body.scrollHeight : 0,
        document.documentElement ? document.documentElement.scrollHeight : 0,
      );
      const w = Math.max(
        document.body ? document.body.scrollWidth : 0,
        document.documentElement ? document.documentElement.scrollWidth : 0,
        window.innerWidth,
      );
      return { height: h, width: w };
    });

    const width = Math.min(metrics.width, config.webViewportWidth);
    const height = Math.max(metrics.height, 900);
    const seg = Math.floor(config.webSegmentHeight / config.webDeviceScale);

    const images = [];
    let idx = 0;
    for (let y = 0; y < height; y += seg) {
      const h = Math.min(seg, height - y);
      idx += 1;
      const buf = await page.screenshot({
        clip: { x: 0, y, width, height: h },
      });
      images.push({
        pageNumber: idx,
        width: Math.round(width * config.webDeviceScale),
        height: Math.round(h * config.webDeviceScale),
        buffer: buf,
        label: height > seg ? `第 ${idx} 段` : '全页',
      });
    }
    return images;
  } finally {
    await page.close();
  }
}

/**
 * 把 SVG（论文 HTML 里的矢量图常见形态）渲染成 PNG 位图。
 * 公众号素材 / 预览只吃位图，SVG 必须栅格化。
 * @param {string} svgMarkup 或 svgPath
 * @param {{svgPath?: string, scale?: number, waitForFonts?: boolean}} [opts]
 */
export async function svgToPng(input, opts = {}) {
  const timeoutMs = Number(opts.timeoutMs || 25000);
  const markup = opts.svgPath
    ? await fs.readFile(opts.svgPath, 'utf-8')
    : String(input || '');
  const scale = opts.scale || 2;
  // 单张 SVG 栅格化加超时：个别超大/异常图会让 Chrome 卡住（实测会挂住整轮取源）
  let timer;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('svg 栅格化超时')), timeoutMs);
  });
  return Promise.race([renderSvg(), guard]).finally(() => clearTimeout(timer));

  async function renderSvg() {
    const browser = await getBrowser();
    const page = await browser.newPage();
    page.setDefaultTimeout(timeoutMs);
    try {
      await page.setViewport({ width: 1600, height: 1200, deviceScaleFactor: scale });
      await page.setContent(
        `<!doctype html><html><body style="margin:0;padding:0;background:#fff">${markup}</body></html>`,
        { waitUntil: 'load', timeout: timeoutMs },
      );
      // 内嵌 data: 字体（头图的手写体）解码是异步的：不等它，截图可能拿到回退字体甚至空字
      if (opts.waitForFonts) {
        await page.evaluate(() => (document.fonts ? document.fonts.ready : Promise.resolve())).catch(() => {});
      }
      const box = await page.evaluate(() => {
        const el = document.querySelector('svg');
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { width: Math.ceil(r.width || 0), height: Math.ceil(r.height || 0) };
      });
      if (!box || !box.width || !box.height) return null;
      const buf = await page.screenshot({
        clip: { x: 0, y: 0, width: box.width, height: box.height },
        captureBeyondViewport: true,
      });
      return { buffer: buf, width: box.width * scale, height: box.height * scale };
    } catch {
      return null;
    } finally {
      await page.close().catch(() => {});
    }
  }
}

/** 关闭浏览器（进程退出时调用） */
export async function closeBrowser() {
  if (browserPromise) {
    try {
      const b = await browserPromise;
      await b.close();
    } catch {
      /* ignore */
    }
    browserPromise = null;
  }
}
