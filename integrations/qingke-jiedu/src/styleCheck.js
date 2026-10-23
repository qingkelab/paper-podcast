/**
 * 文风量化自检：把"去 AI 味 / 手机可读"这类定性要求变成可统计指标。
 * 参考公众号爆款拆解的统计法（段落粒度、句长、标点密度），用于：
 *  1) 生成后给出体检报告（前端展示）
 *  2) 超限时把问题清单注入审校 prompt，让模型整改
 */

/** 各产出的文风档位（阈值可按内容类型调）。 */
export const STYLE_PROFILES = {
  copy: { label: '图文文案', maxParaChars: 150, maxAvgParaChars: 110, maxSentenceChars: 80, maxExclaim: 1, maxDash: 2, maxAiPhrases: 0 },
  deepread: { label: '深度解读', maxParaChars: 180, maxAvgParaChars: 130, maxSentenceChars: 90, maxExclaim: 2, maxDash: 3, maxAiPhrases: 1 },
  podcast: { label: '播客脚本', maxParaChars: 130, maxAvgParaChars: 120, maxSentenceChars: 70, maxExclaim: 1, maxDash: 2, maxAiPhrases: 0 },
};

/** AI 味词表（命中即提示；不含"首先/其次"这类在列表里合法的词）。 */
const AI_PHRASES = [
  '随着',
  '总而言之',
  '综上所述',
  '总的来说',
  '本质上',
  '更重要的是',
  '说白了',
  '归根结底',
  '关键在',
  '核心在',
  '具有重要意义',
  '值得关注',
  '提供新思路',
  '新方向',
  '赋能',
  '闭环',
  '抓手',
  '本文研究',
  '作者提出',
  '我们提出',
  '值得一提的是',
  '需要注意的是',
  '不是……而是',
  '不仅能',
  '更能',
];

