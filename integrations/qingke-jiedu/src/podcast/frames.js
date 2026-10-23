import { createCanvas, loadImage } from '@napi-rs/canvas';
import fs from 'node:fs/promises';
import path from 'node:path';
import { config } from '../config.js';

const W = config.videoWidth;
const H = config.videoHeight;
const BRAND = '青稞Talk · 论文播客';
const ACCENT = '#ffd678';

async function loadAny(p) {
  try {
    return await loadImage(p);
  } catch (err) {
    throw new Error(`图片加载失败 ${p}: ${err.message}`);
  }
}

/** 渐变底画布。 */
function gradientCanvas(top, bottom) {
  const c = createCanvas(W, H);
  const x = c.getContext('2d');
  const g = x.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, top);
  g.addColorStop(1, bottom);
  x.fillStyle = g;
  x.fillRect(0, 0, W, H);
  return { c, x };
}

/** 简易高斯模糊：把小图放大绘制到 canvas（图像平滑 → 天然模糊）。 */
function blurredBackdrop(img, dim = 0.5) {
  const tmp = createCanvas(96, 54);
  const tx = tmp.getContext('2d');
  tx.imageSmoothingEnabled = true;
  tx.drawImage(img, 0, 0, 96, 54);
  const { c, x } = gradientCanvas('#0c0f1a', '#0c0f1a');
  x.globalAlpha = dim;
  x.imageSmoothingEnabled = true;
  // 放大 20 倍铺满 → 粗颗粒近似模糊背景
  x.drawImage(tmp, 0, 0, W, H);
  x.globalAlpha = 1;
  return c;
}

function centerContain(x, img, ratio = 0.86) {
  const maxW = W * (ratio > 0.8 ? 0.86 : 0.9);
  const maxH = H * (ratio <= 0.8 ? 0.9 : 0.86);
  const r = Math.min(maxW / img.width, maxH / img.height, 1);
  const dw = Math.max(1, Math.floor(img.width * r));
  const dh = Math.max(1, Math.floor(img.height * r));
  const dx = (W - dw) >> 1;
  const dy = (H - dh) >> 1;
  x.drawImage(img, dx, dy, dw, dh);
  return { dx, dy, dw, dh };
}

