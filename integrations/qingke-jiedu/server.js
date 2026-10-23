import express from 'express';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import multer from 'multer';
import { config } from './src/config.js';
import { prepareUrl, prepareUpload, generateCopy, processUrl } from './src/pipeline.js';
import { closeBrowser } from './src/webToImages.js';
import { createProvider, apiProviderName, apiConfigured } from './src/ai/index.js';
import { syncWechatPic } from './src/wechat.js';
import { plainFromMarkdown } from './src/markdown.js';
import { fetchArxivMeta } from './src/arxiv.js';
import { ARXIV_SRC_DIR } from './src/arxivSource.js';
import { searchArxiv } from './src/arxivSearch.js';
import { loadHistory, addHistory, clearHistory } from './src/history.js';
import { launchWechatBrowser, copyToClipboard } from './src/wechatBrowser.js';
import { generatePodcast, PODCAST_OUT } from './src/podcast/index.js';
import { MINIMAX_VOICES, EDGE_VOICES, resolveEngine } from './src/podcast/tts.js';

const app = express();
app.use(express.json({ limit: '1mb' }));

// 静态前端
app.use(express.static(path.join(config.root, 'public')));

// arXiv 本地化素材（TeX 源码 / SVG 栅格化后的图），供 Markdown 嵌入与公众号同步抓取
app.use('/_arxivsrc', express.static(ARXIV_SRC_DIR, { maxAge: '7d' }));

// 生成的图片 / zip / md（图片内联预览，其余直接返回）
app.get('/files/:id/:filename', (req, res, next) => {
  const { id, filename } = req.params;
  const safe = path.basename(filename);
  const abs = path.join(config.outputDir, id, safe);
  if (safe.endsWith('.png')) res.type('png');
  else if (safe.endsWith('.md')) res.type('text/markdown; charset=utf-8');
  else if (safe.endsWith('.zip')) res.type('application/zip');
  res.sendFile(abs, (err) => err && next());
});

// 强制下载（Content-Disposition: attachment）
app.get('/download/:id/:filename', (req, res, next) => {
  const { id, filename } = req.params;
  const safe = path.basename(filename);
  res.download(path.join(config.outputDir, id, safe), safe, (err) => err && next());
});

function errorMessage(err) {
  return (err && err.message) || String(err);
}

// 阶段一：链接 → 图片（抓取 + 转图 + 抽取正文）
// 完全不依赖文案模型：模型挂了 / 没配 key，这一步照样成功，图片与 ZIP 立即可下载。
app.post('/api/images', async (req, res) => {
  const { url } = req.body || {};
  if (!url || typeof url !== 'string') {
    return res.status(400).json({ error: '缺少 url 参数' });
  }
  try {
    const result = await prepareUrl(url.trim());
    addHistory({
      url: result.source?.url || url.trim(),
      title: result.source?.title || '',
      type: result.type,
    }).catch(() => {});
    res.json(result);
  } catch (err) {
    const message = errorMessage(err);
    console.error('[images]', message);
    res.status(500).json({ error: message });
  }
});

// 阶段二：按 id 生成解读文案 + 爆款标题（唯一依赖模型的一步）
// 失败只影响文案：阶段一落盘的图片 / ZIP / summary.md 依然可下载，前端可一键重试。
app.post('/api/copy', async (req, res) => {
  const { id, provider, model } = req.body || {};
  if (!id || typeof id !== 'string') {
    return res.status(400).json({ error: '缺少 id 参数' });
  }
  try {
    const result = await generateCopy(id, provider || undefined, model || undefined);
    res.json(result);
  } catch (err) {
    const message = errorMessage(err);
    console.error('[copy]', message);
    const missing = err && err.code === 'ENOENT';
    res.status(missing ? 404 : 502).json({
      error: missing ? '结果不存在或已过期（请重新转图）' : message,
      id,
      copyStatus: 'error',
    });
  }
});

