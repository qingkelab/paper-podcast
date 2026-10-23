const $ = (sel) => document.querySelector(sel);

if (new URLSearchParams(location.search).get('embed') === '1') {
  document.body.classList.add('is-embedded');

  const sendEmbedHeight = () => {
    const height = Math.max(
      document.documentElement.scrollHeight,
      document.body.scrollHeight,
      document.documentElement.offsetHeight,
    );
    parent.postMessage({ type: 'qingke-embed:resize', height }, '*');
  };
  const embedResizeObserver = new ResizeObserver(sendEmbedHeight);
  embedResizeObserver.observe(document.documentElement);
  embedResizeObserver.observe(document.body);
  new MutationObserver(sendEmbedHeight).observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
  });
  window.addEventListener('load', sendEmbedHeight, { once: true });
  window.addEventListener('resize', sendEmbedHeight);
  requestAnimationFrame(sendEmbedHeight);
}

const form = $('#form');
const urlInput = $('#url');
const submitBtn = $('#submit');
const statusEl = $('#status');
const statusText = $('#status-text');
const errorEl = $('#error');
const resultEl = $('#result');
const podcastResultEl = $('#podcastresult');
let currentId = null;
let currentCopy = '';
let currentTitleText = ''; // 当前可用于分享/预填的标题（阶段一先用原文标题，阶段二换成爆款标题）
let currentZipUrl = '';
let currentMarkdownUrl = '';
let selectedFiles = new Set();
let currentMode = 'pic'; // pic | deep | podcast | latest
let podSource = null; // 论文播客 SSE 事件源（当前活动任务）

function setStatus(msg, show = true) {
  statusEl.hidden = !show;
  statusText.textContent = msg;
}

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
  resultEl.hidden = true;
  podcastResultEl.hidden = true;
  podcastResultEl.hidden = true;
}

function copyText(text) {
  return navigator.clipboard
    ? navigator.clipboard.writeText(text)
    : Promise.reject(new Error('clipboard 不可用'));
}

function toast(msg) {
  const t = document.createElement('div');
  t.textContent = msg;
  Object.assign(t.style, {
    position: 'fixed', bottom: '28px', left: '50%', transform: 'translateX(-50%)',
    background: '#1f2329', color: '#fff', padding: '9px 18px', borderRadius: '999px',
    fontSize: '13px', zIndex: 99, boxShadow: '0 8px 24px rgba(0,0,0,.2)',
  });
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 1800);
}

function triggerDownload(url) {
  const a = document.createElement('a');
  a.href = url;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** 浏览器兜底：复制标题/文案到剪贴板并用 Chrome 打开公众号后台（免 IP 白名单）。 */
async function openWechatBrowser(title, content, noteEl) {
  try {
    const res = await fetch('/api/sync/wechat-browser', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, content }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '打开失败');
    toast(data.copied ? '已打开公众号后台，标题与文案已复制' : '已打开公众号后台，请手动粘贴');
    if (noteEl) noteEl.textContent = '已在浏览器打开公众号后台（登录一次即可、不依赖 IP 白名单），粘贴后手动上传图片发布。';
  } catch (err) {
    const msg = '打开后台失败：' + (err.message || err);
    if (noteEl) noteEl.textContent = msg;
    else toast(msg);
  }
}

function updateSelCount() {
  const el = $('#sel-count');
  const n = selectedFiles.size;
  el.hidden = n === 0;
  el.textContent = `已选 ${n} 张`;
}

function setAllSelected(selected) {
  document.querySelectorAll('#gallery .tile-check').forEach((c) => {
    if (c.checked !== selected) c.click();
  });
}

// 示例 chip
document.querySelectorAll('.chip').forEach((chip) => {
  chip.addEventListener('click', () => {
    urlInput.value = chip.dataset.url;
  });
});

// 模式切换
function setMode(mode) {
  currentMode = mode;
  $('#mode-pic').classList.toggle('active', mode === 'pic');
  $('#mode-podcast').classList.toggle('active', mode === 'podcast');
  $('#mode-latest').classList.toggle('active', mode === 'latest');
  const isLatest = mode === 'latest';
  $('#form').hidden = isLatest;
  document.querySelector('.examples').hidden = isLatest;
  $('#upload-row').hidden = mode !== 'pic';
  $('#latest-form').hidden = !isLatest;
  const podOpts = $('#podcast-options');
  if (podOpts) podOpts.hidden = mode !== 'podcast';
  // 「只转图」开关只对「图文转图」有意义（深度解读 / 播客必须用模型）
  const onlyImagesRow = $('#only-images-row');
  if (onlyImagesRow) onlyImagesRow.hidden = mode !== 'pic';
  resultEl.hidden = mode !== 'pic';
  podcastResultEl.hidden = mode !== 'podcast';
  // 切走播客：中断当前进度订阅（后台任务不受影响）
  if (mode !== 'podcast') closePodSource();
  $('#submit').textContent = mode === 'podcast' ? '生成播客视频' : '生成图文';
  urlInput.placeholder =
    mode === 'podcast'
      ? 'https:// 输入 arXiv 论文链接或文章链接'
      : 'https:// 输入论文 PDF 链接或网页链接';
}
$('#mode-pic').addEventListener('click', () => setMode('pic'));
$('#mode-podcast').addEventListener('click', () => setMode('podcast'));
$('#mode-latest').addEventListener('click', () => setMode('latest'));
setMode('pic');

