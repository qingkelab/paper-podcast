// 覆盖「文案 provider 兼容 + /api/copy 主链路」：全部用假 fetch，不发真实网络请求。
process.env.DEEPSEEK_API_KEY = process.env.DEEPSEEK_API_KEY || 'test-key';
process.env.OPENAI_API_KEY = process.env.OPENAI_API_KEY || 'test-key';
process.env.QUALITY_REVIEW = '0'; // 关掉生成后审校，专注主链路断言

import test from 'node:test';
import assert from 'node:assert/strict';

// 注意：配置在模块加载时读取 .env / process.env，所以必须动态导入（静态 import 会被提升到赋值之前）
const { createProvider } = await import('../src/ai/index.js');

/** 假 chat/completions：记录所有请求，并按 system 提示返回对应内容。 */
function installFakeFetch() {
  const original = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, init = {}) => {
    const body = init.body ? JSON.parse(init.body) : {};
    const messages = body.messages || [];
    const sys = messages[0]?.content || '';
    const user = messages[messages.length - 1]?.content || '';
    requests.push({ url: String(url), system: sys, user, maxTokens: body.max_tokens, model: body.model });
    const content = respond(sys);
    return {
      ok: true,
      status: 200,
      async json() {
        return { choices: [{ message: { content, reasoning: '' } }] };
      },
      async text() {
        return content;
      },
    };
  };
  return {
    requests,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

function respond(sys) {
  if (/爆款标题/.test(sys) && /严格输出 JSON/.test(sys)) {
    return JSON.stringify({
      title: '循环状态替代全局注意力',
      titles: ['循环状态替代全局注意力', '省一半显存的长上下文', 'BLEU 41.8 的新架构'],
      copy:
        '## 一句话总结\n\n用循环状态替代全局注意力，显存降到 18.6 GB。\n\n## 背景\n\n…\n\n## 核心方法\n\n…\n\n## 结果\n\nBLEU 41.8。\n\n## 局限与结论\n\n只在文本模态验证。',
    });
  }
  return '';
}

for (const providerName of ['deepseek', 'openai', 'ollama']) {
  test(`${providerName} provider：图文文案（generate）可正常解析`, async () => {
    const fake = installFakeFetch();
    try {
      const provider = createProvider(providerName);
      const result = await provider.generate({
        source: { type: 'pdf', title: 'LoopFormer', text: 'BLEU 41.8', terms: ['BLEU'] },
        limits: { maxCopyChars: 1000, maxTitleChars: 20 },
      });
      assert.ok(result.copy.includes('一句话总结'), `${providerName} 文案应正常解析`);
      assert.equal(result.titles.length, 3);
      assert.ok(result.titles.every((t) => t.length <= 20), '标题应受字数上限约束');
      assert.equal(fake.requests.length, 1, '关掉审校后只应有一次调用');
      assert.match(fake.requests[0].system, /爆款标题/);
    } finally {
      fake.restore();
    }
  });
}

test('模型不可用时 provider.generate 抛出可读错误（不静默失败）', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('fetch failed');
  };
  try {
    const provider = createProvider('deepseek');
    await assert.rejects(
      () =>
        provider.generate({
          source: { type: 'pdf', title: 'LoopFormer', text: 'BLEU 41.8', terms: ['BLEU'] },
          limits: { maxCopyChars: 1000, maxTitleChars: 20 },
        }),
      (err) => /fetch failed|无法连接/.test(err.message),
    );
  } finally {
    globalThis.fetch = original;
  }
});