// 上传文章（阶段一）：PDF → 转图；.md/.txt → 纯文本（无图片）。文案由 /api/copy 单独生成。
const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: config.uploadMaxMb * 1024 * 1024 },
});
app.post('/api/upload', upload.single('file'), async (req, res) => {
  if (!req.file) return res.status(400).json({ error: '缺少文件' });
  try {
    const result = await prepareUpload({
      filename: req.file.originalname || 'upload.txt',
      buffer: req.file.buffer,
    });
    res.json(result);
  } catch (err) {
    const message = errorMessage(err);
    console.error('[upload]', message);
    res.status(500).json({ error: message });
  }
});

// 一站式接口（保持兼容）：转图 + 文案一次跑完；
// 文案失败时返回 200 + copyStatus: 'error'，图片部分照常可用。
app.post('/api/process', async (req, res) => {
  const { url, provider, model } = req.body || {};
  if (!url || typeof url !== 'string') {
    return res.status(400).json({ error: '缺少 url 参数' });
  }
  try {
    const result = await processUrl(url.trim(), provider || undefined, model || undefined);
    addHistory({
      url: result.source?.url || url.trim(),
      title: result.source?.title || '',
      type: result.type,
    }).catch(() => {});
    res.json(result);
  } catch (err) {
    const message = errorMessage(err);
    console.error('[process]', message);
    res.status(500).json({ error: message });
  }
});

// 论文深度解读：读 arXiv HTML 版，图片以 CDN 嵌入，输出 Markdown 报告
// ===== 论文视频播客：异步任务 + SSE 进度 =====
const podJobs = new Map(); // jobId -> { state, stage, detail, result, errorMessage, clients:Set }