// 上传文章
$('#file-upload').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  e.target.value = '';
  errorEl.hidden = true;
  resultEl.hidden = true;
  podcastResultEl.hidden = true;
  submitBtn.disabled = true;
  setStatus('正在上传并转图…');
  let imagesReady = false;
  try {
    const fd = new FormData();
    fd.append('file', file);
    // 上传只做阶段一（转图 / 抽取正文），文案由 /api/copy 单独生成
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    renderPrepared(data);
    imagesReady = true;
    if ($('#only-images').checked) {
      renderCopyIdle();
    } else {
      setStatus('文件已转图，正在生成解读文案…');
      await runCopyGeneration(data.id);
    }
  } catch (err) {
    if (imagesReady) renderCopyFailure(err.message || String(err));
    else showError('上传处理失败：' + (err.message || err));
  } finally {
    submitBtn.disabled = false;
    setStatus('', false);
  }
});

// 最新论文搜索
$('#latest-search').addEventListener('click', searchLatest);
let latestKeyword = '';

// 预设关键词：点击即按该关键词搜索，再点取消
document.querySelectorAll('.chip.kw').forEach((chip) => {
  chip.addEventListener('click', () => {
    const kw = chip.dataset.kw;
    latestKeyword = latestKeyword === kw ? '' : kw;
    document.querySelectorAll('.chip.kw').forEach((c) =>
      c.classList.toggle('active', c.dataset.kw === latestKeyword),
    );
    searchLatest();
  });
});

async function searchLatest() {
  const days = $('#latest-days').value;
  const category = $('#latest-category').value;
  errorEl.hidden = true;
  resultEl.hidden = true;
  podcastResultEl.hidden = true;
  $('#arxivlist').hidden = true;
  setStatus('正在搜索 arXiv…');
  try {
    const res = await fetch(
      `/api/arxiv/search?days=${days}&category=${encodeURIComponent(category)}&keyword=${encodeURIComponent(latestKeyword)}`,
    );
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    renderArxivList(data);
  } catch (err) {
    showError('搜索失败：' + (err.message || err));
  } finally {
    setStatus('', false);
  }
}