function wrapByWidth(x, text, font, maxW) {
  const chars = String(text || '').split('');
  const lines = [];
  let line = '';
  x.font = font;
  for (const ch of chars) {
    if (x.measureText(line + ch).width > maxW && line) {
      lines.push(line);
      line = ch;
    } else {
      line += ch;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function roundedRect(x, rx, ry, rw, rh, r, fill) {
  x.beginPath();
  x.moveTo(rx + r, ry);
  x.arcTo(rx + rw, ry, rx + rw, ry + rh, r);
  x.arcTo(rx + rw, ry + rh, rx, ry + rh, r);
  x.arcTo(rx, ry + rh, rx, ry, r);
  x.arcTo(rx, ry, rx + rw, ry, r);
  x.closePath();
  x.fillStyle = fill;
  x.fill();
}

/** 标题卡（无 PDF 首页兜底 / 结尾卡同构）。 */
export function makeTitleCard(title) {
  const { c, x } = gradientCanvas('#0c0f1a', '#461860');
  x.font = '900 150px "PingFang SC", sans-serif';
  x.textAlign = 'center';
  x.textBaseline = 'middle';
  x.fillText('🎙️', W / 2, H * 0.22);
  x.font = '600 44px "PingFang SC", sans-serif';
  x.fillStyle = ACCENT;
  x.fillText(BRAND, W / 2, H * 0.32);
  const lines = wrapByWidth(x, title, '700 76px "PingFang SC", sans-serif', W * 0.84);
  x.font = '700 76px "PingFang SC", sans-serif';
  x.fillStyle = '#fafafc';
  let ty = H * 0.52 - (lines.length - 1) * 45;
  for (const line of lines.slice(0, 3)) {
    x.fillText(line, W / 2, ty);
    ty += 95;
  }
  x.font = '400 34px "PingFang SC", sans-serif';
  x.fillStyle = '#bec6dc';
  x.fillText('第一人称 · 三分钟读懂一篇论文', W / 2, H * 0.86);
  return c;
}

/** 开场封面：论文首页（模糊放大为背景 + 清晰首页居中 + 顶部品牌条 + 底部标题）。 */
export async function makeCoverCard(coverPath, title) {
  const page = await loadAny(coverPath);
  const img = blurredBackdrop(page, 0.55);
  const x = img.getContext('2d');
  const maxW = W * 0.78;
  const maxH = H * 0.78;
  const r = Math.min(maxW / page.width, maxH / page.height);
  const dw = Math.floor(page.width * r);
  const dh = Math.floor(page.height * r);
  x.drawImage(page, (W - dw) >> 1, (H - dh) >> 1, dw, dh);

  roundedRect(x, W / 2 - 240, 28, 480, 66, 24, 'rgba(0,0,0,0.72)');
  x.font = '600 32px "PingFang SC", sans-serif';
  x.textAlign = 'center';
  x.textBaseline = 'middle';
  x.fillStyle = ACCENT;
  x.fillText(BRAND, W / 2, 62);

  x.fillStyle = 'rgba(0,0,0,0.66)';
  x.fillRect(0, H - 210, W, 210);
  const lines = wrapByWidth(x, title, '600 52px "PingFang SC", sans-serif', W * 0.84);
  x.font = '600 52px "PingFang SC", sans-serif';
  x.fillStyle = '#ffffff';
  let ty = H - 120 - (lines.length - 1) * 40;
  for (const line of lines.slice(0, 2)) {
    x.fillText(line, W / 2, ty);
    ty += 80;
  }
  return img;
}

/** 论文页/无图场景：模糊放大页为背景 + 清晰页居中（不带文字）。 */
export async function makePageCard(pagePath) {
  const page = await loadAny(pagePath);
  const img = blurredBackdrop(page, 0.5);
  const x = img.getContext('2d');
  centerContain(x, page, 0.85);
  return img;
}

/** 图场景：渐变底 + 图片 contain 居中。 */
export async function makeFigureFrame(figPath) {
  const fig = await loadAny(figPath);
  const { c, x } = gradientCanvas('#0d1117', '#1a2436');
  centerContain(x, fig, 0.86);
  return c;
}

/** 结尾致谢卡。 */
export async function makeEndCard(backdropPath) {
  let img;
  if (backdropPath) {
    try {
      img = blurredBackdrop(await loadAny(backdropPath), 0.4);
    } catch {
      img = null;
    }
  }
  if (!img) {
    img = gradientCanvas('#0a241a', '#103c34').c;
  }
  const x = img.getContext('2d');
  roundedRect(x, W / 2 - 330, H * 0.40 - 60, 660, 330, 30, 'rgba(8,10,16,0.78)');
  x.textAlign = 'center';
  x.textBaseline = 'middle';
  x.font = '900 80px "PingFang SC", sans-serif';
  x.fillStyle = '#ffffff';
  x.fillText('🏁', W / 2, H * 0.46);
  x.fillText('谢谢观看', W / 2, H * 0.58);
  x.font = '400 38px "PingFang SC", sans-serif';
  x.fillStyle = '#9aa6c0';
  x.fillText('我们下期再见 · 青稞Talk', W / 2, H * 0.70);
  return img;
}

function savePng(c, file) {
  return fs.writeFile(file, c.toBuffer('image/png'));
}

/**
 * 为每个场景准备静态画面（1920×1080 PNG），写 scene.visual。
 * ctx: { title, pages: string[], figures: [{num,caption,path}], coverPath? }
 * scene.figure = 图编号|null；scene._isFirst/_isLast 由调用方标记。
 */
export async function prepareVisuals(scenes, ctx, workDir) {
  const visDir = path.join(workDir, 'visuals');
  await fs.mkdir(visDir, { recursive: true });
  const figByNum = new Map(ctx.figures.map((f) => [f.num, f]));
  const n = scenes.length;
  for (let i = 0; i < n; i++) {
    const s = scenes[i];
    const isFirst = i === 0;
    const isLast = i === n - 1 && s.figure == null;
    const file = path.join(visDir, `scene_${String(i).padStart(2, '0')}.png`);
    let canvas = null;
    if (isFirst) {
      const title = s.title || ctx.title || '论文播客';
      canvas = ctx.coverPath ? await makeCoverCard(ctx.coverPath, title) : makeTitleCard(title);
    } else if (isLast) {
      canvas = await makeEndCard(ctx.pages[0] || (figByNum.size ? [...figByNum.values()][0].path : null));
    } else {
      const fig = s.figure != null ? figByNum.get(s.figure) : null;
      if (fig && fig.path) {
        canvas = await makeFigureFrame(fig.path);
      } else if (ctx.pages.length) {
        canvas = await makePageCard(ctx.pages[Math.min(i - 1, ctx.pages.length - 1)]);
      } else {
        canvas = makeTitleCard(s.narration.slice(0, 40) || '青稞Talk');
      }
    }
    await savePng(canvas, file);
    s.visual = file;
  }
  return scenes;
}
