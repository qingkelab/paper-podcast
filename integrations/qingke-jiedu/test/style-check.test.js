// 文风体检新增「慎用词 / 研究边界词」扫描（迁移自青稞技术解读规范）。
// 关键约束：这两类是提示（info），**不能**把 ok 翻成 false——否则会误伤本来没问题的稿子。
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  BOUNDARY_PHRASES,
  CAUTION_PHRASES,
  CAUTION_PHRASE_LIMIT,
  checkStyle,
  countPhraseHits,
  styleMetrics,
  styleWarningsForPrompt,
} from '../src/styleCheck.js';

test('慎用词/边界词清单非空且去重', () => {
  assert.ok(CAUTION_PHRASES.length > 8);
  assert.ok(BOUNDARY_PHRASES.includes('SOTA'));
  assert.equal(new Set(CAUTION_PHRASES).size, CAUTION_PHRASES.length);
  assert.ok(CAUTION_PHRASE_LIMIT >= 1);
});

test('countPhraseHits：按出现次数统计并降序返回', () => {
  const hits = countPhraseHits('真正的问题在于，真正关键的是首次做到。', ['真正', '首次', '不存在']);
  assert.deepEqual(hits, [
    { word: '真正', count: 2 },
    { word: '首次', count: 1 },
  ]);
});

test('styleMetrics：慎用词与研究边界词分别计数', () => {
  const m = styleMetrics('这真正是首次把 SOTA 拉下来的工作，真正值得注意。');
  assert.ok(m.cautionPhraseTotal >= 3, `实际 ${m.cautionPhraseTotal}`);
  assert.ok(m.boundaryPhraseTotal >= 2, `实际 ${m.boundaryPhraseTotal}`);
  assert.ok(m.cautionPhraseHits.some((h) => h.word === '真正' && h.count === 2));
});

test('checkStyle：慎用词超过阈值才提示，且是 info（不阻断）', () => {
  const few = checkStyle('## 结果\n\n真正重要的是这个数字。', 'deepread');
  assert.equal(few.warnings.some((w) => w.text.includes('慎用词')), false, '一个慎用词不该报警');

  const many = checkStyle('## 结果\n\n真正重要的是，尤其值得注意的是，首次出现了颠覆性的下一代方案。', 'deepread');
  const hit = many.warnings.find((w) => w.text.includes('慎用词'));
  assert.ok(hit, '堆多了应提示');
  assert.equal(hit.level, 'info');
  assert.equal(many.ok, true, 'info 级提示不能把 ok 翻成 false');
});

test('checkStyle：研究边界词命中即提示，并要求给出归属', () => {
  const res = checkStyle('## 结论\n\n这篇工作已经解决 SOTA 问题，碾压所有基线。', 'deepread');
  const hit = res.warnings.find((w) => w.text.includes('研究边界词'));
  assert.ok(hit, '应提示研究边界词');
  assert.match(hit.text, /来源归属/);
  assert.equal(hit.level, 'info');
  assert.equal(res.ok, true);
});

test('checkStyle：原有 warn 级判定（AI 味词）不受影响', () => {
  const res = checkStyle('## 结果\n\n随着时代的发展，总而言之，综上所述，本质上这件事很重要。', 'deepread');
  assert.equal(res.ok, false, 'AI 味词超标仍应判不通过');
  assert.ok(res.warnings.some((w) => w.level === 'warn' && w.text.includes('AI 味词')));
});

test('styleWarningsForPrompt：把边界提示一并带进审校清单', () => {
  const prompt = styleWarningsForPrompt('## 结论\n\n首次做到 SOTA，超越所有方法。', 'deepread');
  assert.match(prompt, /文风体检/);
  assert.match(prompt, /研究边界词/);
});
