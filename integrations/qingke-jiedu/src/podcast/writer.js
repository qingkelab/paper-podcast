import { config } from '../config.js';
import { chatRequest } from '../ai/openaiProvider.js';

/** 时长档位 → 目标字数区间（中文口播约 4.2 字/秒）。 */
const DURATION_TARGETS = { short: '800–950', auto: '900–1150', long: '1150–1400' };

export function countChinese(text) {
  return (String(text || '').match(/[\u4e00-\u9fff]/g) || []).length;
}

/** 解析写稿模型连接（与 createProvider 同规则）。 */
function resolveLLM(providerName, model) {
  let p = providerName || config.llmProvider;
  if (p === 'ollama') {
    return { name: p, baseUrl: config.ollamaBaseUrl, apiKey: 'ollama', model: model || config.ollamaModel };
  }
  if (p === 'openai') {
    if (!config.openaiApiKey) throw new Error('未配置 OPENAI_API_KEY');
    return { name: p, baseUrl: config.openaiBaseUrl, apiKey: config.openaiApiKey, model: model || config.openaiModel };
  }
  if (!config.deepseekApiKey) throw new Error('未配置 DEEPSEEK_API_KEY');
  return { name: 'deepseek', baseUrl: config.deepseekBaseUrl, apiKey: config.deepseekApiKey, model: model || config.deepseekModel };
}

async function chatJSON(messages, { providerName, model }, maxTokens = 8000) {
  const llm = resolveLLM(providerName, model);
  const r = await chatRequest({
    name: llm.name,
    apiKey: llm.apiKey,
    baseUrl: llm.baseUrl,
    model: llm.model,
    messages,
    maxTokens,
    timeoutMs: 600000,
  });
  return r;
}

/** 从模型文本里宽松抠 JSON 对象。 */
export function parseJsonLoose(text) {
  let t = String(text || '').trim();
  t = t.replace(/^```(?:json)?\s*/i, '').replace(/```\s*$/, '');
  try {
    return JSON.parse(t);
  } catch {
    const m = t.match(/\{[\s\S]*\}/);
    if (m) {
      try {
        return JSON.parse(m[0]);
      } catch {
        /* ignore */
      }
    }
  }
  return null;
}

const SYSTEM_PROMPT = `你是一位资深科研传播主播，擅长把晦涩的论文/文章讲成普通人也爱听的科普播客。
你的讲解遵循"深读"方法论：
1. 零背景可进入——不假设听众懂专业术语，关键术语首次出现时先用一句白话解释，再给一个生活类比，最后才讲它在论文里的作用；
2. 双层讲解——先讲直觉（用最小例子走通机制），再讲精确细节；
3. 用具体数字支撑观点，并说明数字高/低代表什么；
4. 明确区分论文作者的主张与你的推断；
5. 不只讲贡献，还要讲代价、适用边界和未解决的问题。`;

function metaBlock(meta) {
  const parts = [];
  if (meta.authors) parts.push(`作者：${String(meta.authors).slice(0, 200)}`);
  if (meta.institution) parts.push(`机构：${String(meta.institution).slice(0, 120)}`);
  if (meta.date) parts.push(`时间：${String(meta.date).slice(0, 40)}`);
  return parts.length ? parts.join('\n') : '（暂无机构/时间元数据）';
}

function figLines(figures) {
  if (!figures || !figures.length) return '（本文没有可用图片）';
  return figures
    .map((f) => `- 图片 ${f.num}${f.page != null ? `（第 ${f.page} 页）` : ''}图注：${String(f.caption || '无图注').replace(/\s+/g, ' ').slice(0, 200)}`)
    .join('\n');
}

/**
 * 播客脚本任务：从素材（论文属性 + 图片清单 + 正文截断）一次性产出
 * { title, image_rank, scenes:[{narration, figure}] }。
 */