function podPush(job, event, payload) {
  const frame = `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
  for (const res of job.clients) {
    try {
      res.write(frame);
    } catch {
      /* ignore */
    }
  }
}
function podSnapshot(job) {
  return { state: job.state, stage: job.stage || '', detail: job.detail || '' };
}
function podEndClients(job) {
  for (const res of job.clients) {
    try {
      res.end();
    } catch {
      /* ignore */
    }
  }
  job.clients.clear();
}

// 发起播客任务：立即返回 job id，后台执行（抽取→写稿→配音→合成）
app.post('/api/podcast', async (req, res) => {
  const { url, provider: providerName, model, duration, voice, engine } = req.body || {};
  if (!url || typeof url !== 'string') {
    return res.status(400).json({ error: '缺少 url 参数' });
  }
  const id = crypto.randomUUID();
  const job = {
    id,
    state: 'running',
    stage: 'queued',
    detail: '',
    result: null,
    errorMessage: '',
    clients: new Set(),
  };
  podJobs.set(id, job);
  (async () => {
    const update = (stage, detail) => {
      job.stage = stage || job.stage;
      job.detail = detail || '';
      podPush(job, 'stage', podSnapshot(job));
    };
    try {
      const result = await generatePodcast(
        { url: url.trim(), providerName, model, duration, voice, engine },
        update,
        (msg) => {
          job.stage = job.stage || 'run';
          job.detail = msg;
          podPush(job, 'stage', podSnapshot(job));
        },
      );
      addHistory({ url: url.trim(), title: result.title || '', type: 'podcast' }).catch(() => {});
      job.state = 'done';
      job.result = result;
      podPush(job, 'done', { result });
    } catch (err) {
      job.state = 'error';
      job.errorMessage = (err && err.message) || String(err);
      console.error('[podcast]', job.errorMessage);
      podPush(job, 'fail', { message: job.errorMessage });
    }
    podEndClients(job);
    setTimeout(() => podJobs.delete(id), 10 * 60 * 1000);
  })();
  res.json({ id });
});

// SSE：订阅播客任务进度
app.get('/api/podcast/events', (req, res) => {
  const job = podJobs.get(String(req.query.job || ''));
  if (!job) return res.status(404).json({ error: '任务不存在或已过期' });
  res.set({
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  res.flushHeaders();
  const send = (event, payload) => {
    try {
      res.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
    } catch {
      /* ignore */
    }
  };
  send('snapshot', podSnapshot(job));
  if (job.state === 'done') {
    send('done', { result: job.result });
    return res.end();
  }
  if (job.state === 'error') {
    send('fail', { message: job.errorMessage || '播客生成失败' });
    return res.end();
  }
  job.clients.add(res);
  const heartbeat = setInterval(() => {
    try {
      res.write(': ping\n\n');
    } catch {
      /* ignore */
    }
  }, 20000);
  req.on('close', () => {
    job.clients.delete(res);
    clearInterval(heartbeat);
  });
});

// 配音音色与默认引擎信息（前端选择用）
app.get('/api/podcast/info', (_req, res) => {
  res.json({
    engine: resolveEngine(),
    minimaxConfigured: !!config.minimaxApiKey,
    edgeVoices: Object.entries(EDGE_VOICES).map(([id, name]) => ({ id, name })),
    minimaxVoices: Object.entries(MINIMAX_VOICES).map(([id, name]) => ({ id, name })),
  });
});

// 播客产物（podcast.mp4 / meta.json / 画面素材等）
app.get('/api/podcast/files/:id/:filename', (req, res, next) => {
  const { id, filename } = req.params;
  if (!/^[0-9a-f-]{36}$/.test(id)) return res.status(400).json({ error: '非法的任务 id' });
  const safe = path.basename(filename);
  const abs = path.join(PODCAST_OUT, id, safe);
  if (safe.endsWith('.mp4')) res.type('video/mp4');
  else if (safe.endsWith('.json')) res.type('application/json; charset=utf-8');
  res.sendFile(abs, (err) => err && next(err));
});

app.get('/api/health', (_req, res) => {
  let provider = '未配置';
  let model = '';
  try {
    const p = createProvider();
    provider = p.name;
    model = p.model || '';
  } catch {
    /* 未配置 */
  }
  res.json({ ok: true, provider, model, apiConfigured: !!apiConfigured() });
});

// 当前出站 IP（供微信 IP 白名单配置用，缓存 60s）
let ipCache = { ip: '', at: 0 };
app.get('/api/ip', async (_req, res) => {
  if (ipCache.ip && Date.now() - ipCache.at < 60000) {
    return res.json({ ip: ipCache.ip });
  }
  try {
    const r = await fetch('https://ipv4.icanhazip.com', { signal: AbortSignal.timeout(8000) });
    const text = (await r.text()).trim();
    ipCache = { ip: /^\d{1,3}(\.\d{1,3}){3}$/.test(text) ? text : '', at: Date.now() };
    res.json({ ip: ipCache.ip });
  } catch {
    res.json({ ip: ipCache.ip || '' });
  }
});

// 历史记录（使用过的链接）
app.get('/api/history', async (_req, res) => {
  try {
    res.json({ items: await loadHistory() });
  } catch (err) {
    res.status(500).json({ error: (err && err.message) || String(err) });
  }
});
app.delete('/api/history', async (_req, res) => {
  try {
    await clearHistory();
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: (err && err.message) || String(err) });
  }
});

// 可用模型 provider（供前端切换）
app.get('/api/providers', (_req, res) => {
  res.json({
    api: apiProviderName(),
    apiConfigured: apiConfigured(),
    ollamaModel: config.ollamaModel,
    deepseekModel: config.deepseekModel,
    openaiModel: config.openaiModel,
  });
});

// 已安装的 Ollama 模型列表（供前端选择）
app.get('/api/ollama/models', async (_req, res) => {
  try {
    const base = config.ollamaBaseUrl.replace(/\/v1\/?$/, '');
    const r = await fetch(`${base}/api/tags`, { signal: AbortSignal.timeout(8000) });
    if (!r.ok) throw new Error(`Ollama 查询失败 HTTP ${r.status}`);
    const data = await r.json();
    const models = (Array.isArray(data.models) ? data.models : [])
      .map((m) => m && m.name)
      .filter(Boolean);
    res.json({ models, current: config.ollamaModel });
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    console.error('[ollama/models]', message);
    res.status(500).json({ error: message });
  }
});

// 按时间搜索 arXiv 最新论文（只返回标题等元信息）
app.get('/api/arxiv/search', async (req, res) => {
  const days = Math.min(Number(req.query.days) || 3, 30);
  const category = String(req.query.category || '');
  const keyword = String(req.query.keyword || '');
  try {
    const papers = await searchArxiv({ days, category, keyword });
    res.json({ days, category, keyword, papers });
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    console.error('[arxiv/search]', message);
    res.status(500).json({ error: message });
  }
});

// 单篇元数据（浏览器版在「有后端同源」时会走这个端点，避免跨域）
app.get('/api/arxiv/meta', async (req, res) => {
  const id = String(req.query.id || '').trim();
  if (!id) return res.status(400).json({ error: '缺少 id 参数' });
  try {
    const meta = await fetchArxivMeta(id);
    if (!meta) return res.status(404).json({ error: `没有找到 arXiv ${id}` });
    res.json({ id, title: meta.title, authors: meta.authors, published: meta.published, url: `https://arxiv.org/abs/${id}` });
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    console.error('[arxiv/meta]', message);
    res.status(500).json({ error: message });
  }
});