/** 去掉 markdown 结构，便于按"人读的文字"统计。 */
function stripMarkdown(md) {
  let t = String(md || '');
  t = t.replace(/```[\s\S]*?```/g, '\n\n'); // 代码块（含出处块/公式块）
  t = t.replace(/^\s*!\[[^\]]*\]\([^)]*\)\s*$/gm, ''); // 图片行
  t = t.replace(/\$\$[\s\S]*?\$\$/g, ' '); // 块级公式
  t = t.replace(/\$[^$\n]*\$/g, ' '); // 行内公式
  t = t.replace(/^\s*#{1,6}\s+.*$/gm, ''); // 标题
  t = t.replace(/^\s*>\s?/gm, ''); // 引用符
  t = t.replace(/^\s*[-*+]\s+/gm, ''); // 列表符
  t = t.replace(/^\s*\d+[.)]\s+/gm, '');
  t = t.replace(/!\[[^\]]*\]\([^)]*\)/g, '');
  t = t.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1'); // 链接保留文字
  t = t.replace(/\*\*|__|`/g, '');
  return t;
}

/**
 * 「少用」清单（迁移自青稞社区技术解读规范）：这些词本身不算错，堆多了会把
 * 「解释」写成「宣传」。命中只提示、不阻断（level: info），并进审校清单。
 */
export const CAUTION_PHRASES = [
  '真正',
  '尤其',
  '关键在于',
  '这意味着',
  '值得注意的是',
  '需要注意的是',
  '首次',
  '革命',
  '颠覆',
  '最强',
  'SOTA',
  '碾压',
  '下一代',
  '已经解决',
];

/** 慎用词的提示阈值：超过这个数量才提示，避免一句话里出现一次就被当成问题。 */
export const CAUTION_PHRASE_LIMIT = 3;

/**
 * 研究边界扫描词（同样来自规范）：命中后要么删掉，要么给明确来源归属。
 * 注意这里不判断「有没有归属」——那是模型的活，这里只负责把命中点摆出来。
 */
export const BOUNDARY_PHRASES = [
  '排名',
  '最强',
  '下一代',
  '已经解决',
  '已解决',
  '超越',
  '碾压',
  '证明',
  '首次',
  'SOTA',
  'best method',
  'ranking',
  'state-of-the-art',
];

/**
 * 「人声」检查表（整理自社区流传的「去 AI 味」提示词，见 README）：
 * 把「AI 腔」拆成四类可数的模式，全部只做提示（info），不阻断交付。
 *   fakeDepth 动名词假深度：突出了 / 反映了 / 促进了……
 *   highFreq  空泛高频词：至关重要 / 里程碑 / 错综复杂……
 *   chatbot   客服套话：希望对你有帮助 / 期待你的回复……
 *   passive   被动与幽灵主语：被视为 / 需要被 / 能够被……
 */
export const FAKE_DEPTH_PHRASES = ['突出了', '反映了', '促进了', '彰显了', '体现了', '表明了', '展示了', '凸显了'];

export const AI_HIGH_FREQ_PHRASES = [
  '至关重要',
  '具有里程碑意义',
  '里程碑',
  '不可或缺',
  '必不可少',
  '错综复杂',
  '全景',
  '见证了',
  '彰显著',
  '深入剖析',
  '发挥重要作用',
  '起到关键作用',
];

export const CHATBOT_PHRASES = [
  '希望对你有帮助',
  '希望对你有所帮助',
  '很棒的问题',
  '这是个好问题',
  '为了总结',
  '期待你的回复',
  '未来充满希望',
  '让我们一起',
  '如你所见',
];

export const PASSIVE_PHRASES = [
  '被视为',
  '被认为是',
  '被认为',
  '被称为',
  '需要被',
  '能够被',
  '可以被',
  '被广泛',
  '被用于',
  '被用来',
  '被设计为',
  '被证明',
];

/** 每类模式的提示阈值（超过才提示，避免正常行文里出现一两次就报警）。 */
export const VOICE_PHRASE_LIMIT = 2;

/** 「AI 腔密度」阈值：每千字命中多少个 AI 腔标记算偏高。 */
export const AI_TONE_PER_1K_LIMIT = 2;

/** 密度指标的最小样本长度：短片段会把密度放大成噪声，低于这个字数不提示。 */
export const AI_TONE_MIN_CHARS = 400;

/** 四类「AI 腔」清单：既做统计也做提示，单一来源避免漏改。 */
export const VOICE_LISTS = [
  { key: 'fakeDepth', label: '动名词假深度', phrases: FAKE_DEPTH_PHRASES },
  { key: 'highFreq', label: '空泛高频词', phrases: AI_HIGH_FREQ_PHRASES },
  { key: 'chatbot', label: '客服套话', phrases: CHATBOT_PHRASES },
  { key: 'passive', label: '被动/幽灵主语', phrases: PASSIVE_PHRASES },
];

function countChars(s) {
  // 计"可见字符"：排除空白与常见 markdown 残符
  return String(s || '').replace(/[\s*_#>`~|]/g, '').length;
}

function splitParagraphs(text) {
  return String(text || '')
    .split(/\n\s*\n+/)
    .map((p) => p.trim())
    .filter((p) => countChars(p) > 0);
}

function splitSentences(text) {
  return String(text || '')
    .split(/[。！？!?；;\n]+/)
    .map((s) => s.trim())
    .filter((s) => countChars(s) > 1);
}

/**
 * 统计一组词在文本里的命中次数，按次数从多到少排序。
 *
 * 默认去重叠：长词优先占位，「被认为是」命中后不再把「被认为」重复计一次
 * （否则同一处文字会被数两遍，密度指标虚高）。
 */
export function countPhraseHits(text, phrases, { dedupeOverlaps = true } = {}) {
  const plain = String(text || '');
  const lower = plain.toLowerCase();
  const taken = [];
  const overlaps = (start, end) => taken.some((t) => start < t.end && end > t.start);
  const ordered = [...phrases]
    .map(String)
    .filter(Boolean)
    .sort((a, b) => (dedupeOverlaps ? b.length - a.length : 0));
  const hits = [];
  for (const word of ordered) {
    const needle = word.toLowerCase();
    if (!needle) continue;
    let n = 0;
    let from = 0;
    for (;;) {
      const at = lower.indexOf(needle, from);
      if (at === -1) break;
      const end = at + needle.length;
      if (!dedupeOverlaps || !overlaps(at, end)) {
        n += 1;
        if (dedupeOverlaps) taken.push({ start: at, end });
      }
      from = end;
    }
    if (n > 0) hits.push({ word, count: n });
  }
  hits.sort((a, b) => b.count - a.count);
  return hits;
}