function renderArxivList(data) {
  errorEl.hidden = true;
  resultEl.hidden = true;
  podcastResultEl.hidden = true;
  const list = $('#arxivlist');
  list.hidden = false;
  const parts = [`最近 ${data.days} 天`];
  if (data.keyword) parts.push(`关键词「${data.keyword}」`);
  parts.push(data.category || '全部');
  parts.push(`${data.papers.length} 篇`);
  $('#arxivlist-count').textContent = parts.join(' · ');
  const body = $('#arxivlist-body');
  body.innerHTML = '';
  data.papers.forEach((p) => {
    const row = document.createElement('div');
    row.className = 'arxiv-item';
    const date = (p.published || '').slice(0, 10);
    row.innerHTML = `
      <div class="arxiv-main">
        <a class="arxiv-title" href="${p.url}" target="_blank" rel="noopener">${escapeHtml(p.title)}</a>
        <div class="arxiv-meta">${escapeHtml(date)}${p.authors ? ' · ' + escapeHtml(p.authors) : ''}</div>
      </div>
      <div class="arxiv-actions">
        <button class="arxiv-act pic" type="button" title="导入到「图文转图」并生成">转图</button>
        <button class="arxiv-act pod" type="button" title="用该论文生成视频播客（写稿+配音+合成）">🎬 视频</button>
        <button class="arxiv-act copy" type="button" title="复制 arXiv 链接">复制</button>
      </div>`;
    row.querySelector('.arxiv-act.pic').onclick = () => usePaper(p.pdfUrl, 'pic');
    row.querySelector('.arxiv-act.pod').onclick = () => runPodcastForUrl(p.url);
    row.querySelector('.arxiv-act.copy').onclick = () =>
      copyText(p.url).then(() => toast('arXiv 链接已复制')).catch(() => toast('复制失败'));
    body.appendChild(row);
  });
  list.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** 把论文链接导入到指定功能（图文转图/深度解读）并自动开始生成。 */
function usePaper(url, mode) {
  setMode(mode);
  urlInput.value = url;
  form.requestSubmit();
}

/** 「最新论文」列表里的视频按钮：用该论文直接跑播客视频生成（不切换模式，结果在下方展示）。 */
async function runPodcastForUrl(url) {
  if (!url) return toast('缺少链接');
  errorEl.hidden = true;
  podcastResultEl.hidden = true;
  closePodSource();
  submitBtn.disabled = true;
  try {
    setStatus('正在建立播客任务…');
    const res = await fetch('/api/podcast', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url,
        provider: currentProvider,
        model: currentProvider === 'ollama' ? ollamaModel || undefined : undefined,
        duration: $('#pod-duration').value || 'auto',
        voice: $('#pod-voice').value || undefined,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    await streamPodcast(data.id);
    loadHistory();
  } catch (err) {
    showError('视频生成失败：' + (err.message || err));
  } finally {
    closePodSource();
    submitBtn.disabled = false;
    setStatus('', false);
  }
}

// 公众号账号列表（单选按钮，渲染到多个容器）
let wechatAccounts = [];
function renderAccountRadios() {
  ['#wechat-account', '#deep-wechat-account'].forEach((sel) => {
    const el = document.querySelector(sel);
    if (!el) return;
    el.innerHTML = '';
    if (!wechatAccounts.length) {
      el.innerHTML = '<span class="muted">未配置公众号凭证</span>';
      return;
    }
    wechatAccounts.forEach((a, i) => {
      const label = document.createElement('label');
      label.className = 'radio-option';
      label.innerHTML =
        `<input type="radio" name="${sel.slice(1)}" value="${a.index}" ${i === 0 ? 'checked' : ''} /> <span>${escapeHtml(a.name)}</span>`;
      el.appendChild(label);
    });
  });
}
function getSelectedAccount(sel) {
  const el = document.querySelector(`${sel} input[type="radio"]:checked`);
  return el ? Number(el.value) : 0;
}
async function loadWechatAccounts() {
  try {
    const res = await fetch('/api/wechat/accounts');
    const data = await res.json();
    wechatAccounts = data.accounts || [];
  } catch {
    wechatAccounts = [];
  }
  renderAccountRadios();
}
loadWechatAccounts();

// 模型切换（Ollama / API）+ Ollama 模型选择
let currentProvider = 'ollama';
let apiProviderName = 'deepseek';
let providerModels = {};
let ollamaModel = ''; // 当前选择的 Ollama 模型
let apiKeyConfigured = true; // API 类模型是否已配置 key（未配置时只能转图）

const ollamaModelSel = $('#ollama-model');

function updateModelIndicator() {
  const el = $('#model-indicator');
  if (!el) return;
  if (currentProvider === 'ollama') {
    el.textContent = `模型：ollama（${ollamaModel || providerModels.ollamaModel || '...'}）`;
  } else if (!apiKeyConfigured) {
    el.textContent = `模型：${currentProvider}（未配置 API key：仍可转图下载，文案生成会报错）`;
  } else {
    el.textContent = `模型：${currentProvider}（${providerModels[currentProvider + 'Model'] || '...'}）`;
  }
}

function setProvider(prov) {
  currentProvider = prov;
  $('#prov-ollama').classList.toggle('active', prov === 'ollama');
  $('#prov-api').classList.toggle('active', prov !== 'ollama');
  $('#ollama-model-row').hidden = prov !== 'ollama';
  updateModelIndicator();
}

async function loadOllamaModels() {
  const prev = ollamaModel; // 保留上次选择，刷新时不重置回默认
  try {
    const res = await fetch('/api/ollama/models');
    if (!res.ok) return;
    const d = await res.json();
    const models = Array.isArray(d.models) ? d.models : [];
    const defaultModel = d.current || '';
    const chosen = models.includes(prev)
      ? prev
      : models.includes(defaultModel)
        ? defaultModel
        : models[0] || defaultModel || '';
    ollamaModelSel.innerHTML = '';
    for (const name of models) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      if (name === chosen) opt.selected = true;
      ollamaModelSel.appendChild(opt);
    }
    if (!models.length) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '未检测到 Ollama 模型（请确认 Ollama 已启动）';
      ollamaModelSel.appendChild(opt);
    }
    ollamaModel = models.length ? chosen : '';
    updateModelIndicator();
  } catch {
    updateModelIndicator();
  }
}
ollamaModelSel.addEventListener('change', () => {
  ollamaModel = ollamaModelSel.value;
  updateModelIndicator();
});
$('#ollama-refresh').addEventListener('click', async () => {
  const btn = $('#ollama-refresh');
  btn.disabled = true;
  btn.classList.add('spin');
  await loadOllamaModels();
  btn.disabled = false;
  btn.classList.remove('spin');
  toast('已重新读取 Ollama 模型列表');
});

async function loadProviders() {
  try {
    const res = await fetch('/api/providers');
    const d = await res.json();
    apiProviderName = d.api || 'deepseek';
    apiKeyConfigured = d.apiConfigured !== false;
    providerModels = {
      ollamaModel: d.ollamaModel,
      deepseekModel: d.deepseekModel,
      openaiModel: d.openaiModel,
    };
    const health = await (await fetch('/api/health')).json();
    currentProvider = health.provider === 'ollama' ? 'ollama' : apiProviderName;
    setProvider(currentProvider);
    await loadOllamaModels();
  } catch {
    updateModelIndicator();
  }
}
$('#prov-ollama').addEventListener('click', () => setProvider('ollama'));
$('#prov-api').addEventListener('click', () => setProvider(apiProviderName));
loadProviders();

// 常驻出站 IP 显示（微信白名单用）
async function loadIpIndicator() {
  try {
    const res = await fetch('/api/ip');
    const d = await res.json();
    const el = $('#ip-indicator');
    if (el) el.textContent = d.ip ? `出站 IP：${d.ip}（白名单用）` : '出站 IP：未知';
  } catch {
    const el = $('#ip-indicator');
    if (el) el.textContent = '出站 IP：未知';
  }
}
loadIpIndicator();

