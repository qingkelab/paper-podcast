import { config } from '../config.js';
import { openAiCompatibleProvider } from './openaiProvider.js';

/**
 * Provider 统一接口：`generate({ source, limits }) => Promise<{title, titles, copy}>`
 *
 * @param {string} [name] provider 名：'ollama' | 'openai' | 'deepseek'；省略时用 .env 的 LLM_PROVIDER
 * @param {string} [model] 模型名覆盖（如前端选择的 Ollama 模型）
 */
export function createProvider(name = config.llmProvider, model) {
  if (name === 'ollama') {
    return openAiCompatibleProvider({
      name: 'ollama',
      apiKey: 'ollama', // 本地无需鉴权，占位即可
      baseUrl: config.ollamaBaseUrl,
      model: model || config.ollamaModel,
    });
  }

  if (name === 'openai') {
    if (!config.openaiApiKey) {
      throw new Error('未配置 OPENAI_API_KEY：请在 .env 中填写');
    }
    return openAiCompatibleProvider({
      name: 'openai',
      apiKey: config.openaiApiKey,
      baseUrl: config.openaiBaseUrl,
      model: model || config.openaiModel,
    });
  }

  // deepseek（默认）
  if (!config.deepseekApiKey) {
    throw new Error('未配置 DEEPSEEK_API_KEY：请在 .env 中填写');
  }
  return openAiCompatibleProvider({
    name: 'deepseek',
    apiKey: config.deepseekApiKey,
    baseUrl: config.deepseekBaseUrl,
    model: model || config.deepseekModel,
  });
}

/** 返回「API」选项应使用的 provider 名（deepseek 优先，其次 openai）。 */
export function apiProviderName() {
  if (config.deepseekApiKey) return 'deepseek';
  if (config.openaiApiKey) return 'openai';
  return 'deepseek';
}

/** API 类 provider 是否已配置 key（未配置时只能转图，不能生成文案）。 */
export function apiConfigured() {
  return !!(config.deepseekApiKey || config.openaiApiKey);
}