// 已配置的公众号账号列表（不含密钥）
app.get('/api/wechat/accounts', (_req, res) => {
  res.json({
    accounts: config.wechatAccounts.map((a) => ({ index: a.index, name: a.name })),
  });
});

// 浏览器兜底：复制内容到剪贴板并打开公众号后台（免 IP 白名单，登录会话持久）
app.post('/api/sync/wechat-browser', async (req, res) => {
  const { title, content } = req.body || {};
  const text = [title, '', content].filter((s) => typeof s === 'string' && s.trim()).join('\n');
  const copied = text ? await copyToClipboard(text) : false;
  const opened = launchWechatBrowser();
  res.json({ ok: true, copied, opened, message: copied ? '标题与文案已复制到剪贴板' : '已打开公众号后台' });
});

// 同步到公众号「贴图」（图文转图模式；多账号）
app.post('/api/sync/wechat', async (req, res) => {
  const { id, filenames, accountIndex } = req.body || {};
  if (!id) return res.status(400).json({ error: '缺少 id 参数' });
  const dir = path.join(config.outputDir, id);
  try {
    const meta = JSON.parse(await readFile(path.join(dir, 'result.json'), 'utf-8'));

    if (!config.wechatAccounts.length) {
      return res.json({
        ok: false,
        needCreds: true,
        message: '未配置公众号凭证（WECHAT_APP_ID / WECHAT_APP_SECRET）',
        imageCount: (meta.images || []).length,
      });
    }

    const account =
      config.wechatAccounts.find((a) => a.index === Number(accountIndex)) ||
      config.wechatAccounts[0];

    // 仅上传选中的图片（只允许 result.json 里存在的文件名，防路径穿越）
    const wanted = Array.isArray(filenames) && filenames.length ? new Set(filenames) : null;
    const images = [];
    for (const img of meta.images || []) {
      if (wanted && !wanted.has(img.filename)) continue;
      images.push({
        filename: img.filename,
        label: img.label,
        buffer: await readFile(path.join(dir, img.filename)),
      });
    }
    if (!images.length) {
      return res.status(400).json({ error: '未选中任何图片' });
    }

    const result = await syncWechatPic({
      account,
      // 文案可能还没生成（两段式：图片先落盘），标题回落到原文标题
      title: meta.title || meta.sourceTitle || '未命名内容',
      content: plainFromMarkdown(meta.copy || ''),
      images,
    });
    res.json({ ok: true, account: account.name, type: 'pic', draft: result });
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    console.error('[sync/wechat]', message);
    res.status(500).json({ error: message });
  }
});


// 统一 JSON 错误处理（multer 文件过大等中间件错误，避免默认返回 HTML 页面导致前端 JSON 解析失败）
app.use((err, _req, res, next) => {
  if (res.headersSent) return next(err);
  const tooLarge = err instanceof multer.MulterError && err.code === 'LIMIT_FILE_SIZE';
  const message = tooLarge
    ? `文件过大：最大允许 ${config.uploadMaxMb} MB`
    : (err && err.message) || String(err);
  console.error('[error]', err && err.stack ? err.stack : message);
  res.status(tooLarge ? 413 : 500).json({ error: message });
});

const server = app.listen(config.port, config.host, () => {
  console.log(`\n  青稞解读 已启动 →  http://127.0.0.1:${config.port}\n`);
  console.log(`  文案引擎: ${config.llmProvider}（配置 .env 后切换真实模型）\n`);
});

async function shutdown() {
  await closeBrowser();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 1000).unref();
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