// 历史记录（使用过的链接）
async function loadHistory() {
  try {
    const res = await fetch('/api/history');
    const d = await res.json();
    renderHistory(d.items || []);
  } catch {
    /* ignore */
  }
}
function renderHistory(items) {
  const list = $('#history-list');
  list.innerHTML = '';
  if (!items.length) {
    const li = document.createElement('li');
    li.className = 'history-empty';
    li.textContent = '暂无记录';
    list.appendChild(li);
    return;
  }
  items.forEach((it) => {
    const li = document.createElement('li');
    li.className = 'history-item';
    const when = it.at ? new Date(it.at) : null;
    const time =
      when && !isNaN(when)
        ? `${when.getMonth() + 1}/${when.getDate()} ${String(when.getHours()).padStart(2, '0')}:${String(when.getMinutes()).padStart(2, '0')}`
        : '';
    li.innerHTML = `
      <button class="history-link" type="button" title="${escapeHtml(it.url)}">${escapeHtml(it.title || it.url)}</button>
      <span class="history-time">${time}</span>`;
    li.querySelector('.history-link').onclick = () => {
      setMode(it.type === 'podcast' ? 'podcast' : 'pic');
      urlInput.value = it.url;
      urlInput.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    };
    list.appendChild(li);
  });
}
$('#history-clear').addEventListener('click', async () => {
  try {
    const res = await fetch('/api/history', { method: 'DELETE' });
    if (res.ok) {
      renderHistory([]);
      toast('历史已清空');
    } else {
      toast('清空失败');
    }
  } catch {
    toast('清空失败');
  }
});
loadHistory();

// ===== 论文播客：异步任务 + SSE 进度 =====
let podVoiceNames = {};
function closePodSource() {
  if (podSource) {
    podSource.close();
    podSource = null;
  }
}
function podStageText(p) {
  const s = (p && p.stage) || '';
  const d = (p && p.detail) || '';
  const map = {
    queued: '排队中…',
    extract: d || '正在抽取论文内容与图片…',
    web: '正在抓取网页…',
    pages: '正在渲染 PDF 整页…',
    write: '正在撰写播客脚本…',
    tts: d || '正在合成语音…',
    frame: '正在绘制视频画面…',
    compose: d || 'ffmpeg 正在合成视频…',
  };
  return map[s] || d || '处理中…';
}
/** 订阅播客任务直至终态：done → renderPodcast，fail/断线 → reject。 */
function streamPodcast(jobId) {
  return new Promise((resolve, reject) => {
    closePodSource();
    let settled = false;
    const finish = (err) => {
      if (settled) return;
      settled = true;
      closePodSource();
      err ? reject(err) : resolve();
    };
    const es = new EventSource(`/api/podcast/events?job=${encodeURIComponent(jobId)}`);
    podSource = es;
    es.addEventListener('stage', (ev) => {
      try {
        setStatus(podStageText(JSON.parse(ev.data)));
      } catch {
        /* ignore */
      }
    });
    es.addEventListener('done', (ev) => {
      try {
        const d = JSON.parse(ev.data);
        renderPodcast(d.result);
        loadHistory();
        finish();
      } catch {
        finish(new Error('结果解析失败'));
      }
    });
    es.addEventListener('fail', (ev) => {
      try {
        finish(new Error(JSON.parse(ev.data).message || '播客生成失败'));
      } catch {
        finish(new Error('播客生成失败'));
      }
    });
    es.onerror = () => {
      if (!settled) finish(new Error('进度连接中断，请重试'));
    };
  });
}
async function loadPodcastInfo() {
  try {
    const res = await fetch('/api/podcast/info');
    if (!res.ok) return;
    const info = await res.json();
    const engine = info.engine || 'edge';
    const lists = engine === 'minimax' ? info.minimaxVoices || [] : info.edgeVoices || [];
    podVoiceNames = {};
    [...(info.minimaxVoices || []), ...(info.edgeVoices || [])].forEach((v) => {
      podVoiceNames[v.id] = v.name;
    });
    const sel = $('#pod-voice');
    const prev = sel.value;
    sel.innerHTML = '';
    lists.forEach((v) => {
      const opt = document.createElement('option');
      opt.value = v.id;
      opt.textContent = v.name;
      sel.appendChild(opt);
    });
    if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
    const hint = $('#pod-engine-hint');
    if (hint) {
      hint.textContent = info.minimaxConfigured
        ? `配音引擎：MiniMax 高保真（${engine}）`
        : '配音引擎：Edge 免费 · 配置 MINIMAX_API_KEY 可升级 MiniMax 高保真';
    }
  } catch {
    /* ignore */
  }
}
/** 渲染播客结果（视频播放 + 下载 + 旁白脚本）。 */
function renderPodcast(d) {
  errorEl.hidden = true;
  resultEl.hidden = true;
  podcastResultEl.hidden = true;
  podcastResultEl.hidden = false;
  currentId = d.id;

  $('#pod-title').textContent = d.title || d.source?.title || '';
  const src = d.source || {};
  $('#pod-origin').textContent = [
    src.authors,
    src.institution,
    src.date,
    d.figureCount ? `${d.figureCount} 张配图` : '',
    d.pageCount ? `${d.pageCount} 页画面` : '',
  ].filter(Boolean).join(' · ');

  const voiceLabel = podVoiceNames[d.voice] || d.voice || '';
  $('#pod-meta').textContent = [
    `${Math.round(d.videoDuration || 0)}s`,
    d.totalChars ? `约 ${d.totalChars} 字` : '',
    d.engine,
    voiceLabel,
  ].filter(Boolean).join(' · ');

  const video = $('#pod-video');
  video.src = d.videoUrl;
  $('#pod-download').href = d.videoUrl;
  renderStyleCheck($('#pod-style'), d.style);

  const scriptEl = $('#pod-script');
  scriptEl.innerHTML = '';
  (d.scenes || []).forEach((s, i) => {
    const row = document.createElement('div');
    row.className = 'scene';
    const fig = s.figure != null ? `图 ${s.figure}` : '论文页';
    row.innerHTML =
      `<div class="meta"><b>场景 ${i + 1}</b> · ${escapeHtml(fig)} · ${Math.round(s.duration || 0)}s</div>` +
      `<div>${escapeHtml(s.narration)}</div>`;
    scriptEl.appendChild(row);
  });
  $('#pod-copy').onclick = () => {
    const text = (d.scenes || [])
      .map((s, i) => `【场景 ${i + 1}｜${s.figure != null ? `图 ${s.figure}` : '论文页'}】${s.narration}`)
      .join('\n\n');
    copyText(text).then(() => toast('脚本已复制')).catch(() => toast('复制失败'));
  };
  podcastResultEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
loadPodcastInfo();

// 主流程
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = urlInput.value.trim();
  if (!url) return;

  errorEl.hidden = true;
  resultEl.hidden = true;
  podcastResultEl.hidden = true;
  submitBtn.disabled = true;

  const isPod = currentMode === 'podcast';
  if (!isPod) setStatus('正在抓取并转图…');

  let imagesReady = false; // 图片阶段是否已经成功（决定失败时是整块报错还是只提示文案）
  try {
    const payload = {
      url,
      provider: currentProvider,
      model: currentProvider === 'ollama' ? ollamaModel || undefined : undefined,
    };
    if (isPod) {
      payload.duration = $('#pod-duration').value || 'auto';
      payload.voice = $('#pod-voice').value || undefined;
    }
    const body = JSON.stringify(payload);
    const headers = { 'Content-Type': 'application/json' };

    if (isPod) {
      // 论文播客：建任务 → SSE 实时进度 → done 事件携带结果
      setStatus('正在建立播客任务…');
      const res = await fetch('/api/podcast', { method: 'POST', headers, body });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      await streamPodcast(data.id);
      loadHistory();
    } else {
      // 图文转图：阶段一先拿图片（不依赖模型），阶段二再单独生成文案
      const res = await fetch('/api/images', { method: 'POST', headers, body });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      renderPrepared(data);
      imagesReady = true;
      if ($('#only-images').checked) {
        // 明确只要图片：不调用模型，文案留待需要时一键生成
        renderCopyIdle();
      } else {
        setStatus('图片已就绪，正在生成解读文案…');
        await runCopyGeneration(data.id);
      }
      loadHistory();
    }
  } catch (err) {
    // 图片已经出来时不要把结果区整块换成错误页，只在文案卡片里提示失败
    if (imagesReady) renderCopyFailure(err.message || String(err));
    else showError('处理失败：' + (err.message || err));
  } finally {
    closePodSource();
    submitBtn.disabled = false;
    setStatus('', false);
  }
});

