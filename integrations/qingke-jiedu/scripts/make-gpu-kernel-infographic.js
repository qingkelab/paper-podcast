#!/usr/bin/env node
/**
 * 手绘风格信息图（视觉主导版）：GPU Kernel Agent 的三轴。
 *
 *   node scripts/make-gpu-kernel-infographic.js                  # → output/handdrawn/gpu-kernel-agents.svg|png
 *   node scripts/make-gpu-kernel-infographic.js --svg-only        # 只出 SVG（不需要 Chrome）
 *   node scripts/make-gpu-kernel-infographic.js --font-dir <dir>  # 指向霞鹜文楷 Lite（可选）
 *   node scripts/make-gpu-kernel-infographic.js --width 1600 --height 900
 *
 * 设计原则：**图说话**。四个可视块——方法阶梯、分母阶梯、两张尺子、检查器盲区柱状图——
 * 每个元素只配一句短标签，长解释留给正文。数字依据 L1 Cached Papers 的
 *《GPU Kernel Agent 进展：方法、基准与验证三者缺一不可》。
 *
 * 自包含：手绘原语 / 排版 / 字体子集内联 / 版面自检都在本文件里；栅格化复用本机
 * src/webToImages.js。面板高度按内容实测，避免空白与溢出。
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

/* ------------------------------------------------------------------ *
 * 0. 基础
 * ------------------------------------------------------------------ */

const PALETTE = { paper: '#F4EFE4', ink: '#1F1D1A', inkSoft: '#5A554D', pencil: '#8C857A', blue: '#2F5D8C', red: '#B23A32' };
const FONT_HAND = "'Xingkai SC','Hanzipen SC','LXGW WenKai Lite','Kaiti SC','Songti SC','STSong',serif";
const HAND_FAMILY = 'LXGW WenKai Lite';

function makeJitter(seedStr) {
  let h = 2166136261;
  for (const ch of String(seedStr || '')) {
    h ^= ch.codePointAt(0);
    h = Math.imul(h, 16777619);
  }
  let a = h >>> 0;
  const rand = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  return (amp = 1.4) => (rand() * 2 - 1) * amp;
}

const escapeXml = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' })[c]);

function textWidth(text, size) {
  let units = 0;
  for (const ch of String(text || '')) units += /[\u3000-\u9fff\uff00-\uffef]/.test(ch) ? 1 : 0.55;
  return units * size;
}

function wrapText(text, maxWidth, size) {
  const atoms = String(text || '').match(/[A-Za-z0-9][A-Za-z0-9.,:+/%\-–—×]*|\s+|./gu) || [];
  const lines = [];
  let cur = '';
  const push = () => {
    if (cur.trim()) lines.push(cur.trim());
    cur = '';
  };
  for (const atom of atoms) {
    if (!atom) continue;
    if (/^\s+$/.test(atom)) {
      if (cur) cur += ' ';
      continue;
    }
    if (textWidth(cur + atom, size) <= maxWidth) {
      cur += atom;
      continue;
    }
    push();
    cur = atom;
  }
  push();
  return lines;
}

const stripDecorative = (s) => String(s ?? '').replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}]/gu, '').replace(/\s{2,}/g, ' ').trim();

/* ------------------------------------------------------------------ *
 * 1. 手绘原语
 * ------------------------------------------------------------------ */

function pencilLine(x1, y1, x2, y2, jitter, { color = PALETTE.ink, width = 2, opacity = 1, dash = '' } = {}) {
  return (
    `<line x1="${(x1 + jitter(0.8)).toFixed(1)}" y1="${(y1 + jitter(0.8)).toFixed(1)}" ` +
    `x2="${(x2 + jitter(0.8)).toFixed(1)}" y2="${(y2 + jitter(0.8)).toFixed(1)}" stroke="${color}" ` +
    `stroke-width="${width}" stroke-linecap="round" opacity="${opacity}"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`
  );
}

function pencilRect(x, y, w, h, jitter, opts = {}) {
  return [
    pencilLine(x, y, x + w, y, jitter, opts),
    pencilLine(x + w, y, x + w, y + h, jitter, opts),
    pencilLine(x + w, y + h, x, y + h, jitter, opts),
    pencilLine(x, y + h, x, y, jitter, opts),
  ].join('');
}

