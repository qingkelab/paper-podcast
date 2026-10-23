// HTML → TeX → PDF 三级回退 + 结构化切片（用假 fetch，不联网）
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const tmpOut = fs.mkdtempSync(path.join(os.tmpdir(), 'qkj-deepread-'));
process.env.OUTPUT_DIR = tmpOut;

import test from 'node:test';
import assert from 'node:assert/strict';
import { paperHtml, paperTex, minimalPdf } from './fixtures.js';

const { fetchArxivSource } = await import('../src/arxivSource.js');
const { fetchArxivHtml } = await import('../src/arxivHtml.js');

// 每个用例用不同 arXiv id：源码/图片会缓存到 output/_arxivsrc/<id>/，避免互相命中缓存
const HTML_URL = 'https://arxiv.org/abs/2601.00001';
const TEX_URL = 'https://arxiv.org/abs/2601.00002';
const PDF_URL = 'https://arxiv.org/abs/2601.00003';

function mockFetch(routes) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url) => {
    const u = String(url);
    calls.push(u);
    const hit = routes.find((r) => r.match.test(u));
    if (!hit) {
      return { ok: false, status: 404, async text() { return 'not found'; }, async arrayBuffer() { return new ArrayBuffer(0); } };
    }
    return {
      ok: true,
      status: 200,
      url: u,
      headers: new Map(),
      async text() { return hit.text || ''; },
      async arrayBuffer() { return hit.buffer ? hit.buffer.buffer.slice(hit.buffer.byteOffset, hit.buffer.byteOffset + hit.buffer.byteLength) : new ArrayBuffer(0); },
    };
  };
  return { calls, restore: () => { globalThis.fetch = original; } };
}

test('HTML 可用时：取 HTML 正文并产出结构化切片', async () => {
  const mock = mockFetch([{ match: /arxiv\.org\/html\//, text: paperHtml() }]);
  try {
    const src = await fetchArxivSource(HTML_URL, () => {});
    assert.equal(src.kind, 'html');
    assert.ok(src.structure, 'HTML 应带结构化切片');
    const titles = src.structure.sections.map((s) => s.title).join('|');
    assert.match(titles, /Introduction/);
    assert.match(titles, /Limitations/);
    assert.ok(src.structure.chunks.some((c) => c.type === 'formula'), 'HTML 里的公式应进入切片');
    assert.ok(src.structure.chunks.some((c) => c.type === 'table'), 'HTML 里的表格应进入切片');
  } finally {
    mock.restore();
  }
});

test('HTML 404 时回退 TeX 源码，并保留章节结构', async () => {
  const mock = mockFetch([
    { match: /arxiv\.org\/e-print\//, buffer: Buffer.from(`\\documentclass{article}\\begin{document}\n${paperTex().replace(/^/gm, '')}\n\\end{document}\n`, 'utf8') },
  ]);
  try {
    const src = await fetchArxivSource(TEX_URL, () => {});
    assert.equal(src.kind, 'tex');
    assert.ok(src.text.length > 500, 'TeX 正文应被 latexToText 处理后使用');
    assert.ok(src.structure, 'TeX 应带结构化切片');
    assert.ok(src.structure.sections.length >= 4, `应识别多个章节：${src.structure.sections.map((s) => s.title).join('|')}`);
    assert.ok(src.structure.chunks.some((c) => c.type === 'formula'));
  } finally {
    mock.restore();
  }
});

test('HTML 与 TeX 都不可用时回退 PDF，并抽出带换行的正文', async () => {
  const mock = mockFetch([{ match: /arxiv\.org\/pdf\//, buffer: minimalPdf() }]);
  try {
    const src = await fetchArxivSource(PDF_URL, () => {});
    assert.equal(src.kind, 'pdf');
    assert.ok(src.text.length > 300, 'PDF 正文应可提取');
    assert.ok(src.textLines && src.textLines.includes('\n'), 'PDF 需额外提供带换行的正文（供章节启发式分节）');
    assert.match(src.textLines, /1 Introduction|Introduction/);
  } finally {
    mock.restore();
  }
});

test('fetchArxivHtml 对非 arXiv 链接直接报错', async () => {
  await assert.rejects(() => fetchArxivHtml('https://example.com/paper.pdf'), /不是 arXiv 论文链接/);
});