function renderPrepared(d) {
  errorEl.hidden = true;
  resultEl.hidden = false;
  currentId = d.id;
  currentCopy = '';
  currentTitleText = d.source.title || d.source.url || '';
  currentZipUrl = d.zipUrl;
  currentMarkdownUrl = d.markdownUrl;

  $('#meta-type').textContent =
    d.type === 'pdf' ? '📄 论文 PDF' : d.type === 'text' ? '📄 文章' : '🌐 网页';
  $('#meta-provider').textContent = '文案引擎：生成中…';
  $('#meta-title').textContent = d.source.title || d.source.url;
  const origin = [d.source.institution, d.source.date].filter(Boolean).join(' · ');
  $('#meta-origin').textContent = origin;

  // 图片
  selectedFiles = new Set();
  $('#img-count').textContent = `${d.images.length} 张`;
  $('#zip-btn').href = d.zipUrl;
  updateSelCount();
  const gallery = $('#gallery');
  gallery.innerHTML = '';
  if (!d.images.length) {
    gallery.innerHTML = '<p class="muted">该来源没有可转的图片（纯文本输入），文案生成后可直接复制发布。</p>';
  }
  d.images.forEach((img) => {
    const tile = document.createElement('div');
    tile.className = 'tile';
    tile.innerHTML = `
      <input type="checkbox" class="tile-check" data-filename="${img.filename}" title="选择上传" />
      <img src="${img.url}" alt="${img.label}" loading="lazy" />
      <div class="tile-foot">
        <span>${img.label} · ${img.width}×${img.height}</span>
        <a class="tile-dl" href="/download/${d.id}/${img.filename}" download>下载</a>
      </div>`;
    const check = tile.querySelector('.tile-check');
    check.addEventListener('change', () => {
      if (check.checked) selectedFiles.add(img.filename);
      else selectedFiles.delete(img.filename);
      tile.classList.toggle('selected', check.checked);
      updateSelCount();
    });
    tile.querySelector('img').addEventListener('click', () => openLightbox(img));
    gallery.appendChild(tile);
  });

  // 文案区进入「生成中」状态，图片与同步功能不受影响
  setCopyPending('正在生成解读文案与标题…');
  bindSyncActions();

  resultEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/**
 * 阶段二渲染：把生成的文案 / 标题填进已渲染的卡片。
 */
function renderCopy(d) {
  if (d.title && d.title.text) currentTitleText = d.title.text;
  currentZipUrl = d.zipUrl || currentZipUrl;
  currentMarkdownUrl = d.markdownUrl || currentMarkdownUrl;
  if (d.provider) {
    $('#meta-provider').textContent =
      '文案引擎：' + d.provider + (d.model ? '（' + d.model + '）' : '');
  }

  // 文案
  setCopyReady();
  currentCopy = (d.copy && d.copy.text) || '';
  $('#copy-count').textContent = `${d.copy.charCount} 字 / ≤1000`;
  $('#copy-render').innerHTML = renderMarkdown(currentCopy);
  $('#copy-src').textContent = currentCopy;
  const reasoningBox = $('#reasoning');
  if (d.reasoning) {
    reasoningBox.hidden = false;
    $('#reasoning-text').textContent = d.reasoning;
  } else {
    reasoningBox.hidden = true;
  }
  $('#copy-copy').onclick = () =>
    copyText(stripMarkdown(currentCopy)).then(() => toast('文案已复制')).catch(() => toast('复制失败'));
  $('#md-btn').href = d.markdownUrl;
  setCopyView('preview');
  renderStyleCheck($('#copy-style'), d.style);

  // 标题
  $('#title-count').textContent = `每条约 ≤20 字`;
  const titlesEl = $('#titles');
  titlesEl.innerHTML = '';
  d.titles.forEach((t, i) => {
    const row = document.createElement('div');
    row.className = 'title-row' + (i === 0 ? ' main' : '');
    row.innerHTML = `
      <span class="num">${i + 1}</span>
      <span class="txt">${escapeHtml(t.text)}</span>
      <span class="cnt">${t.charCount} 字</span>
      <button class="copy">复制</button>`;
    row.querySelector('.copy').onclick = () =>
      copyText(t.text).then(() => toast('标题已复制')).catch(() => toast('复制失败'));
    titlesEl.appendChild(row);
  });

  // 同步发布
  bindSyncActions();

  resultEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** 文案生成失败：只影响文案卡片，图片 / ZIP 继续可用，并给出重试入口。 */
function renderCopyFailure(message) {
  $('#copy-pending').hidden = true;
  $('#copy-body').hidden = true;
  $('#copy-count').hidden = true;
  $('#copy-copy').disabled = true;
  setMarkdownLink(false);
  $('#copy-retry').textContent = '重新生成文案';
  $('#copy-notice-text').textContent =
    `文案生成失败：${message}（图片不受影响，可照常下载 / 同步；修好模型后点右侧重试）`;
  $('#copy-notice').hidden = false;
  $('#titles').innerHTML = '';
  $('#titles-empty').hidden = false;
  $('#titles-empty').textContent = '文案生成失败，暂无候选标题；图片仍可正常下载。';
}

/** 文案区：待生成（勾了「只转图」时）——图片先可用，留一个「生成文案」入口。 */
function renderCopyIdle() {
  $('#copy-pending').hidden = true;
  $('#copy-body').hidden = true;
  $('#copy-count').hidden = true;
  $('#copy-copy').disabled = true;
  setMarkdownLink(false);
  $('#meta-provider').textContent = '文案引擎：未生成（只转图）';
  $('#copy-retry').textContent = '生成文案';
  $('#copy-notice-text').textContent =
    '已选择「只转图」：图片已就绪，可下载 / 打包 / 同步；需要解读文案时点右侧按钮单独生成。';
  $('#copy-notice').hidden = false;
  $('#titles').innerHTML = '';
  $('#titles-empty').hidden = false;
  $('#titles-empty').textContent = '还没生成文案，暂无候选标题。';
}

/** 文案区：生成中。 */
function setCopyPending(text) {
  $('#copy-notice').hidden = true;
  $('#copy-body').hidden = true;
  $('#copy-pending').hidden = false;
  $('#copy-pending-text').textContent = text;
  $('#copy-count').hidden = true;
  $('#copy-copy').disabled = true;
  setMarkdownLink(false);
  $('#titles').innerHTML = '';
  $('#titles-empty').hidden = false;
  $('#titles-empty').textContent = text;
}

/** 文案区：已生成。 */
function setCopyReady() {
  $('#copy-notice').hidden = true;
  $('#copy-pending').hidden = true;
  $('#copy-body').hidden = false;
  $('#copy-count').hidden = false;
  $('#title-count').hidden = false;
  $('#copy-copy').disabled = false;
  setMarkdownLink(true);
  $('#titles-empty').hidden = true;
}

/** 没有文案时不让「下载 .md」跳到空链接。 */
function setMarkdownLink(enabled) {
  const el = $('#md-btn');
  el.href = enabled ? currentMarkdownUrl || '#' : '#';
  el.setAttribute('aria-disabled', enabled ? 'false' : 'true');
}

/** 同步发布按钮：标题 / 文案缺失时回落到原文标题与图片，图片同步始终可用。 */
function bindSyncActions() {
  const syncNote = $('#sync-note');
  $('#sync-hint').textContent =
    '选公众号账号后，把勾选的图片同步为「贴图」；需认证服务号凭证，否则打包下载图片。X：打开发帖框预填标题。';
  syncNote.textContent = '';

  $('#sync-x').onclick = () => {
    const text = currentTitleText || '';
    if (!text) return toast('还没有可用标题');
    window.open('https://twitter.com/intent/tweet?text=' + encodeURIComponent(text), '_blank', 'noopener');
    copyText(text).then(() => toast('已打开 X 发帖框，标题已复制')).catch(() => toast('已打开 X 发帖框'));
  };

  $('#sync-wechat-browser').onclick = () =>
    openWechatBrowser(currentTitleText, stripMarkdown(currentCopy), syncNote);

  $('#sync-wechat').onclick = async () => {
    if (selectedFiles.size === 0) {
      toast('请先在图片上勾选要上传的图片');
      return;
    }
    syncNote.textContent = '正在生成公众号「贴图」草稿…';
    try {
      const res = await fetch('/api/sync/wechat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: currentId,
          filenames: [...selectedFiles],
          accountIndex: getSelectedAccount('#wechat-account'),
        }),
      });
      const data = await res.json();
      if (res.ok && data.ok) {
        syncNote.textContent = `已同步到「${data.account}」的贴图草稿（media_id: ${data.draft.media_id}），到公众号后台「草稿箱」查看。`;
        toast(`已同步贴图到${data.account}`);
      } else if (data.needCreds) {
        triggerDownload(currentZipUrl);
        window.open('https://mp.weixin.qq.com/', '_blank', 'noopener');
        syncNote.textContent = '未配置公众号凭证：已打包下载图片，请到公众号后台手动上传。';
        toast('已打包图片，请手动上传');
      } else {
        throw new Error(data.error || data.message || '同步失败');
      }
    } catch (err) {
      syncNote.textContent = '同步失败：' + (err.message || err);
    }
  };
}

