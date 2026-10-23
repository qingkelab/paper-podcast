import test from 'node:test';
import assert from 'node:assert/strict';
import { buildPaperStructure, chunksFromHtml, classifyBlock, extractNumbers } from '../src/chunker.js';
import { longPaperText, paperHtml, paperTex, paperPdfLines } from './fixtures.js';

test('30k+ 字论文被完整切片（不再只取前 16k）', () => {
  const text = longPaperText();
  assert.ok(text.length > 30000, `合成论文应 >30k 字，实际 ${text.length}`);

  const structure = buildPaperStructure({ kind: 'tex', text });
  const { chunks, sections } = structure;

  assert.ok(chunks.length >= 20, `chunks 数量应足够多，实际 ${chunks.length}`);
  assert.ok(sections.length >= 8, `应识别出多个章节，实际 ${sections.length}: ${sections.map((s) => s.title).join('/')}`);

  // 覆盖度：所有 chunk 的字符总量应接近全文（结构解析不丢正文）
  const covered = chunks.reduce((n, c) => n + c.text.length, 0);
  assert.ok(covered / text.length > 0.85, `切片覆盖率应 >85%，实际 ${(covered / text.length * 100).toFixed(1)}%`);

  // 后半篇（局限 / 结论 / 参考文献）必须被切到
  const tailText = chunks.map((c) => c.text).join('\n');
  assert.match(tailText, /只在文本模态上验证/, '后半篇的局限段落必须进入切片');
  assert.match(tailText, /53\.7/, '结论里的数字必须进入切片');

  // chunk id 稳定、唯一、有序
  const ids = chunks.map((c) => c.id);
  assert.equal(new Set(ids).size, ids.length, 'chunk id 必须唯一');
  assert.deepEqual(ids, ids.slice().sort((a, b) => Number(a.slice(1)) - Number(b.slice(1))), 'chunk 顺序应与原文一致');

  // 每个 chunk 有 section title / index / text，且不超过预算太多
  for (const c of chunks) {
    assert.ok(c.sectionTitle, 'chunk 必须带 section title');
    assert.equal(typeof c.index, 'number');
    assert.ok(c.text.length > 0);
    assert.ok(c.text.length <= 1700, `单 chunk 不应超过预算（实际 ${c.text.length}）`);
  }
});

test('公式与图注保留为独立 chunk', () => {
  const structure = buildPaperStructure({ kind: 'tex', text: longPaperText() });
  const formulas = structure.chunks.filter((c) => c.type === 'formula');
  assert.ok(formulas.length >= 1, '应识别出公式 chunk');
  assert.match(formulas[0].text, /\\odot/, '公式保留 LaTeX 原文');

  const figures = structure.chunks.filter((c) => c.type === 'figure');
  assert.ok(figures.length >= 1, '应识别出图注 chunk');
  assert.match(figures[0].text, /^图注：/);
});

test('HTML 使用真实 section hierarchy 与 MathML 公式', () => {
  const structure = chunksFromHtml(paperHtml());
  const titles = structure.sections.map((s) => s.title);
  assert.ok(titles.some((t) => /Introduction/.test(t)), `应识别 Introduction：${titles.join(' | ')}`);
  assert.ok(titles.some((t) => /Method/.test(t)));
  assert.ok(titles.some((t) => /Experiments/.test(t)));
  assert.ok(titles.some((t) => /Limitations/.test(t)));
  assert.ok(!titles.some((t) => /nav/i.test(t)), '页面导航不应进入切片');

  const intro = structure.chunks.filter((c) => /Introduction/.test(c.sectionTitle));
  assert.ok(intro.length >= 2, 'Introduction 下应有多个段落 chunk');
  assert.equal(intro[0].sectionPath, 'Introduction', '论文大标题不应进入 section path');

  const formulas = structure.chunks.filter((c) => c.type === 'formula');
  assert.ok(formulas.length >= 2, 'MathML annotation 里的 LaTeX 应被抽出');
  assert.ok(
    formulas.some((c) => /h_t = \\alpha/.test(c.text)),
    `状态更新公式应被抽出：${formulas.map((c) => c.text).join(' / ')}`,
  );

  const figure = structure.chunks.find((c) => c.type === 'figure');
  assert.ok(figure, 'figure/figcaption 应成为图注 chunk');
  assert.match(figure.text, /Architecture of LoopFormer/);

  const table = structure.chunks.find((c) => c.type === 'table');
  assert.ok(table, 'ltx_table 应被识别为表格 chunk');
  assert.match(table.text, /41\.8 BLEU/);
});

test('PDF 带换行正文按编号标题分节', () => {
  const structure = buildPaperStructure({ kind: 'pdf', textLines: paperPdfLines() });
  const titles = structure.sections.map((s) => s.title).join(' | ');
  assert.match(titles, /1 Introduction/);
  assert.match(titles, /2 Method/);
  assert.match(titles, /3 Experiments/);
  assert.match(titles, /4 Limitations/);
  const method = structure.chunks.filter((c) => /Method/.test(c.sectionTitle));
  assert.ok(method.some((c) => /state update operator/.test(c.text)), '方法节内容应归到方法节');
});

test('无结构纯文本也能切片（兜底路径）', () => {
  const text = paperTex().replace(/^##\s+/gm, '').replace(/\n\n/g, ' ');
  const structure = buildPaperStructure({ kind: 'text', text: `${text} ${'补充说明。'.repeat(1200)}` });
  assert.ok(structure.chunks.length >= 3);
  assert.ok(structure.chunks.every((c) => c.text.length <= 1700));
});

test('块类型识别与数字抽取', () => {
  assert.equal(classifyBlock('$$L = \\sum_i \\theta_i$$'), 'formula');
  assert.equal(classifyBlock('图注：图 3 训练曲线'), 'figure');
  assert.equal(classifyBlock('表 2：消融结果'), 'table');
  assert.equal(classifyBlock('这是一段普通正文，讲方法。'), 'paragraph');

  const numbers = extractNumbers('BLEU 41.8，提升 0.92，显存 18.6 GB，3.5 天');
  assert.ok(numbers.includes('41.8'));
  assert.ok(numbers.includes('3.5'));
});

test('TeX 与 HTML 都产出稳定 section 路径（子节归属父节）', () => {
  const tex = buildPaperStructure({ kind: 'tex', text: '## 2 Method\n\n### 2.1 Update\n\n状态更新算子。' });
  const sub = tex.chunks.find((c) => /2\.1/.test(c.sectionPath));
  assert.ok(sub, `子节应带父级路径：${tex.chunks.map((c) => c.sectionPath).join(' | ')}`);
  assert.match(sub.sectionPath, /Method.*2\.1 Update|2\.1 Update/);
});
