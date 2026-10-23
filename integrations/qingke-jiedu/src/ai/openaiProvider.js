import { config } from '../config.js';
import { charCount, isChineseText, truncateAtSentence, truncateTitle } from '../textUtils.js';
import { checkStyle, styleWarningsForPrompt } from '../styleCheck.js';
import { parseJsonLoose } from './json.js';

function buildMessages(source, limits) {
  const sys = [
    '你是一名面向微信公众号读者的「论文深度解读」写手，采用「零背景可进入 + 技术足够硬」的双层讲解法：',
    '完全不懂该领域的人能顺着看懂，懂行的人也不会觉得讲错。',
    '',
    '## 硬性要求',
    `1. 解读文案必须**完整成稿**：总长控制在 ${limits.maxCopyChars} 字以内（含 Markdown 符号，不含空白），并以一句结论自然收尾，绝不允许写到一半被截断。`,
    '   字数预算建议：一句话总结约 35 字 / 背景约 100 字 / 核心方法约 380 字（重点，深度解析）/ 结果约 170 字 / 局限与结论约 120 字（留出 Markdown 与标点的余量）。',
    '   「局限与结论」是**必写**小节：即使前面小节需要压缩，也必须保留它并以此收尾。',
    '   宁可少写一个例子、砍掉次要细节，也要保证「问题 → 方法 → 证据 → 结论」完整闭环。',
    `2. 爆款标题：每个标题控制在 8–16 字（最多 ${limits.maxTitleChars} 字，不含空白），必须语义完整、不断词，宁可更短也不要超长被截断。`,
    '   标题要具体到本文，每条至少命中一种「爆款钩子」，三条各用一种、不重复；禁止空洞无信息或照抄论文原标题：',
    '   - 数字前置：把本文最硬的一个结果/数据放到开头（如「38% 误差一次抹平」「训练快 10 倍」）；',
    '   - 反差颠覆：挑战直觉或旧常识（如「越标注越差？这篇论文说反了」）；',
    '   - 悬念留白：结果前置但扣住最关键一步（如「让模型自己写奖励函数，结果…」）；',
    '   - 痛点代入：直接喊话具体人群（如「还在人工调 prompt 的人，这篇要读」）；',
    '   - 结果直给：一句话说清「解决了什么 + 达到什么」（如「一句话让 LLM 学会用工具」）。',
    '   用词口语、有张力；数字必须来自原文，**不夸大、不标题党、不虚构数据**；不用「震撼/重磅/炸裂/绝了」这类空喊，也不加无关的「！？」堆叠。',
    '   文案用 Markdown 输出（## 小节、**加粗**、- 列表）。不要输出「原文线索」这类照抄原文的引用块，把字数留给解读本身。',
    '',
    '## 文案结构（按顺序，用 ## 分节，共 5 节）',
    '- **一句话总结**：中文 ≤50 字，同时说清「解决什么问题、做了什么、最关键成立依据」；自然点出机构与时效（如「Google 团队在 2017 年 6 月提出…」），信息缺失时不编造。',
    '- **背景**：现在的问题或旧做法卡在哪；从具体场景/失败案例开场，不要用一串方法名开场；理解主线必需的术语、缩写、指标首次出现即用白话解释（先讲「它是什么」，再讲「在本文中做什么」，必要时补精确定义/单位/范围）；不要循环定义或留孤立缩写。',
    '- **核心方法（全文重点，要深度解析）**：这是全文核心，要讲透，不是一句带过。要求：',
    '  1) 先点出作者的关键洞见或新做法；',
    '  2) 用 `- ` 列表逐条拆解关键组件/步骤，每条说清「它做什么 → 为什么需要 → 去掉会失去什么」；',
    '  3) 用一个最小例子从输入走到输出，把机制完整跑一遍；',
    '  4) 点出相对旧工作的最小差分（Before / After / Diff / Trade-off）。',
    '  术语先白话后精确；每个组件都交代「输入 → 做了什么 → 输出」。',
    '- **结果**：用具体数字支撑结论；说明关键指标「数值高低代表什么」；区分作者主张、直接证据、推断并标注证据强度（强/中等/弱）；点出最可信与最易被夸大的结论。',
    '- **局限与结论**：用 1–2 句讲清适用边界与失效条件；最后用一句有信息量的结论收尾（不是「值得一读」这类套话）。',
    '- 我会在文末自动加上「## 论文链接」小节并填好链接，你不要自己写链接。',
    '',
    '## 写作纪律',
    '- 术语首次出现即解释；类比后要回到精确定义，并说明类比在哪失效。',
    '- 不堆术语、不照抄摘要、不写模板套话；宁可少而准，不要空话。',
    '- 去 AI 味：不用「我们提出 / 本文研究 / 作者提出」这类转述开场，直接陈述事实；不用「不是 A 而是 B / 本质上 / 更重要的是」这类套话；结尾不写「提供新思路 / 新方向 / 值得关注」这类空话。',
    '- 若来源是普通网页而非论文，同样的解读法适用，但「证据强度」按网页内容的可信度与来源质量处理。',
    '',
    '## 术语准确性（硬性）',
    '- 专业名词、方法名、模型名、数据集名、指标名、缩写一律**保留原文英文**（如 Transformer、BLEU、KV cache、RL、VLA），不要自创中文译名；有通用译名时可「英文（中文）」并列，首次出现用白话解释。',
    '- 以「术语表」里的写法为准：术语表里的词必须原文保留、不要翻译、不要改写。',
    '- 所有数字、单位、百分比、指标值必须与原文一致，不四舍五入、不夸大、不改写。',
    '- 只用原文里真实出现的术语与结论；拿不准的术语宁可写原文英文，也不要翻译错或张冠李戴。',
  ];
  if (source.memory) {
    sys.push('', '## 历史记忆', source.memory, '');
  }
  sys.push(
    '严格输出 JSON，不要输出任何多余文字，格式如下：',
    '{"title":"主标题","titles":["主标题","备选1","备选2"],"copy":"Markdown 格式的解读文案"}',
  );

  const context = [
    `来源类型：${source.type === 'pdf' ? '论文 PDF' : '网页'}`,
    `标题：${source.title || '（无）'}`,
    `作者：${source.byline || '（无）'}`,
    `机构：${source.institution || '（未知）'}`,
    `发表时间：${source.date || '（未知）'}`,
    `摘要：${source.excerpt || '（无）'}`,
    `术语表（这些术语保留原文英文，不要翻译）：${(source.terms || []).join('、') || '（无）'}`,
    `正文（截断）：${(source.text || '').slice(0, 6000)}`,
  ].join('\n');

  return [
    { role: 'system', content: sys.join('\n') },
    { role: 'user', content: `请解读以下内容：\n\n${context}` },
  ];
}

