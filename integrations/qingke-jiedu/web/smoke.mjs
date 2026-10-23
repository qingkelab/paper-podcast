// 浏览器版（web/）端到端冒烟：真实 Chrome + 反节流参数，验证 PDF→图片→文案准备 链路。
// 用法：node /tmp/web-smoke.mjs   （需要先在本仓库根目录起静态服务：python3 -m http.server 8080）
import puppeteer from 'puppeteer-core';

const CHROME = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = process.env.WEB_BASE || 'http://127.0.0.1:8080/web/';
const PDF_URL = process.env.PDF_URL || 'https://arxiv.org/pdf/1706.03762';

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: [
    '--no-sandbox',
    '--disable-gpu',
    '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding',
    '--disable-backgrounding-occluded-windows',
    '--window-size=1400,900',
  ],
});

const page = await browser.newPage();
await page.setViewport({ width: 1400, height: 900 });
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message.slice(0, 200)));

try {
  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 });
  const visible = await page.evaluate(() => ({
    state: document.visibilityState,
    rAF: typeof requestAnimationFrame === 'function',
  }));
  console.log('页面可见性:', JSON.stringify(visible));

  await page.waitForSelector('#url', { timeout: 15000 });
  await page.$eval('#url', (el, v) => { el.value = v; }, PDF_URL);
  await page.click('#submit');

  // 等到图库出现图片（pdf.js 逐页渲染）
  await page.waitForFunction(() => document.querySelectorAll('#gallery .tile').length > 0, { timeout: 180000, polling: 1000 });
  await page.waitForFunction(() => document.querySelector('#status').hidden, { timeout: 180000, polling: 1000 }).catch(() => {});

  const result = await page.evaluate(() => {
    const checks = document.querySelectorAll('#gallery .tile-check');
    document.querySelector('#sel-all')?.click();
    const afterAll = {
      label: document.querySelector('#sel-count')?.textContent || '',
      zipSelDisabled: document.querySelector('#zip-sel-btn')?.disabled,
      selectedTiles: document.querySelectorAll('#gallery .tile.selected').length,
    };
    document.querySelector('#sel-clear')?.click();
    const afterClear = {
      hidden: document.querySelector('#sel-count')?.hidden,
      zipSelDisabled: document.querySelector('#zip-sel-btn')?.disabled,
    };
    return {
      tiles: document.querySelectorAll('#gallery .tile').length,
      checkboxes: checks.length,
      selection: { afterAll, afterClear },
      syncButtons: ['#sync-wechat-browser', '#sync-copy', '#sync-x'].filter((id) => !!document.querySelector(id)).length,
      imgCount: document.querySelector('#img-count')?.textContent || '',
      metaTitle: document.querySelector('#meta-title')?.textContent || '',
      copyNotice: document.querySelector('#copy-notice-text')?.textContent?.slice(0, 80) || '',
      errorShown: document.querySelector('#error')?.hidden ? '' : document.querySelector('#error').textContent.slice(0, 200),
    };
  });
  console.log('结果:', JSON.stringify(result, null, 1));
  console.log(errors.length ? '控制台错误:\n - ' + errors.join('\n - ') : '控制台错误: 无');
  const ok =
    result.tiles > 0 &&
    result.checkboxes === result.tiles &&
    result.selection.afterAll.selectedTiles === result.tiles &&
    result.selection.afterAll.zipSelDisabled === false &&
    result.selection.afterClear.zipSelDisabled === true &&
    result.syncButtons === 3 &&
    !result.errorShown;
  console.log(ok ? '✅ 冒烟通过' : '❌ 冒烟失败');
  process.exitCode = ok ? 0 : 1;
} catch (err) {
  console.log('❌ 异常:', err.message);
  console.log(errors.length ? '控制台错误:\n - ' + errors.join('\n - ') : '控制台错误: 无');
  process.exitCode = 1;
} finally {
  await browser.close();
}