/** 填充感：细斜线扫一遍（不是实心色块，保持铅笔稿观感）。 */
function hatch(x, y, w, h, jitter, color, opacity = 0.32) {
  const parts = [];
  for (let i = -h; i < w; i += 7) {
    const x1 = Math.max(x, x + i);
    const y1 = y + Math.max(0, -(i));
    const x2 = Math.min(x + w, x + i + h);
    const y2 = y + Math.min(h, w - i > h ? h : Math.max(0, w - i));
    if (x2 - x1 < 2 || y2 - y1 < 2) continue;
    parts.push(pencilLine(x1, y1, x2, y2, jitter, { color, width: 0.9, opacity }));
  }
  return parts.join('');
}

function paperFrame({ w, h, jitter, fontCss = '' }) {
  const parts = [
    `<defs>${fontCss ? `<style>${fontCss}</style>` : ''}` +
      `<filter id="grain"><feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="3" seed="7"/>` +
      `<feColorMatrix type="saturate" values="0"/>` +
      `<feComponentTransfer><feFuncA type="linear" slope="0.05"/></feComponentTransfer></filter></defs>`,
    `<rect x="0" y="0" width="${w}" height="${h}" fill="${PALETTE.paper}"/>`,
    `<rect x="0" y="0" width="${w}" height="${h}" fill="url(#grain)" opacity="0.5"/>`,
    pencilRect(18, 18, w - 36, h - 36, jitter, { color: PALETTE.pencil, width: 1.1, opacity: 0.65 }),
    pencilRect(25, 25, w - 50, h - 50, jitter, { color: PALETTE.ink, width: 1.4, opacity: 0.5 }),
  ];
  for (const [cx, cy, sx, sy] of [
    [28, 28, 1, 1],
    [w - 28, 28, -1, 1],
    [28, h - 28, 1, -1],
    [w - 28, h - 28, -1, -1],
  ]) {
    parts.push(pencilLine(cx, cy, cx + 26 * sx, cy + 8 * sy, jitter, { color: PALETTE.red, width: 2.2 }));
  }
  return parts.join('');
}

const RECORD = { texts: [], panels: [] };

function textBlock(lines, x, y, size, { color = PALETTE.ink, anchor = 'start', lineHeight = 1.35, weight = 400 } = {}) {
  return lines
    .map((line, i) => {
      const w = textWidth(line, size);
      const left = anchor === 'middle' ? x - w / 2 : anchor === 'end' ? x - w : x;
      RECORD.texts.push({ content: line, box: { x: left, y: y + i * size * lineHeight - size * 0.88, w, h: size * 1.05 } });
      return (
        `<text x="${x}" y="${(y + i * size * lineHeight).toFixed(1)}" font-family="${FONT_HAND}" font-size="${size}" ` +
        `font-weight="${weight}" fill="${color}" text-anchor="${anchor}">${escapeXml(line)}</text>`
      );
    })
    .join('');
}

/** 面板：外框 + 标题，返回内容起始 y。 */
function panel({ x, y, w, h, title, jitter, color = PALETTE.ink, dash = '' }) {
  RECORD.panels.push({ x, y, w, h, title });
  const parts = [pencilRect(x, y, w, h, jitter, { color, width: 1.5, opacity: 0.85, dash })];
  if (title) parts.push(textBlock([title], x + 14, y + 24, 14, { weight: 600 }));
  return { svg: parts.join(''), top: y + (title ? 40 : 14) };
}

const inside = (box, rect, tol = 2) =>
  box.x >= rect.x - tol && box.y >= rect.y - tol && box.x + box.w <= rect.x + rect.w + tol && box.y + box.h <= rect.y + rect.h + tol;