const parseJson = parseJsonLoose;

/** 超字数时的压缩重试提示词：保留全部小节（含结论）与关键信息，只删冗余。 */
function buildCompressMessages(copy, limits) {
  const sys = [
    `你是文案压缩编辑。把下面这份解读文案压缩到 ${limits.maxCopyChars} 字以内（含 Markdown 符号，不含空白）。`,
    '硬性要求：',
    '1. 必须保留以下全部小节，缺一不可（标题用 ## 开头，顺序不变）：',
    '   ## 一句话总结 / ## 背景 / ## 核心方法 / ## 结果 / ## 局限与结论',
    '2. 结尾必须是「## 局限与结论」小节，并以一句完整结论收尾，绝不允许写到一半被截断；',
    '3. 只删冗余、合并同义表述，保留关键机制、具体数字与证据强度；',
    '4. 只输出 JSON：{"copy":"压缩后的 Markdown 文案"}',
  ].join('\n');
  return [
    { role: 'system', content: sys },
    { role: 'user', content: `待压缩文案：\n\n${copy}` },
  ];
}

/** 任一标题过长时，一次性重写全部 3 条完整标题（把超长旧标题作为反例）。 */
function buildTitlesFixMessages(source, limits, prevTitles) {
  const sys = [
    `给下面这篇内容写 3 个爆款标题。每个必须 ≤ ${limits.maxTitleChars} 字（不含空白）且语义完整、不断词；为保险每个控制在 8–14 字以内。`,
    '三条各用一种爆款钩子，不重复：数字前置 / 反差颠覆 / 悬念留白 / 痛点代入 / 结果直给。',
    '标题要具体到本文；数字必须来自原文，不夸大、不标题党、不虚构数据；不用「震撼/重磅/炸裂」这类空喊，不堆「！？」。',
    '只输出 JSON：{"titles":["标题1","标题2","标题3"]}',
  ].join('\n');
  const ctx = [
    `主题：${source.title || '（无）'}`,
    `摘要：${(source.excerpt || source.text || '').slice(0, 300)}`,
    prevTitles && prevTitles.length
      ? `注意：下面这些旧标题都超长被截断了，请写得明显更短：\n${prevTitles.join('\n')}`
      : '',
  ]
    .filter(Boolean)
    .join('\n');
  return [
    { role: 'system', content: sys },
    { role: 'user', content: ctx },
  ];
}