/**
 * 阶段二调用：按 id 生成文案。
 * 失败只在文案卡片内提示（图片 / ZIP / 同步都不受影响），并保留重试入口。
 */
async function runCopyGeneration(id) {
  if (!id) return false;
  try {
    const res = await fetch('/api/copy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id,
        provider: currentProvider,
        model: currentProvider === 'ollama' ? ollamaModel || undefined : undefined,
      }),
    });
    let data = {};
    try {
      data = await res.json();
    } catch {
      /* 非 JSON 响应，用 HTTP 状态兜底 */
    }
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    renderCopy(data);
    return true;
  } catch (err) {
    renderCopyFailure(err.message || String(err));
    toast('文案生成失败，图片仍可下载');
    return false;
  }
}

// 文案重试：复用已落盘的图片与正文，只重跑模型那一步
$('#copy-retry').addEventListener('click', async () => {
  if (!currentId) return;
  const btn = $('#copy-retry');
  btn.disabled = true;
  setCopyPending('正在重新生成解读文案…'); // 顺手把重试入口藏起来
  await runCopyGeneration(currentId);
  btn.disabled = false;
});

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/**
 * 证据审计面板：展示全文切片规模、研究地图来源、审计各项结论与定点修复次数。
 * 只做展示，不进入正文（Markdown 里不含审计噪声）。
 */