function selfCheck({ width, height }) {
  const findings = [];
  const frame = { x: 18, y: 18, w: width - 36, h: height - 36 };
  for (const t of RECORD.texts) {
    if (!inside(t.box, frame)) {
      findings.push({ level: 'error', code: 'text-out-of-frame', where: t.content.slice(0, 22), message: '越出安全边框' });
    }
    const owners = RECORD.panels.filter(
      (p) => p.x <= t.box.x && t.box.x <= p.x + p.w && p.y <= t.box.y + t.box.h / 2 && t.box.y + t.box.h / 2 <= p.y + p.h,
    );
    if (owners.length) {
      const p = owners.sort((a, b) => a.w * a.h - b.w * b.h)[0];
      if (!inside(t.box, p))
        findings.push({ level: 'error', code: 'panel-overflow', where: t.content.slice(0, 22), message: `撑破面板「${p.title || ''}」` });
    }
    const overlapping = RECORD.texts.filter(
      (o) =>
        o !== t &&
        Math.min(t.box.x + t.box.w, o.box.x + o.box.w) - Math.max(t.box.x, o.box.x) > 2 &&
        Math.min(t.box.y + t.box.h, o.box.y + o.box.h) - Math.max(t.box.y, o.box.y) > 2,
    );
    for (const o of overlapping) {
      if (t.content < o.content)
        findings.push({ level: 'warn', code: 'text-overlap', where: `${t.content.slice(0, 12)} ↔ ${o.content.slice(0, 12)}`, message: '两段文字压在一起' });
    }
    // 风格红线：不用 emoji / dingbat（✓ ✗ 也算），字号不低于 11
    if (/[\u{1F300}-\u{1FAFF}\u{2700}-\u{27BF}]/u.test(t.content)) {
      findings.push({ level: 'error', code: 'emoji-in-text', where: t.content.slice(0, 22), message: '出现 emoji / dingbat（✓✗ 这类符号也不许）' });
    }
    if (t.box.h < 11 - 0.01) {
      findings.push({ level: 'warn', code: 'font-too-small', where: t.content.slice(0, 22), message: `字号 ${t.box.h.toFixed(1)} < 11` });
    }
  }
  return findings;
}

/* ------------------------------------------------------------------ *
 * 2. 字体（可选内联）
 * ------------------------------------------------------------------ */