/**
 * 统计文风指标。
 * @param {string} text markdown 或纯文本
 * @returns {object} metrics
 */
export function styleMetrics(text) {
  const raw = String(text || '');
  const plain = stripMarkdown(raw);
  const paragraphs = splitParagraphs(plain);
  const paraLens = paragraphs.map(countChars);
  const sentences = splitSentences(plain);
  const sentLens = sentences.map(countChars);
  const chars = countChars(plain);
  const sum = (a) => a.reduce((x, y) => x + y, 0);
  const avg = (a) => (a.length ? Math.round((sum(a) / a.length) * 10) / 10 : 0);

  const count = (re) => (plain.match(re) || []).length;
  const aiHits = [];
  for (const w of AI_PHRASES) {
    const n = plain.split(w).length - 1;
    if (n > 0) aiHits.push({ word: w, count: n });
  }
  aiHits.sort((a, b) => b.count - a.count);

  const cautionHits = countPhraseHits(plain, CAUTION_PHRASES);
  const boundaryHits = countPhraseHits(plain, BOUNDARY_PHRASES);
  // 人声检查：四类 AI 腔模式各自计数，另给一个总指标「每千字 AI 腔标记数」
  const voice = {};
  let voiceTotal = 0;
  for (const { key, phrases } of VOICE_LISTS) {
    const hits = countPhraseHits(plain, phrases);
    const total = hits.reduce((s, x) => s + x.count, 0);
    voice[key] = { hits, total };
    voiceTotal += total;
  }

  const boldMatch = raw.match(/\*\*[^*\n]+\*\*/g) || [];
  return {
    chars,
    paragraphs: paragraphs.length,
    avgParaChars: avg(paraLens),
    maxParaChars: paraLens.length ? Math.max(...paraLens) : 0,
    sentences: sentences.length,
    avgSentenceChars: avg(sentLens),
    maxSentenceChars: sentLens.length ? Math.max(...sentLens) : 0,
    periods: count(/[。]/g),
    commas: count(/[，,]/g),
    exclamations: count(/[！!]/g),
    questions: count(/[？?]/g),
    dashes: count(/——|—/g),
    boldAnchors: boldMatch.length,
    firstPerson: count(/我们|我/g),
    firstPersonPer100: chars ? Math.round(((count(/我们|我/g) / chars) * 100) * 10) / 10 : 0,
    aiPhraseHits: aiHits,
    aiPhraseTotal: aiHits.reduce((s, x) => s + x.count, 0),
    cautionPhraseHits: cautionHits,
    cautionPhraseTotal: cautionHits.reduce((s, x) => s + x.count, 0),
    boundaryPhraseHits: boundaryHits,
    boundaryPhraseTotal: boundaryHits.reduce((s, x) => s + x.count, 0),
    voice,
    fakeDepthTotal: voice.fakeDepth.total,
    highFreqTotal: voice.highFreq.total,
    chatbotTotal: voice.chatbot.total,
    passiveTotal: voice.passive.total,
    voiceTotal,
    // 密度指标：不同长度的文章可以直接比
    aiTonePer1k: chars ? Math.round((voiceTotal / chars) * 1000 * 100) / 100 : 0,
  };
}

/**
 * 按档位体检，返回 { metrics, warnings, ok }。
 * warnings: [{ level: 'warn'|'info', text }]
 */