function renderStyleCheck(el, style) {
  if (!el) return;
  const m = style && style.metrics;
  if (!m) {
    el.hidden = true;
    el.innerHTML = '';
    return;
  }
  el.hidden = false;
  const chips = [
    `${style.profile || '文风'}体检`,
    `段落 ${m.paragraphs} 段 · 平均 ${m.avgParaChars} 字`,
    `最长段 ${m.maxParaChars} 字`,
    `平均句长 ${m.avgSentenceChars} 字`,
    `感叹号 ${m.exclamations}`,
    `破折号 ${m.dashes}`,
    `AI 味词 ${m.aiPhraseTotal}`,
    `慎用词 ${m.cautionPhraseTotal || 0}`,
    `边界词 ${m.boundaryPhraseTotal || 0}`,
    `AI 腔 ${m.aiTonePer1k != null ? m.aiTonePer1k : 0}/千字`,
    `加粗锚点 ${m.boldAnchors}`,
  ];
  const warns = (style.warnings || [])
    .map(
      (w) =>
        `<div class="style-warn ${w.level === 'warn' ? 'bad' : 'soft'}">${w.level === 'warn' ? '⚠️' : 'ℹ️'} ${escapeHtml(w.text)}</div>`,
    )
    .join('');
  el.innerHTML = chips.map((c) => `<span class="style-chip">${escapeHtml(c)}</span>`).join('') + warns;
}

/** 用 KaTeX 渲染容器内的 LaTeX（$...$ 行内 / $$...$$ 块级），未加载时静默跳过。 */
function renderMath(el) {
  if (!el || !window.renderMathInElement) return;
  try {
    window.renderMathInElement(el, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '$', right: '$', display: false },
      ],
      throwOnError: false,
    });
  } catch {
    /* ignore */
  }
}