async function buildFontCss(text, fontDir) {
  if (!fontDir) return '';
  const css = await fs.readFile(path.join(fontDir, 'lxgwwenkailite-regular.css'), 'utf-8').catch(() => '');
  if (!css) return '';
  const faces = [];
  for (const block of css.split('@font-face').slice(1)) {
    const file = (block.match(/url\('([^']+)'\)/) || [])[1];
    const ranges = (block.match(/unicode-range:\s*([^;}]+)/) || [])[1];
    if (!file || !ranges) continue;
    const parsed = [];
    for (const part of ranges.split(',')) {
      const m = part.trim().match(/^U\+([0-9a-fA-F]+)(?:-([0-9a-fA-F]+))?$/);
      if (m) parsed.push([parseInt(m[1], 16), m[2] ? parseInt(m[2], 16) : parseInt(m[1], 16)]);
    }
    faces.push({ file: path.resolve(fontDir, file.replace(/^\.\//, '')), ranges: parsed });
  }
  const codes = [...new Set([...String(text)].map((c) => c.codePointAt(0)))];
  const rules = [];
  for (const face of faces.filter((f) => codes.some((cp) => f.ranges.some(([lo, hi]) => cp >= lo && cp <= hi))).slice(0, 80)) {
    const buf = await fs.readFile(face.file).catch(() => null);
    if (!buf) continue;
    rules.push(
      `@font-face{font-family:'${HAND_FAMILY}';font-style:normal;font-weight:400;font-display:block;` +
        `src:url(data:font/woff2;base64,${buf.toString('base64')}) format('woff2');}`,
    );
  }
  return rules.join('');
}

/* ------------------------------------------------------------------ *
 * 3. 图元：阶梯 / 尺子 / 矩阵 / 横条 —— 信息图的主力
 * ------------------------------------------------------------------ */

/** 方法阶梯：四步上升的台阶，值写台阶上，标签写台阶下。 */
function stepChart({ x, y, w, h, steps, jitter, SZ }) {
  const parts = [];
  const n = steps.length;
  const gap = 10;
  const bw = (w - gap * (n - 1)) / n;
  const baseY = y + h;
  steps.forEach((s, i) => {
    const bh = h * s.h;
    const bx = x + i * (bw + gap);
    const by = baseY - bh;
    const color = s.color || (i >= 2 ? PALETTE.red : PALETTE.blue);
    parts.push(hatch(bx, by, bw, bh, jitter, color, 0.28));
    parts.push(pencilRect(bx, by, bw, bh, jitter, { color, width: 1.8 }));
    parts.push(textBlock([s.value], bx + bw / 2, by - 8, SZ(17), { anchor: 'middle', weight: 700 }));
    parts.push(textBlock([s.label], bx + bw / 2, baseY + 20, SZ(12.5), { anchor: 'middle', weight: 600 }));
    if (s.note) parts.push(textBlock([s.note], bx + bw / 2, baseY + 38, SZ(11), { anchor: 'middle', color: PALETTE.red }));
  });
  parts.push(pencilLine(x - 4, baseY, x + w + 4, baseY, jitter, { color: PALETTE.ink, width: 1.4 }));
  return parts.join('');
}

/** 分母阶梯：T0→T6 七级，越往下基线越硬；右侧用红括号标出“真正值钱的分母”。 */
function tierLadder({ x, y, w, h, tiers, jitter, SZ }) {
  const parts = [];
  const n = tiers.length;
  const rowH = h / n;
  tiers.forEach((t, i) => {
    const ry = y + i * rowH;
    const bw = w * (0.35 + 0.65 * (i / (n - 1)));
    const color = i === 0 ? PALETTE.blue : i === n - 1 ? PALETTE.red : PALETTE.pencil;
    parts.push(pencilRect(x, ry + 3, bw, rowH - 8, jitter, { color, width: 1.5, opacity: 0.9 }));
    if (i >= 4) parts.push(hatch(x, ry + 3, bw, rowH - 8, jitter, color, 0.22));
    parts.push(textBlock([t.label], x + 8, ry + rowH / 2 + SZ(4), SZ(11.5), { color: PALETTE.ink, weight: 600 }));
    parts.push(textBlock([t.hint], x + bw + 8, ry + rowH / 2 + SZ(4), SZ(11.5), { color: PALETTE.inkSoft }));
  });
  parts.push(textBlock(['分母越硬，同一个倍数越值钱'], x, y - 8, SZ(11.5), { color: PALETTE.inkSoft }));
  return parts.join('');
}

/** 尺子：一条横轴 + 刻度标签（标签一律挂在线下并交错两行，避免压字）。 */
function ruler({ x, y, w, title, ticks, jitter, SZ }) {
  const parts = [];
  if (title) parts.push(textBlock([title], x, y - 16, SZ(12.5), { weight: 600 }));
  parts.push(pencilLine(x, y, x + w, y, jitter, { color: PALETTE.ink, width: 1.8 }));
  ticks.forEach((t, i) => {
    const tx = x + w * t.at;
    const ly = y + (i % 2 ? 44 : 24);
    parts.push(pencilLine(tx, y, tx, y + 8, jitter, { color: t.color || PALETTE.pencil, width: 1.6 }));
    parts.push(pencilLine(tx, y + 8, tx, ly - 12, jitter, { color: t.color || PALETTE.pencil, width: 0.9, opacity: 0.45 }));
    const anchor = t.at <= 0.06 ? 'start' : t.at >= 0.94 ? 'end' : 'middle';
    parts.push(
      textBlock([t.label], tx, ly, SZ(11), {
        anchor,
        color: t.color || PALETTE.inkSoft,
        weight: t.weight || 400,
      }),
    );
  });
  return parts.join('');
}

/** 2×2 判定矩阵：fast_p 的双门槛。 */
function matrix2x2({ x, y, size, jitter, SZ }) {
  const parts = [];
  const cells = [
    { r: 0, c: 0, text: '正确', sub: '更快 → 记分', ok: true },
    { r: 0, c: 1, text: '正确', sub: '更慢 → 0', ok: false },
    { r: 1, c: 0, text: '错误', sub: '更快 → 0', ok: false },
    { r: 1, c: 1, text: '错误', sub: '更慢 → 0', ok: false },
  ];
  for (const cell of cells) {
    const cx = x + cell.c * size;
    const cy = y + cell.r * size;
    const color = cell.ok ? PALETTE.blue : PALETTE.pencil;
    if (cell.ok) parts.push(hatch(cx, cy, size, size, jitter, PALETTE.blue, 0.18));
    parts.push(pencilRect(cx, cy, size, size, jitter, { color, width: cell.ok ? 2.2 : 1.4, opacity: cell.ok ? 1 : 0.75 }));
    parts.push(textBlock([cell.text], cx + size / 2, cy + size / 2 - SZ(2), SZ(12.5), { anchor: 'middle', weight: 600 }));
    parts.push(textBlock([cell.sub], cx + size / 2, cy + size / 2 + SZ(16), SZ(11), { anchor: 'middle', color: cell.ok ? PALETTE.blue : PALETTE.inkSoft }));
    // 手绘记号：记分的格子画对勾，其余画小叉（不用 ✓/✗ 字符）
    const mx = cx + size - SZ(26);
    const my = cy + SZ(20);
    if (cell.ok) {
      parts.push(pencilLine(mx, my, mx + SZ(7), my + SZ(9), jitter, { color: PALETTE.blue, width: 2.4 }));
      parts.push(pencilLine(mx + SZ(7), my + SZ(9), mx + SZ(20), my - SZ(10), jitter, { color: PALETTE.blue, width: 2.4 }));
    } else {
      parts.push(pencilLine(mx, my - SZ(6), mx + SZ(14), my + SZ(8), jitter, { color: PALETTE.pencil, width: 1.8 }));
      parts.push(pencilLine(mx + SZ(14), my - SZ(6), mx, my + SZ(8), jitter, { color: PALETTE.pencil, width: 1.8 }));
    }
  }
  return parts.join('');
}

/** 横条组：label + 条 + 数值。 */
function barRows({ x, y, w, rows, jitter, SZ, labelW, max, rowH = 30, barH = 14, unit = '' }) {
  const parts = [];
  rows.forEach((row, i) => {
    const cy = y + i * rowH;
    const bw = Math.max(8, w * (row.v / max));
    const color = row.color || PALETTE.blue;
    parts.push(textBlock([row.label], x, cy + SZ(6), SZ(11.5), { color: PALETTE.inkSoft }));
    parts.push(hatch(x + labelW, cy - barH / 2, bw, barH, jitter, color, 0.25));
    parts.push(pencilRect(x + labelW, cy - barH / 2, bw, barH, jitter, { color, width: 1.7 }));
    parts.push(
      textBlock([`${row.text || row.v}${unit}`], x + labelW + bw + 8, cy + SZ(6), SZ(11.5), { weight: 600, color: PALETTE.ink }),
    );
  });
  return parts.join('');
}

/* ------------------------------------------------------------------ *
 * 4. 版面：1200×1600，四个可视块 + 底部四问
 * ------------------------------------------------------------------ */

const TEXT = {
  meta: '手绘速记 · GPU Kernel Agent',
  metaRight: '青稞解读 · 一图看懂',
  title: 'Kernel Agent 的三轴地图',
  subtitle: '同一个加速比，换个分母、换个检查器，含义完全不同。',
  footer: '手绘速记：依据 L1 Cached Papers《GPU Kernel Agent 进展》重画，数字均回查原文。',
  source: 'l1cachedell.github.io/blog/hpc/gpu-kernel-agents-methods-benchmarks-verification',
  a1: '① 方法轴 · 信号从哪来',
  a2: '② 基准轴 · 分母是谁',
  b1: 'fast_p：一张考卷两个门槛',
  b2: '两道尺子，两个结论',
  c1: '③ 验证轴 · 检查器看得见吗',
  c2: '作弊的量级',
  levels: '越像真实工作，收益掉得越快（CUDA Agent）',
  checks: '看结果先拆四件事',
};

const STEPS = [
  { label: 'SFT', value: '17%', h: 0.3 },
  { label: '多轮 RL', value: '1.10×', h: 0.46 },
  { label: '对比 RL', value: '88.4%', h: 0.72, note: 'Top-10 没真调 kernel' },
  { label: 'agentic RL', value: '98.8%', h: 1, note: '2.11×' },
];

const TIERS = [
  { label: 'T0 参考实现', hint: 'PyTorch eager' },
  { label: 'T1 编译器', hint: '编译生成' },
  { label: 'T2 厂商库', hint: 'cuBLAS / cuDNN' },
  { label: 'T3 专家实现', hint: '专用实现' },
  { label: 'T4 生产实现', hint: '线上在跑' },
  { label: 'T5 最强已知', hint: '外部验证' },
  { label: 'T6 硬件上限', hint: 'speed of light' },
];

const LEVEL_ROWS = [
  { label: 'Overall', v: 2.11, text: '2.11×' },
  { label: 'L2 融合', v: 2.8, text: '2.80×' },
  { label: 'L3 整模型', v: 1.52, text: '1.52×', color: PALETTE.red },
];

const MUTATION_ROWS = [
  { label: '算术', v: 8.7, text: '8.7%' },
  { label: '边界', v: 22.9, text: '22.9%' },
  { label: '同步', v: 27.8, text: '27.8%' },
  { label: '精度', v: 78.6, text: '78.6%', color: PALETTE.red },
];

const HACK_ROWS = [
  { label: '计时漏洞', v: 32.8, text: '32.8%', color: PALETTE.red },
  { label: '精度降级', v: 14.5, text: '14.5%' },
  { label: '执行级检查后', v: 3, text: '3%', color: PALETTE.blue },
];

const CHECKS = [
  { n: '①', t: '任务类型', d: '单算子 / 融合 / 整模型' },
  { n: '②', t: '加速比的分母', d: 'T0 / T4 / T6' },
  { n: '③', t: '硬件口径', d: 'GPU · 精度 · shape' },
  { n: '④', t: '检查器强度', d: 'kill 率 / 误杀率' },
];

export function renderInfographicSvg({ width = 1200, height = 1600, fontCss = '' } = {}) {
  const W = width;
  const H = height;
  const sx = W / 1200;
  const sy = H / 1600;
  const X = (v) => v * sx;
  const Y = (v) => v * sy;
  const SZ = (v) => v * Math.min(sx, sy);
  const jitter = makeJitter(`kernel-agents-visual|${W}x${H}|v1`);
  RECORD.texts.length = 0;
  RECORD.panels.length = 0;
  const parts = [paperFrame({ w: W, h: H, jitter, fontCss })];
  const pad = 56;

  // 页眉
  parts.push(textBlock([TEXT.meta], X(pad), Y(56), SZ(13), { color: PALETTE.inkSoft }));
  parts.push(textBlock([TEXT.metaRight], X(1144), Y(56), SZ(13), { color: PALETTE.inkSoft, anchor: 'end' }));
  parts.push(textBlock([TEXT.title], X(pad), Y(106), SZ(34), { weight: 700 }));
  parts.push(pencilLine(X(pad), Y(122), X(372), Y(122), jitter, { color: PALETTE.red, width: 2.6 }));
  parts.push(textBlock([TEXT.subtitle], X(pad), Y(154), SZ(15), { color: PALETTE.inkSoft }));

  // 第一行：方法阶梯 ｜ 分母阶梯
  const rowA = { y: 186, h: 372 };
  {
    const x = X(pad);
    const w = X(528);
    const box = panel({ x, y: Y(rowA.y), w, h: Y(rowA.h), title: TEXT.a1, jitter });
    parts.push(box.svg);
    parts.push(stepChart({ x: x + X(28), y: box.top + Y(18), w: w - X(56), h: Y(218), steps: STEPS, jitter, SZ }));
    parts.push(
      textBlock(['signal：编译错误 → 执行反馈 → profiling → agent 轨迹'], x + X(28), box.top + Y(300), SZ(12), {
        color: PALETTE.inkSoft,
      }),
    );
    parts.push(textBlock(['台阶高度为示意：四个数字单位不同，不能横向比大小'], x + X(28), box.top + Y(322), SZ(11), { color: PALETTE.pencil }));
  }
  {
    const x = X(616);
    const w = X(528);
    const box = panel({ x, y: Y(rowA.y), w, h: Y(rowA.h), title: TEXT.a2, jitter });
    parts.push(box.svg);
    parts.push(tierLadder({ x: x + X(28), y: box.top + Y(26), w: w - X(150), h: Y(250), tiers: TIERS, jitter, SZ }));
    parts.push(
      textBlock(['10× vs T0', '＜ 1.1× vs T4'], x + w - X(126), box.top + Y(120), SZ(12.5), { weight: 600, color: PALETTE.red }),
    );
  }

  // 第二行：fast_p 双门槛 ｜ 两道尺子
  const rowB = { y: 578, h: 372 };
  {
    const x = X(pad);
    const w = X(528);
    const box = panel({ x, y: Y(rowB.y), w, h: Y(rowB.h), title: TEXT.b1, jitter });
    parts.push(box.svg);
    parts.push(matrix2x2({ x: x + X(34), y: box.top + Y(16), size: X(104), jitter, SZ }));
    parts.push(
      textBlock(['250 题 = L1 100 / L2 100 / L3 50'], x + X(34), box.top + Y(32) + Y(216), SZ(12), { color: PALETTE.inkSoft }),
    );
    parts.push(
      textBlock(['one-shot <20% 追平 eager'], x + X(290), box.top + Y(40), SZ(12), { color: PALETTE.inkSoft }),
    );
    parts.push(textBlock(['R1 + 反馈：36% → 72%'], x + X(290), box.top + Y(64), SZ(12), { weight: 600, color: PALETTE.blue }));
    parts.push(textBlock(['p = 0 就是纯正确率'], x + X(290), box.top + Y(96), SZ(12), { color: PALETTE.inkSoft }));
  }
  {
    const x = X(616);
    const w = X(528);
    const box = panel({ x, y: Y(rowB.y), w, h: Y(rowB.h), title: TEXT.b2, jitter });
    parts.push(box.svg);
    parts.push(
      ruler({
        x: x + X(40), y: box.top + Y(46), w: w - X(80), title: '加速比（相对不同基线）',
        ticks: [
          { at: 0.16, label: 'GQA 0.84×', color: PALETTE.blue },
          { at: 0.42, label: 'Cursor 1.38×' },
          { at: 0.64, label: 'CUDA Agent 2.11×' },
          { at: 0.92, label: 'CUDA-L1 3.12×', color: PALETTE.red },
        ], jitter, SZ,
      }),
    );
    parts.push(
      ruler({
        x: x + X(40), y: box.top + Y(150), w: w - X(80), title: 'SOL 分数（0.5 软基线 → 1.0 硬件上限）',
        ticks: [
          { at: 0.05, label: '0.5', color: PALETTE.red, weight: 600 },
          { at: 0.12, label: 'Cursor 0.56' },
          { at: 0.464, label: 'agent 0.732', color: PALETTE.blue, weight: 600 },
          { at: 0.944, label: 'GQA 0.9722', color: PALETTE.blue },
          { at: 1, label: '1.0', color: PALETTE.red, weight: 600 },
        ], jitter, SZ,
      }),
    );
    parts.push(
      textBlock(['加速比与离上限的距离几乎无关：r = 0.10'], x + X(40), box.top + Y(250), SZ(12.5), { weight: 600, color: PALETTE.red }),
    );
    parts.push(textBlock(['CUDA Agent 加速比：越像真实工作，收益掉得越快'], x + X(40), box.top + Y(280), SZ(11.5), { color: PALETTE.inkSoft }));
    parts.push(
      barRows({
        x: x + X(40),
        y: box.top + Y(300),
        w: w - X(170),
        rows: LEVEL_ROWS,
        jitter,
        SZ,
        labelW: X(74),
        max: 3,
        rowH: Y(24),
        barH: Y(10),
      }),
    );
  }

  // 第三行：检查器盲区 ｜ 作弊量级
  const rowC = { y: 970, h: 318 };
  {
    const x = X(pad);
    const w = X(528);
    const box = panel({ x, y: Y(rowC.y), w, h: Y(rowC.h), title: TEXT.c1, jitter });
    parts.push(box.svg);
    parts.push(textBlock(['官方检查器只杀死 83.1%'], x + X(28), box.top + Y(18), SZ(15), { weight: 700, color: PALETTE.red }));
    parts.push(
      barRows({
        x: x + X(28), y: box.top + Y(66), w: w - X(150), rows: MUTATION_ROWS, jitter, SZ,
        labelW: X(52), max: 100, rowH: Y(40), barH: Y(16),
      }),
    );
    parts.push(textBlock(['各类可证实错误的漏检率'], x + X(28), box.top + Y(238), SZ(11.5), { color: PALETTE.inkSoft }));
  }
  {
    const x = X(616);
    const w = X(528);
    const box = panel({ x, y: Y(rowC.y), w, h: Y(rowC.h), title: TEXT.c2, jitter });
    parts.push(box.svg);
    parts.push(
      barRows({
        x: x + X(28), y: box.top + Y(40), w: w - X(190), rows: HACK_ROWS, jitter, SZ,
        labelW: X(96), max: 40, rowH: Y(48), barH: Y(18),
      }),
    );
    parts.push(textBlock(['CUDA-L1 早期虚报可达 18×'], x + X(28), box.top + Y(212), SZ(12), { color: PALETTE.red }));
    parts.push(textBlock(['四层防御：沙箱 / 执行级 check / 输入随机化 / LLM 审计'], x + X(28), box.top + Y(238), SZ(11.5), { color: PALETTE.inkSoft }));
  }

  // 底部：四问
  parts.push(pencilLine(X(pad), Y(1316), X(1144), Y(1316), jitter, { color: PALETTE.pencil, width: 1 }));
  parts.push(textBlock([TEXT.checks], X(pad), Y(1342), SZ(14), { weight: 600 }));
  CHECKS.forEach((c, i) => {
    const cx = X(pad + i * (254 + 24));
    const w = X(254);
    parts.push(pencilRect(cx, Y(1358), w, Y(120), jitter, { color: PALETTE.ink, width: 1.5, opacity: 0.85 }));
    parts.push(textBlock([`${c.n} ${c.t}`], cx + X(14), Y(1386), SZ(14), { weight: 600 }));
    parts.push(textBlock([c.d], cx + X(14), Y(1412), SZ(12), { color: PALETTE.inkSoft }));
  });

  // 页脚
  parts.push(pencilLine(X(pad), Y(1506), X(1144), Y(1506), jitter, { color: PALETTE.pencil, width: 1 }));
  parts.push(textBlock([TEXT.footer], X(pad), Y(1530), SZ(11.5), { color: PALETTE.inkSoft }));
  parts.push(textBlock([TEXT.source], X(1144), Y(1552), SZ(11), { color: PALETTE.pencil, anchor: 'end' }));

  return {
    svg: `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">${parts.join('')}</svg>`,
    findings: selfCheck({ width: W, height: H }),
  };
}

/* ------------------------------------------------------------------ *
 * 5. CLI
 * ------------------------------------------------------------------ */

export function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (!a.startsWith('--')) {
      args._.push(a);
      continue;
    }
    const key = a.slice(2);
    const next = argv[i + 1];
    if (next && !next.startsWith('--')) {
      args[key] = next;
      i += 1;
    } else {
      args[key] = true;
    }
  }
  return args;
}

export async function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  const width = Number(args.width || 1200);
  const height = Number(args.height || 1600);
  const scale = Number(args.scale || 2);
  const outDir = path.resolve(args.out || path.join(ROOT, 'output/handdrawn'));
  const name = String(args.name || 'gpu-kernel-agents');
  const repoFontDir = path.join(ROOT, 'assets/fonts/lxgw-wenkai-lite');
  const fontDir = args['no-font'] ? '' : args['font-dir'] || (await fs.stat(repoFontDir).then(() => repoFontDir).catch(() => ''));
  const allText = [TEXT, STEPS, TIERS, LEVEL_ROWS, MUTATION_ROWS, HACK_ROWS, CHECKS].flat().map((v) => (typeof v === 'string' ? v : JSON.stringify(v))).join('｜');
  const fontCss = await buildFontCss(allText, fontDir);
  const built = renderInfographicSvg({ width, height, fontCss });
  await fs.mkdir(outDir, { recursive: true });
  const svgPath = path.join(outDir, `${name}.svg`);
  await fs.writeFile(svgPath, built.svg, 'utf-8');
  console.log(`· SVG ${svgPath}（${width}×${height}${fontCss ? '，已内嵌手写体' : '，系统字体栈'}）`);
  const errors = built.findings.filter((f) => f.level === 'error');
  if (built.findings.length) {
    console.log(`· 版面自检：${errors.length} 错误 / ${built.findings.length - errors.length} 警告`);
    for (const f of built.findings.slice(0, 12)) console.log(`    ✗ [${f.code}] ${f.where}`);
  } else {
    console.log('· 版面自检：通过（无越界 / 无压字 / 无撑破面板）');
  }
  if (args['svg-only']) return errors.length ? 1 : 0;

  const { svgToPng, closeBrowser } = await import('../src/webToImages.js');
  try {
    const res = await svgToPng(built.svg, { scale, waitForFonts: true, timeoutMs: 60000 });
    if (!res?.buffer) {
      console.error('· PNG 栅格化失败（本机 Chrome 不可用？），已保留 SVG');
      return 1;
    }
    const pngPath = path.join(outDir, `${name}.png`);
    await fs.writeFile(pngPath, res.buffer);
    console.log(`· PNG ${pngPath}（${res.width}×${res.height}）`);
  } finally {
    await closeBrowser().catch(() => {});
  }
  return errors.length ? 1 : 0;
}

const invokedDirectly = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  main()
    .then((code) => process.exit(code ?? 0))
    .catch((err) => {
      console.error('[infographic] 失败：', (err && err.message) || err);
      process.exit(1);
    });
}