export function checkStyle(text, profileName = 'deepread') {
  const profile = STYLE_PROFILES[profileName] || STYLE_PROFILES.deepread;
  const m = styleMetrics(text);
  const warnings = [];

  if (m.maxParaChars > profile.maxParaChars) {
    warnings.push({
      level: 'warn',
      text: `有段落过长（最长 ${m.maxParaChars} 字 > ${profile.maxParaChars} 字），手机上一屏塞不下，需要拆段`,
    });
  }
  if (m.avgParaChars > profile.maxAvgParaChars) {
    warnings.push({
      level: 'info',
      text: `段落偏大（平均 ${m.avgParaChars} 字），建议再碎一点（目标 ≤ ${profile.maxAvgParaChars} 字）`,
    });
  }
  if (m.avgSentenceChars > profile.maxSentenceChars) {
    warnings.push({
      level: 'info',
      text: `句子偏长（平均 ${m.avgSentenceChars} 字），长句多拆或用逗号拉开口语节奏`,
    });
  }
  if (m.exclamations > profile.maxExclaim) {
    warnings.push({
      level: 'warn',
      text: `感叹号 ${m.exclamations} 个（上限 ${profile.maxExclaim}），情绪要克制`,
    });
  }
  if (m.dashes > profile.maxDash) {
    warnings.push({
      level: 'info',
      text: `破折号 ${m.dashes} 个（上限 ${profile.maxDash}），可用句号替代`,
    });
  }
  if (m.aiPhraseTotal > profile.maxAiPhrases) {
    const top = m.aiPhraseHits.slice(0, 5).map((h) => `${h.word}×${h.count}`).join('、');
    warnings.push({
      level: 'warn',
      text: `AI 味词命中 ${m.aiPhraseTotal} 处（上限 ${profile.maxAiPhrases}）：${top}`,
    });
  }
  // 慎用词：不阻断（info），但进审校清单，让模型自己决定删还是保留。
  if (m.cautionPhraseTotal > CAUTION_PHRASE_LIMIT) {
    const top = m.cautionPhraseHits.slice(0, 6).map((h) => `${h.word}×${h.count}`).join('、');
    warnings.push({
      level: 'info',
      text: `慎用词命中 ${m.cautionPhraseTotal} 处（> ${CAUTION_PHRASE_LIMIT} 才提示，规范建议少用）：${top}`,
    });
  }
  // 研究边界：命中就要求「要么删，要么给来源归属」，这是事实纪律而不是文风问题。
  if (m.boundaryPhraseTotal > 0) {
    const top = m.boundaryPhraseHits.slice(0, 6).map((h) => `${h.word}×${h.count}`).join('、');
    warnings.push({
      level: 'info',
      text: `研究边界词命中 ${m.boundaryPhraseTotal} 处：${top}。能删则删；必须保留的必须写明来源归属（论文称/报告称），不得写成领域共识`,
    });
  }
  // 人声检查：四类 AI 腔模式，逐类给命中点，并给密度总指标
  const voiced = VOICE_LISTS.map(({ key, label }) => ({ label, ...m.voice[key] })).filter((v) => v.total > 0);
  if (m.voiceTotal > VOICE_PHRASE_LIMIT && voiced.length) {
    const detail = voiced
      .map((v) => `${v.label} ${v.total} 处（${v.hits.slice(0, 3).map((h) => `${h.word}×${h.count}`).join('、')}）`)
      .join('；');
    warnings.push({
      level: 'info',
      text: `AI 腔模式：${detail}。删掉这些模板，换成具体事实、动作动词或主动句`,
    });
  }
  // 密度指标只对「成文」的稿子有意义：太短的片段会把密度放大成噪声
  if (m.chars >= AI_TONE_MIN_CHARS && m.aiTonePer1k > AI_TONE_PER_1K_LIMIT) {
    warnings.push({
      level: 'info',
      text: `AI 腔密度 ${m.aiTonePer1k}/千字（阈值 ${AI_TONE_PER_1K_LIMIT}）：长文里 AI 腔词密度偏高，逐句改成人在说话的样子`,
    });
  }
  return { profile: profile.label, metrics: m, warnings, ok: warnings.filter((w) => w.level === 'warn').length === 0 };
}

/** 把体检问题转成给审校模型的整改清单（无问题时返回空串）。 */
export function styleWarningsForPrompt(text, profileName) {
  const { warnings } = checkStyle(text, profileName);
  if (!warnings.length) return '';
  return [
    '## 文风体检（请在本次审校中一并整改）',
    ...warnings.map((w) => `- ${w.text}`),
  ].join('\n');
}