/** 极简 Markdown → HTML（标题/加粗/列表/引用/代码/表格/围栏代码，先转义防 XSS）。 */
function renderMarkdown(md) {
  const inline = (s) => {
    let t = escapeHtml(s);
    t = t.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img src="$2" alt="$1" loading="lazy" />');
    t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
    return t;
  };
  const splitRow = (line) => {
    let t = line.trim();
    if (t.startsWith('|')) t = t.slice(1);
    if (t.endsWith('|')) t = t.slice(0, -1);
    return t.split('|').map((c) => c.trim());
  };
  const isTableSep = (line) => {
    const cells = splitRow(line);
    return cells.length > 0 && cells.every((c) => /^:?-{2,}:?$/.test(c));
  };

  const lines = String(md || '').replace(/\r\n?/g, '\n').split('\n');
  let html = '';
  let listType = null;
  let para = [];
  const closeList = () => {
    if (listType) { html += `</${listType}>`; listType = null; }
  };
  const flushPara = () => {
    if (para.length) { html += `<p>${inline(para.join(' '))}</p>`; para = []; }
  };

  let i = 0;
  while (i < lines.length) {
    const t = lines[i].trimEnd().trim();
    if (!t) { flushPara(); closeList(); i++; continue; }

    // 围栏代码块 ```...```（```svg 直接渲染为图形）
    if (/^```/.test(t)) {
      flushPara(); closeList();
      const lang = t.replace(/^```/, '').trim().toLowerCase();
      const code = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) { code.push(lines[i]); i++; }
      i++; // 跳过结束 ```
      const body = code.join('\n');
      if (lang === 'svg') html += `<div class="diagram">${body}</div>`;
      else html += `<pre><code>${escapeHtml(body)}</code></pre>`;
      continue;
    }

    // 表格
    if (t.startsWith('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      flushPara(); closeList();
      const header = splitRow(t);
      const body = [];
      let j = i + 2;
      while (j < lines.length && lines[j].trim().startsWith('|')) { body.push(splitRow(lines[j])); j++; }
      let tbl = '<table><thead><tr>';
      header.forEach((h) => { tbl += `<th>${inline(h)}</th>`; });
      tbl += '</tr></thead><tbody>';
      body.forEach((row) => {
        tbl += '<tr>';
        row.forEach((c) => { tbl += `<td>${inline(c)}</td>`; });
        tbl += '</tr>';
      });
      tbl += '</tbody></table>';
      html += tbl;
      i = j;
      continue;
    }

    const h = t.match(/^(#{1,4})\s+(.+)$/);
    if (h) {
      flushPara(); closeList();
      const level = Math.min(h[1].length + 2, 6);
      html += `<h${level}>${inline(h[2])}</h${level}>`;
      i++; continue;
    }
    const ul = t.match(/^[-*]\s+(.+)$/);
    if (ul) {
      flushPara();
      if (listType !== 'ul') { closeList(); html += '<ul>'; listType = 'ul'; }
      html += `<li>${inline(ul[1])}</li>`;
      i++; continue;
    }
    const ol = t.match(/^\d+[.)]\s+(.+)$/);
    if (ol) {
      flushPara();
      if (listType !== 'ol') { closeList(); html += '<ol>'; listType = 'ol'; }
      html += `<li>${inline(ol[1])}</li>`;
      i++; continue;
    }
    const bq = t.match(/^>\s?(.+)$/);
    if (bq) {
      flushPara(); closeList();
      html += `<blockquote>${inline(bq[1])}</blockquote>`;
      i++; continue;
    }
    para.push(t);
    i++;
  }
  flushPara();
  closeList();
  return html;
}

/** 去掉 Markdown 符号，得到可粘贴的纯文本。 */
function stripMarkdown(md) {
  return String(md || '')
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map((l) =>
      l
        .replace(/^\s*#{1,4}\s+/, '')
        .replace(/^\s*>\s?/, '')
        .replace(/^\s*[-*]\s+/, '• ')
        .replace(/^\s*\d+[.)]\s+/, ''),
    )
    .join('\n')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .trim();
}

/** 切换文案「预览 / Markdown 源码」视图。 */
function setCopyView(view) {
  $('#copy-render').hidden = view !== 'preview';
  $('#copy-src').hidden = view !== 'src';
  $('#view-preview').classList.toggle('active', view === 'preview');
  $('#view-src').classList.toggle('active', view === 'src');
}

// Lightbox
const lightbox = $('#lightbox');
const lightboxImg = $('#lightbox-img');
const lightboxDl = $('#lightbox-dl');

// 文案视图切换
$('#view-preview').addEventListener('click', () => setCopyView('preview'));
$('#view-src').addEventListener('click', () => setCopyView('src'));

// 图片选择
$('#sel-all').addEventListener('click', () => setAllSelected(true));
$('#sel-clear').addEventListener('click', () => setAllSelected(false));

function openLightbox(img) {
  lightboxImg.src = img.url;
  lightboxDl.href = `/download/${currentId}/${img.filename}`;
  lightbox.hidden = false;
}

$('#lightbox-close').addEventListener('click', () => (lightbox.hidden = true));
lightbox.addEventListener('click', (e) => {
  if (e.target === lightbox) lightbox.hidden = true;
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') lightbox.hidden = true;
});