function buildScriptMessages(ctx, duration, extra) {
  const target = DURATION_TARGETS[duration] || DURATION_TARGETS.auto;
  const task = `【成片脚本】论文《${ctx.title || '未知'}》的图片/页面素材如下，请生成 3–5 分钟的第一人称中文科普播客脚本：
【论文属性】
${metaBlock(ctx)}
摘要：${(ctx.abstract || '').slice(0, 1000)}

1. 开场（第 1 个场景，figure=null）：钩子 + 一句话总结（≤50 字），画面用论文首页；
   自然提及机构与时间（如"这是 XX 大学/机构于 XXXX 年发表的一篇论文"）；缺失则不编造。
2. 正文：按叙事逻辑编排（背景动机 → 核心方法 → 实验证据 → 局限与影响），讲某张图时 figure 填图片编号
   （只能填下面清单里存在的编号），每张图最多用一次；没有合适配图的场景填 null（画面会显示论文页）。
   旁白要描述这张图画了什么、观众该怎么看、它支撑什么观点。
3. 结尾（最后场景，figure=null）：总结 + 展望（从论文具体局限推出，不写套话）。

【要求】
1. 全程中文，第一人称"我"，口语化、有感染力。
2. 总字数 ${target} 字，共 8–12 个场景，每段旁白 60–120 字。
3. 术语首次出现先白话+生活类比，再讲作用；数字具体并解释含义。

【输出格式】只输出 JSON（不要任何解释文字）：
{"title": "视频标题（20字内，可带emoji）",
 "image_rank": [{"num": 1, "weight": 9, "reason": "..."}],
 "scenes": [{"narration": "旁白文本", "figure": 1}, {"narration": "旁白文本", "figure": null}]}

图片/页面素材：
${figLines(ctx.figures)}${extra || ''}
论文正文（截断，供取数/核对）：
${(ctx.text || '').slice(0, 18000)}`;
  return [
    { role: 'system', content: SYSTEM_PROMPT },
    { role: 'user', content: task },
  ];
}

function expandInstruction(total, target, summary) {
  return `\n\n【扩写指令】上一版脚本只有约 ${total} 字，未达标（目标 ${target} 字）。请基于以下场景骨架扩写：
保持每个场景的配图 figure 不变，把每段旁白讲得更细更生动（补类比、直觉解释、机制细节、数字含义），
只输出完整 JSON（title 保持不变），不要任何解释。\n${summary}`;
}

export function validateScenes(raw, figIds) {
  const scenes = raw && Array.isArray(raw.scenes) ? raw.scenes : [];
  const out = [];
  for (const s of scenes) {
    const narration = String((s && s.narration) || '').trim();
    if (!narration) continue;
    let fig = s.figure;
    if (fig != null) {
      fig = Number(fig);
      if (!Number.isFinite(fig) || !figIds.has(fig)) fig = null;
    }
    out.push({ narration, figure: fig });
  }
  return out;
}

/** 校验并规范化整份脚本 JSON。 */
export function normalizeScript(raw, ctx) {
  const figIds = new Set(ctx.figures.map((f) => f.num));
  const scenes = validateScenes(raw, figIds);
  if (!scenes.length) throw new Error('写稿输出没有有效场景，请重试或换更强的模型');
  const title = String((raw && raw.title) || ctx.title || '论文播客').slice(0, 40);
  const rank = Array.isArray(raw && raw.image_rank)
    ? raw.image_rank.filter((r) => r && figIds.has(Number(r.num)))
    : [];
  const total = scenes.reduce((s, x) => s + countChinese(x.narration), 0);
  return { title, scenes, rank, totalChars: total };
}

/** 生成脚本：先出稿，字数不足（<700 汉字）则扩写一次。 */
export async function generateScript(ctx, { providerName, model, duration = 'auto' } = {}, log = () => {}) {
  const target = DURATION_TARGETS[duration] || DURATION_TARGETS.auto;

  async function ask(extra) {
    const msgs = buildScriptMessages(ctx, duration, extra);
    const first = await chatJSON(msgs, { providerName, model });
    let raw = parseJsonLoose(first.content);
    if (!raw) {
      // 一次机会：要求只输出 JSON
      const retry = await chatJSON(
        [
          ...msgs,
          { role: 'user', content: '以上输出无法解析为 JSON。请只输出一个合法的 JSON 对象，不要任何其它文字。' },
        ],
        { providerName, model },
      );
      raw = parseJsonLoose(retry.content);
    }
    if (!raw) throw new Error('写稿模型输出无法解析为 JSON，请重试或换更强的模型');
    return raw;
  }

  log('写稿模型生成脚本中…');
  let raw = await ask('');
  let script = normalizeScript(raw, ctx);
  log(`脚本 v1：${script.scenes.length} 个场景，约 ${script.totalChars} 字（目标 ${target}）`);

  if (script.totalChars < 700 && script.scenes.length < 8) {
    log('字数偏少，扩写一次…');
    const summary = script.scenes
      .map((s, i) => `- 场景${i + 1}: 图${s.figure == null ? '无' : s.figure} | ${s.narration.slice(0, 40)}`)
      .join('\n');
    try {
      const raw2 = await ask(expandInstruction(script.totalChars, target, summary));
      const script2 = normalizeScript(raw2, ctx);
      if (script2.totalChars > script.totalChars + 30) {
        script = script2;
        log(`扩写后：${script.scenes.length} 个场景，约 ${script.totalChars} 字`);
      }
    } catch (err) {
      log(`扩写失败（使用 v1）：${err.message}`);
    }
  }
  if (!script.scenes.length) throw new Error('写稿失败：没有生成有效场景');
  return script;
}