/** 图文转图自检自修：对照原文逐项审校并输出修正后的 JSON。 */
function buildReviewMessages(source, limits, current, styleHint = '') {
  const sys = [
    '你是严格审校编辑。下面是一篇论文解读文案（连同标题），对照原文逐项检查，有问题就改，没问题保持原样，**直接输出修正后的 JSON**。',
    '**全文用中文写作**（专业术语、方法名、模型名、指标名保留原文英文，如 LAWA、SAM 2、RoboCasa），不要输出英文。',
    '检查清单：',
    '1. 五个小节齐全、顺序正确（一句话总结 / 背景 / 核心方法 / 结果 / 局限与结论），并以一句完整结论收尾，不写到一半；',
    '2. 所有数字、百分比、指标与原文一致，不夸大、不改写、不四舍五入（发现不符就按原文改）；',
    '3. 专业名词/方法名/模型名/指标名一律保留原文英文，不自创中文译名（LAWA、SAM 2、RoboCasa、Transformer 等保持原样）；',
    '4. 去 AI 味与转述腔：不用「我们提出/本文研究/作者提出」这类开场，直接陈述事实；不用「不是A而是B/本质上/更重要的是」；结尾不写「提供新思路/新方向/值得关注」这类空话；',
    '5. 核心方法要有机制（输入 → 做了什么 → 输出），术语首次出现即用白话解释；',
    `6. 文案仍控制在 ${limits.maxCopyChars} 字以内（含 Markdown 符号），标题仍 ≤ ${limits.maxTitleChars} 字。`,
    styleHint ? `7. 文风体检整改（只调整表达与分段，不得改动事实与结构）：\n${styleHint}` : '',
    '只输出 JSON：{"title":"主标题","titles":["主标题","备选1","备选2"],"copy":"修正后的 Markdown 文案"}',
  ].filter(Boolean).join('\n');
  const ctx = [
    `原文要点（用于核对数字与术语）：\n${(source.text || '').slice(0, 4000)}`,
    `术语表：${(source.terms || []).join('、') || '（无）'}`,
    `待审校标题：${JSON.stringify(current.titles || [])}`,
    `待审校文案：\n${current.copy}`,
  ].join('\n\n');
  return [
    { role: 'system', content: sys },
    { role: 'user', content: ctx },
  ];
}

/**
 * 通用 OpenAI 兼容 chat/completions 请求（provider 与播客写稿共用）。
 *
 * 除了正文，**必须把 finish_reason / usage / model 带回来**：reasoning 模型会把思考 token
 * 算进 max_tokens，只看 content 无法区分「模型没话说」和「输出被截断」——上游阶段可靠性
 * 全靠这几个字段判断（见 src/deepread/stages.js）。
 *
 * @returns {Promise<{content:string, reasoning:string, finishReason:string, usage:object|null, model:string}>}
 */
export async function chatRequest({ baseUrl, apiKey, model, messages, maxTokens = 2200, timeoutMs, name = '' }) {
  const endpoint = `${String(baseUrl).replace(/\/+$/, '')}/chat/completions`;
  const ttl = timeoutMs || config.llmTimeoutMs;
  let res;
  const payload = {
    model,
    messages,
    temperature: 0.7,
    max_tokens: maxTokens,
  };
  // 可选：把 reasoning 预算与可见输出分开（只有显式配置时才发，避免服务端不认这个字段）
  if (config.llmReasoningEffort) payload.reasoning_effort = config.llmReasoningEffort;
  try {
    res = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(ttl),
    });
  } catch (err) {
    const name0 = err && err.name ? err.name : '';
    const msg = err && err.message ? err.message : String(err);
    if (name0 === 'TimeoutError' || name0 === 'AbortError' || /timeout|aborted/i.test(msg)) {
      throw new Error(
        `模型调用超时（${Math.round(ttl / 1000)} 秒）：请稍后重试，或换更快的模型（.env 可用 LLM_TIMEOUT_MS 调大超时）`,
      );
    }
    // 连不上时给出可读信息（前端会把它显示在「解读文案」卡片里；图片不受影响）
    const label = name || '模型';
    const hint = name === 'ollama' ? '；请确认本地 Ollama 已启动' : '；请检查网络与 Base URL';
    throw new Error(`${label} 无法连接（${endpoint}）：${msg}${hint}`);
  }

  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`${name} 调用失败 HTTP ${res.status}: ${detail.slice(0, 200)}`);
  }
  const data = await res.json();
  const choice = data?.choices?.[0] || {};
  const msg = choice.message || {};
  return {
    content: msg.content || '',
    reasoning: msg.reasoning || msg.reasoning_content || '',
    finishReason: choice.finish_reason || '',
    usage: data?.usage || null,
    model: data?.model || model || '',
  };
}

export function openAiCompatibleProvider({ name, apiKey, baseUrl, model }) {
  const chat = (messages, maxTokens = 2200) =>
    chatRequest({ name, apiKey, baseUrl, model, messages, maxTokens });

  return {
    name,
    model,
    async generate({ source, limits }) {
      const first = await chat(buildMessages(source, limits), 12000);
      const parsed = parseJson(first.content);
      const reasoning = first.reasoning || '';

      let copy = parsed?.copy || '';
      // 超预算时做一次压缩重试，保证整篇（含结论）完整收进字数内
      if (charCount(copy) > limits.maxCopyChars) {
        const compressed = parseJson((await chat(buildCompressMessages(copy, limits), 8000)).content);
        if (compressed?.copy) copy = compressed.copy;
      }
      copy = truncateAtSentence(copy, limits.maxCopyChars);

      // 标题：任一条超长（会被截断）时，最多重试 2 次重写，保证完整
      let titlesRaw = (Array.isArray(parsed?.titles) ? parsed.titles : [parsed?.title]).filter(
        Boolean,
      );
      for (let attempt = 0; attempt < 2; attempt++) {
        if (!titlesRaw.some((t) => charCount(t) > limits.maxTitleChars)) break;
        const fixed = parseJson(
          (await chat(buildTitlesFixMessages(source, limits, titlesRaw), 4096)).content,
        );
        if (Array.isArray(fixed?.titles) && fixed.titles.length) titlesRaw = fixed.titles;
        else break;
      }
      let titles = titlesRaw
        .map((t) => truncateTitle(t, limits.maxTitleChars))
        .filter(Boolean);

      // 自检自修：对照原文审校一遍（修正术语/数字/去 AI 味等），并把文风体检问题一并整改
      if (config.qualityReview) {
        try {
          const styleHint = styleWarningsForPrompt(copy, 'copy');
          const review = parseJson(
            (await chat(buildReviewMessages(source, limits, { copy, titles }, styleHint), 4000)).content,
          );
          if (review) {
            // 兜底：审校输出变成英文时丢弃，保留中文原稿
            if (typeof review.copy === 'string' && review.copy.trim() && isChineseText(review.copy)) {
              copy = truncateAtSentence(review.copy, limits.maxCopyChars);
            }
            const rt = (Array.isArray(review.titles) ? review.titles : [review.title])
              .filter(Boolean)
              .map((t) => truncateTitle(t, limits.maxTitleChars))
              .filter(Boolean);
            if (rt.length) titles = rt;
          }
        } catch {
          /* 审校失败，用原稿 */
        }
      }

      return {
        title: titles[0] || '值得一读的新进展',
        titles: titles.length ? titles.slice(0, 3) : ['值得一读的新进展'],
        copy: copy || '（模型未返回文案，请稍后重试）',
        reasoning,
        style: checkStyle(copy, 'copy'),
      };
    },

    /**
     * 论文深度解读：全文结构化理解 + 证据驱动生成。
     *   ① 结构化切片 → ② Research Map → ③ 检索式 context → ④ 逐节写作 →
     *   ⑤ Evidence Audit → ⑥ 定点修复（只重写问题小节）
     * 结构化流程不可用（切片太少 / 异常）时回退旧流程：Ollama 走 multipass，API 模型走整篇生成。
     */
  };
}
