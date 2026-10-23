/**
 * 青稞解读 · 纯浏览器版
 * 所有处理都在本机浏览器内完成：PDF 转图（pdf.js）、正文抽取（Readability）、
 * 全部在本机浏览器内运行：PDF 转图用 pdf.js，正文用 Readability，文案直连 LLM。
 * API key 只存在本页内存中，刷新即失效，不写入任何存储。
 */
import { config } from '../src/config.js';
import { openAiCompatibleProvider } from '../src/ai/openaiProvider.js';
import { extractInstitution, extractTerms, extractDateFromText, parsePdfDate } from '../src/meta.js';
import { charCount } from '../src/textUtils.js';
import { fetchText, fetchBytes, isArxivUrl, parseArxivId } from './lib/net.js';
import { pdfToImages } from './lib/pdf.js';
import { zipBlobs, downloadBlob, downloadText } from './lib/zip.js';
import { extractWebArticle } from './lib/web2text.js';
import { composeCopy } from './lib/compose.js';
import { searchArxiv } from './lib/arxiv.js';

const $ = (sel) => document.querySelector(sel);

/* ================= 接口设置（仅内存，不保存） ================= */

const PRESETS = {
  deepseek: { baseUrl: 'https://api.deepseek.com', model: 'deepseek-chat' },
  openai: { baseUrl: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
  custom: { baseUrl: '', model: '' },
};

/** 全局接口配置：只活在这个 JS 模块作用域里，页面一关就没了。 */
const apiCfg = {
  provider: 'deepseek',
  baseUrl: PRESETS.deepseek.baseUrl,
  model: PRESETS.deepseek.model,
  key: '',
  proxy: '',
};

function refreshApiFromInputs() {
  apiCfg.provider = $('#api-provider').value;
  apiCfg.baseUrl = $('#api-baseurl').value.trim();
  apiCfg.model = $('#api-model').value.trim();
  apiCfg.key = $('#api-key').value.trim();
  apiCfg.proxy = $('#api-proxy').value.trim();
  updateApiIndicator();
}

function updateApiIndicator() {
  const el = $('#api-status');
  const state = $('#api-state');
  if (el) {
    el.textContent = apiCfg.key
      ? `已就绪：${apiCfg.baseUrl}（${apiCfg.model}）· key 仅存内存`
      : '未填写 API key：可转图/抽取正文，生成文案与深度解读需要 key';
  }
  if (state) {
    state.textContent = apiCfg.key ? `${apiCfg.provider} · ${apiCfg.model || '…'}` : '未配置';
    state.classList.toggle('ok', !!apiCfg.key);
  }
}

function requireProvider() {
  refreshApiFromInputs();
  if (!apiCfg.key) {
    $('#api-details').open = true; // 缺 key 时自动展开设置面板，引导填写
    throw new Error('请先在侧边「接口设置」里填写 API key（仅存内存，不会保存）');
  }
  if (!apiCfg.baseUrl || !apiCfg.model) throw new Error('请补全 Base URL 与模型名');
  return openAiCompatibleProvider({
    name: apiCfg.provider,
    apiKey: apiCfg.key,
    baseUrl: apiCfg.baseUrl,
    model: apiCfg.model,
  });
}

function initApiPanel() {
  const prov = $('#api-provider');
  const applyPreset = () => {
    const p = PRESETS[prov.value] || PRESETS.custom;
    if (p.baseUrl) $('#api-baseurl').value = p.baseUrl;
    if (p.model) $('#api-model').value = p.model;
    refreshApiFromInputs();
  };
  prov.addEventListener('change', applyPreset);
  ['#api-baseurl', '#api-model', '#api-key', '#api-proxy'].forEach((sel) =>
    $(sel).addEventListener('input', refreshApiFromInputs),
  );
  // 一键填入公共 CORS 代理：只写进输入框（仍然只存内存），不自动启用其它服务
  const proxyFill = $('#api-proxy-fill');
  if (proxyFill) {
    proxyFill.addEventListener('click', () => {
      const input = $('#api-proxy');
      if (!input.value.trim()) input.value = 'https://corsproxy.io/?url=';
      refreshApiFromInputs();
      const note = $('#api-test-note');
      if (note) note.textContent = '已填入公共代理；它只转发公开页面，与 API key 无关。';
    });
  }
  $('#api-test').addEventListener('click', async () => {
    refreshApiFromInputs();
    const note = $('#api-test-note');
    note.textContent = '正在测试连接…';
    try {
      if (!apiCfg.key) throw new Error('未填写 API key');
      const res = await fetch(`${apiCfg.baseUrl.replace(/\/+$/, '')}/models`, {
        headers: { Authorization: `Bearer ${apiCfg.key}` },
        signal: AbortSignal.timeout(20000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json().catch(() => ({}));
      const found = Array.isArray(data?.data) && data.data.some((m) => m.id === apiCfg.model);
      note.textContent = found
        ? `✓ 连接成功，模型「${apiCfg.model}」可用`
        : `✓ 连接成功（模型列表里没看到 ${apiCfg.model}，请以实际调用为准）`;
    } catch (err) {
      note.textContent = '连接失败：' + ((err && err.message) || err);
    }
  });
  refreshApiFromInputs();
}

/* ================= 通用 UI 工具 ================= */

const statusEl = $('#status');
const statusText = $('#status-text');
const errorEl = $('#error');
const resultEl = $('#result');
const submitBtn = $('#submit');
const urlInput = $('#url');

let currentMode = 'pic';
let currentImages = []; // {filename,label,width,height,blob,objectUrl}
const selectedImages = new Set(); // 勾选中的图片 filename
let currentCopy = '';
let currentTitleText = '';
// 三个内容区各自「有没有东西」：空着时不占版面，只显示引导空状态
let hasResult = false;
let hasList = false;

function refreshEmptyState() {
  const anyVisible = (currentMode === 'pic' && hasResult) || (currentMode === 'latest' && hasList);
  $('#empty-state').hidden = anyVisible;
}

function setStatus(msg, show = true) {
  statusEl.hidden = !show;
  statusText.textContent = msg;
}

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
  resultEl.hidden = true;
  refreshEmptyState();
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

function copyText(text) {
  return navigator.clipboard
    ? navigator.clipboard.writeText(text)
    : Promise.reject(new Error('clipboard 不可用'));
}

function escapeHtml(s) {
  return String(s).replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
}

/* ================= 模式切换 ================= */

function setMode(mode) {
  currentMode = mode;
  $('#mode-pic').classList.toggle('active', mode === 'pic');
  $('#mode-latest').classList.toggle('active', mode === 'latest');
  $('#form').hidden = mode === 'latest';
  document.querySelector('.examples').hidden = mode === 'latest';
  $('#upload-row').hidden = mode !== 'pic';
  $('#only-images-row').hidden = mode !== 'pic';
  $('#latest-form').hidden = mode !== 'latest';
  errorEl.hidden = true;
  resultEl.hidden = !(mode === 'pic' && hasResult);
  $('#arxivlist').hidden = !(mode === 'latest' && hasList);
  refreshEmptyState();
  $('#submit').textContent = '生成图文';
  urlInput.placeholder = 'https:// 输入论文 PDF 链接或网页链接';
}
$('#mode-pic').addEventListener('click', () => setMode('pic'));
$('#mode-latest').addEventListener('click', () => setMode('latest'));

document.querySelectorAll('.chip').forEach((chip) => {
  chip.addEventListener('click', () => {
    urlInput.value = chip.dataset.url;
  });
});

/* ================= 图文转图 ================= */

/** PDF / 网页 / 上传 → 图片 + 正文 source（阶段一，不碰模型）。 */
async function prepareFromUrl(url) {
  // arXiv abs 链接在转图模式下直接换成 PDF（能出图）；html 链接当网页抽正文
  const arxivId = parseArxivId(url);
  if (arxivId && /\/abs\//.test(url)) url = `https://arxiv.org/pdf/${arxivId}`;

  const isPdf = /\.pdf($|\?)/i.test(url) || (isArxivUrl(url) && /\/pdf\//.test(url));
  if (isPdf) {
    const buf = await fetchBytes(url, { proxy: apiCfg.proxy });
    return prepareFromPdfBuffer(buf, url, url.split('/').pop() || 'paper.pdf');
  }
  // 网页：Readability 抽正文（浏览器版无法截图，只出文案）
  const html = await fetchText(url, { proxy: apiCfg.proxy });
  const article = extractWebArticle(html, url);
  if (!article.text || article.text.length < 80) {
    throw new Error('正文抽取失败或内容过短：该网页可能需要登录或有反爬，可换链接重试');
  }
  const source = {
    type: 'web',
    title: article.title,
    byline: article.byline || '',
    excerpt: article.excerpt || '',
    text: article.text,
    institution: extractInstitution(article.text),
    date: extractDateFromText(article.text),
    terms: extractTerms(article.text),
    url,
  };
  return { source, images: [], pageCount: 0, webOnly: true };
}

async function prepareFromPdfBuffer(buf, url, filename) {
  const { images, text, textLines, title, author, creationDate, pageCount } = await pdfToImages(buf, {
    maxPages: config.maxPdfPages,
    scale: config.pdfScale,
  });
  const source = {
    type: 'pdf',
    title: title || titleFromPdfText(text) || filename.replace(/\.pdf$/i, ''),
    byline: author || '',
    excerpt: '',
    text,
    textLines,
    institution: extractInstitution(text),
    date: parsePdfDate(creationDate) || extractDateFromText(text.slice(0, 3000)),
    terms: extractTerms(text),
    url,
  };
  return { source, images, pageCount };
}

/** 从 PDF 正文粗取标题（与 Node 版同一套启发式：去 arXiv 头/版权声明，中文标题优先）。 */
function titleFromChineseHead(text) {
  const head = String(text || '')
    .split(/[\w.+-]+@[\w.-]+|\babstract\b|摘要|关键词|arxiv\s*:|https?:\/\//i)[0]
    .replace(/\b\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\b/g, '')
    .replace(/\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b/g, '')
    .trim();
  if (!/[一-鿿]/.test(head)) return '';
  const m = head.match(/^([一-鿿\s·、，,：:]*[A-Za-z][\w-]*)(?:\s+[\s\S]*)?$/);
  const title = (m ? m[1] : head).replace(/\s+/g, ' ').trim();
  return title.length >= 2 && title.length <= 80 ? title : '';
}

function titleFromPdfText(text) {
  let t = String(text || '');
  t = t
    .replace(/^arxiv\s*:\s*\d{4}\.\d{4,5}(v\d+)?\s*(\[[^\]]*\])?[^A-Za-z一-鿿]*/i, '')
    .replace(/provided proper attribution is provided[^.]*\.\s*/i, '')
    .trim();
  const zh = titleFromChineseHead(t);
  if (zh) return zh;
  const titleCase = t.match(/\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){2,4})\b/);
  if (titleCase) return titleCase[1].trim();
  const upper = t.match(/\b([A-Z]{3,}(?:\s+[A-Z]{3,}){1,12})\b/);
  if (upper) return upper[1].trim();
  const first = t.split(/\s{2,}/).find((s) => s.length > 2 && s.length < 140);
  return (first || t).slice(0, 100);
}

/** 打包并下载一组图片（供「下载选中 / 全部」与半自动同步共用）。 */
async function packImages(list, filename) {
  if (!list.length) return null;
  setStatus('正在打包 ZIP…');
  try {
    const blob = await zipBlobs(list.map((i) => ({ name: i.filename, blob: i.blob })));
    downloadBlob(blob, filename);
    return blob;
  } finally {
    setStatus('', false);
  }
}

function pickedImages() {
  return currentImages.filter((i) => selectedImages.has(i.filename));
}

function updateSelCount() {
  const el = $('#sel-count');
  const n = selectedImages.size;
  el.hidden = n === 0;
  el.textContent = `已选 ${n} 张`;
  $('#zip-sel-btn').disabled = n === 0;
}

function setAllSelected(checked) {
  selectedImages.clear();
  if (checked) currentImages.forEach((i) => selectedImages.add(i.filename));
  document.querySelectorAll('#gallery .tile-check').forEach((c) => {
    c.checked = checked;
    c.closest('.tile')?.classList.toggle('selected', checked);
  });
  updateSelCount();
}

$('#sel-all').addEventListener('click', () => setAllSelected(true));
$('#sel-clear').addEventListener('click', () => setAllSelected(false));

function renderPrepared({ source, images, webOnly }) {
  errorEl.hidden = true;
  hasResult = true;
  resultEl.hidden = false;
  refreshEmptyState();
  currentCopy = '';
  currentTitleText = source.title || source.url || '';

  for (const img of currentImages) URL.revokeObjectURL(img.objectUrl);
  currentImages = images.map((img) => ({ ...img, objectUrl: URL.createObjectURL(img.blob) }));

  $('#meta-type').textContent =
    source.type === 'pdf' ? '📄 论文 PDF' : webOnly ? '🌐 网页（仅正文）' : '🌐 网页';
  $('#meta-provider').textContent = apiCfg.key
    ? `文案引擎：${apiCfg.provider}（${apiCfg.model}）`
    : '文案引擎：未配置 key';
  $('#meta-title').textContent = source.title || source.url;
  $('#meta-origin').textContent = [source.institution, source.date].filter(Boolean).join(' · ');

  $('#img-count').textContent = `${currentImages.length} 张`;
  const gallery = $('#gallery');
  gallery.innerHTML = '';
  if (!currentImages.length) {
    gallery.innerHTML =
      '<p class="muted">该来源没有可转的图片（浏览器版不支持网页截图），文案生成后可直接复制。</p>';
  }
  selectedImages.clear();
  updateSelCount();
  currentImages.forEach((img) => {
    const tile = document.createElement('div');
    tile.className = 'tile';
    tile.innerHTML = `
      <input type="checkbox" class="tile-check" title="选择" />
      <img src="${img.objectUrl}" alt="${escapeHtml(img.label)}" loading="lazy" />
      <div class="tile-foot">
        <span>${escapeHtml(img.label)} · ${img.width}×${img.height}</span>
        <button class="tile-dl" type="button">下载</button>
      </div>`;
    const check = tile.querySelector('.tile-check');
    check.checked = selectedImages.has(img.filename);
    check.addEventListener('change', () => {
      if (check.checked) selectedImages.add(img.filename);
      else selectedImages.delete(img.filename);
      tile.classList.toggle('selected', check.checked);
      updateSelCount();
    });
    tile.querySelector('.tile-dl').addEventListener('click', () =>
      downloadBlob(img.blob, img.filename),
    );
    tile.querySelector('img').addEventListener('click', () => openLightbox(img));
    gallery.appendChild(tile);
  });

  $('#zip-btn').onclick = async () => {
    if (!currentImages.length) return toast('没有可打包的图片');
    await packImages(currentImages, 'images.zip');
  };

  $('#zip-sel-btn').onclick = async () => {
    const list = pickedImages();
    if (!list.length) return toast('请先勾选要下载的图片');
    await packImages(list, 'images-selected.zip');
  };

  if (webOnly) {
    $('#copy-notice-text').textContent =
      '浏览器版无法截取网页整页（需要无头 Chrome），已改为抽取正文生成文案；需要网页截图请用本地 Node 版。';
    $('#copy-notice').hidden = false;
  } else {
    $('#copy-notice').hidden = true;
  }

  resultEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function setCopyPending(text) {
  $('#copy-body').hidden = true;
  $('#copy-pending').hidden = false;
  $('#copy-pending-text').textContent = text;
  $('#copy-count').hidden = true;
  $('#copy-copy').disabled = true;
  $('#titles').innerHTML = '';
  $('#titles-empty').hidden = false;
  $('#titles-empty').textContent = text;
}

function renderCopyIdle() {
  $('#copy-pending').hidden = true;
  $('#copy-body').hidden = true;
  $('#copy-count').hidden = true;
  $('#copy-copy').disabled = true;
  $('#copy-notice').hidden = false;
  $('#copy-notice-text').textContent =
    '已选择「只转图」：图片已就绪，可下载 / 打包；需要解读文案时点右侧按钮单独生成。';
  $('#copy-retry').textContent = '生成文案';
  $('#titles').innerHTML = '';
  $('#titles-empty').hidden = false;
  $('#titles-empty').textContent = '还没生成文案，暂无候选标题。';
}

function renderCopyFailure(message) {
  $('#copy-pending').hidden = true;
  $('#copy-body').hidden = true;
  $('#copy-count').hidden = true;
  $('#copy-copy').disabled = true;
  $('#copy-notice').hidden = false;
  $('#copy-retry').textContent = '重新生成文案';
  $('#copy-notice-text').textContent = `文案生成失败：${message}（图片不受影响，可照常下载）`;
  $('#titles').innerHTML = '';
  $('#titles-empty').hidden = false;
  $('#titles-empty').textContent = '文案生成失败，暂无候选标题；图片仍可正常下载。';
}

function renderCopy(generated, source, url) {
  const limits = { maxCopyChars: config.maxCopyChars, maxTitleChars: config.maxTitleChars };
  const composed = composeCopy(generated, source, url, limits);
  currentTitleText = composed.title;
  currentCopy = composed.copy;

  $('#copy-notice').hidden = true;
  $('#copy-pending').hidden = true;
  $('#copy-body').hidden = false;
  $('#copy-count').hidden = false;
  $('#copy-count').textContent = `${charCount(composed.copyContent)} 字 / ≤${limits.maxCopyChars}`;
  $('#copy-render').innerHTML = renderMarkdown(currentCopy);
  $('#copy-src').textContent = currentCopy;
  $('#copy-copy').disabled = false;
  setCopyView('preview');

  const reasoningBox = $('#reasoning');
  if (generated.reasoning) {
    reasoningBox.hidden = false;
    $('#reasoning-text').textContent = generated.reasoning;
  } else {
    reasoningBox.hidden = true;
  }
  renderStyleCheck($('#copy-style'), generated.style);

  const titlesEl = $('#titles');
  titlesEl.innerHTML = '';
  $('#titles-empty').hidden = true;
  composed.titles.forEach((t, i) => {
    const row = document.createElement('div');
    row.className = 'title-row' + (i === 0 ? ' main' : '');
    row.innerHTML = `
      <span class="num">${i + 1}</span>
      <span class="txt">${escapeHtml(t)}</span>
      <span class="cnt">${charCount(t)} 字</span>
      <button class="copy">复制</button>`;
    row.querySelector('.copy').onclick = () =>
      copyText(t).then(() => toast('标题已复制')).catch(() => toast('复制失败'));
    titlesEl.appendChild(row);
  });

  $('#md-btn').onclick = () =>
    downloadText(`# ${composed.title}\n\n${currentCopy}`, 'summary.md');
}

let lastPrepared = null; // {source, url} 供文案重试
async function runCopyGeneration() {
  if (!lastPrepared) return;
  setCopyPending('正在生成解读文案与标题…');
  try {
    const provider = requireProvider();
    const generated = await provider.generate({
      source: lastPrepared.source,
      limits: { maxCopyChars: config.maxCopyChars, maxTitleChars: config.maxTitleChars },
    });
    renderCopy(generated, lastPrepared.source, lastPrepared.url);
  } catch (err) {
    renderCopyFailure((err && err.message) || String(err));
  }
}
$('#copy-retry').addEventListener('click', runCopyGeneration);

$('#copy-copy').addEventListener('click', () =>
  copyText(stripMarkdown(currentCopy)).then(() => toast('文案已复制')).catch(() => toast('复制失败')),
);

/* ================= 最新论文 ================= */

let latestKeyword = '';
document.querySelectorAll('.chip.kw').forEach((chip) => {
  chip.addEventListener('click', () => {
    const kw = chip.dataset.kw;
    latestKeyword = latestKeyword === kw ? '' : kw;
    document.querySelectorAll('.chip.kw').forEach((c) =>
      c.classList.toggle('active', c.dataset.kw === latestKeyword),
    );
    runSearch();
  });
});
$('#latest-search').addEventListener('click', runSearch);

async function runSearch() {
  refreshApiFromInputs();
  const days = $('#latest-days').value;
  const category = $('#latest-category').value;
  errorEl.hidden = true;
  $('#arxivlist').hidden = true;
  setStatus('正在搜索 arXiv…');
  try {
    const papers = await searchArxiv({
      days: Number(days),
      category,
      keyword: latestKeyword,
      proxy: apiCfg.proxy,
    });
    renderArxivList({ papers, days, category, keyword: latestKeyword, source: papers.source || '' });
  } catch (err) {
    showError('搜索失败：' + ((err && err.message) || err));
  } finally {
    setStatus('', false);
  }
}

function renderArxivList(data) {
  hasList = true;
  refreshEmptyState();
  const list = $('#arxivlist');
  list.hidden = false;
  const parts = [`最近 ${data.days} 天`];
  if (data.keyword) parts.push(`关键词「${data.keyword}」`);
  parts.push(data.category || '全部');
  parts.push(`${data.papers.length} 篇`);
  const SOURCE_LABEL = {
    backend: '同源后端',
    openalex: 'OpenAlex（免代理）',
    semanticscholar: 'Semantic Scholar（免代理）',
  };
  if (SOURCE_LABEL[data.source]) parts.push(`来源 ${SOURCE_LABEL[data.source]}`);
  $('#arxivlist-count').textContent = parts.join(' · ');
  const body = $('#arxivlist-body');
  body.innerHTML = '';
  data.papers.forEach((p) => {
    const row = document.createElement('div');
    row.className = 'arxiv-item';
    row.innerHTML = `
      <div class="arxiv-main">
        <a class="arxiv-title" href="${p.url}" target="_blank" rel="noopener">${escapeHtml(p.title)}</a>
        <div class="arxiv-meta">${escapeHtml(p.published || '')}${p.authors ? ' · ' + escapeHtml(p.authors) : ''}</div>
      </div>
      <div class="arxiv-actions">
        <button class="arxiv-act pic" type="button">转图</button>
        <button class="arxiv-act copy" type="button">复制链接</button>
      </div>`;
    row.querySelector('.arxiv-act.pic').onclick = () => usePaper(p.pdfUrl, 'pic');
    row.querySelector('.arxiv-act.copy').onclick = () =>
      copyText(p.url).then(() => toast('arXiv 链接已复制')).catch(() => toast('复制失败'));
    body.appendChild(row);
  });
  list.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function usePaper(url) {
  setMode('pic');
  urlInput.value = url;
  $('#form').requestSubmit();
}

/* ================= 主流程 ================= */

$('#form').addEventListener('submit', async (e) => {
  e.preventDefault();
  refreshApiFromInputs();
  const url = urlInput.value.trim();
  if (!url) return;

  errorEl.hidden = true;
  resultEl.hidden = true;
  $('#arxivlist').hidden = true;
  submitBtn.disabled = true;

  try {
    // 浏览器版只有「图文转图」一条主流程（PDF 转图 + 文案）
    {
      setStatus('正在抓取并转图…');
      const prepared = await prepareFromUrl(url);
      lastPrepared = { source: prepared.source, url };
      renderPrepared(prepared);
      if ($('#only-images').checked || !apiCfg.key) {
        if (apiCfg.key) renderCopyIdle();
        else {
          renderCopyIdle();
          $('#copy-notice-text').textContent =
            '未配置 API key：图片已就绪，可下载 / 打包；需要文案时在上方「接口设置」填 key 后点右侧按钮生成。';
        }
      } else {
        await runCopyGeneration();
      }
    }
  } catch (err) {
    showError('处理失败：' + ((err && err.message) || err));
  } finally {
    submitBtn.disabled = false;
    setStatus('', false);
  }
});

// 上传 PDF / Markdown / 文本
$('#file-upload').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  e.target.value = '';
  refreshApiFromInputs();
  errorEl.hidden = true;
  resultEl.hidden = true;
  submitBtn.disabled = true;
  setStatus('正在读取文件并转图…');
  try {
    let prepared;
    if (/\.pdf$/i.test(file.name)) {
      prepared = await prepareFromPdfBuffer(await file.arrayBuffer(), '', file.name);
    } else {
      const text = await file.text();
      const source = {
        type: 'text',
        title: file.name.replace(/\.(md|markdown|txt)$/i, ''),
        byline: '',
        excerpt: '',
        text,
        institution: extractInstitution(text),
        date: extractDateFromText(text.slice(0, 3000)),
        terms: extractTerms(text),
        url: '',
      };
      prepared = { source, images: [], pageCount: 0 };
    }
    lastPrepared = { source: prepared.source, url: '' };
    renderPrepared(prepared);
    if ($('#only-images').checked || !apiCfg.key) renderCopyIdle();
    else await runCopyGeneration();
  } catch (err) {
    showError('上传处理失败：' + ((err && err.message) || err));
  } finally {
    submitBtn.disabled = false;
    setStatus('', false);
  }
});

/* ================= 同步（纯前端降级版） ================= */

$('#sync-x').onclick = () => {
  const text = currentTitleText || '';
  if (!text) return toast('还没有可用标题');
  window.open('https://twitter.com/intent/tweet?text=' + encodeURIComponent(text), '_blank', 'noopener');
  copyText(text).then(() => toast('已打开 X 发帖框，标题已复制')).catch(() => toast('已打开 X 发帖框'));
};

$('#sync-copy').onclick = () => {
  const text = [currentTitleText, '', stripMarkdown(currentCopy)].filter((x) => x && x.trim()).join('\n');
  if (!text.trim()) return toast('先生成文案再复制');
  copyText(text).then(() => toast('标题与文案已复制')).catch(() => toast('复制失败'));
};

/** 半自动同步：打包选中图片（未勾选则全部）+ 复制标题文案 + 打开公众号后台。 */
$('#sync-wechat-browser').onclick = async () => {
  const list = selectedImages.size ? pickedImages() : currentImages;
  const note = $('#sync-note');
  const text = [currentTitleText, '', stripMarkdown(currentCopy)].filter((x) => x && x.trim()).join('\n');
  let copied = false;
  try {
    if (text.trim()) {
      await copyText(text);
      copied = true;
    }
  } catch {
    /* 复制失败不阻断 */
  }
  if (list.length) {
    try {
      await packImages(list, `wechat-${list.length}张.zip`);
    } catch {
      /* 打包失败不阻断 */
    }
  }
  window.open('https://mp.weixin.qq.com/', '_blank', 'noopener');
  note.textContent = [
    copied ? '标题与文案已复制 ✓' : '请手动复制文案',
    list.length ? `已打包 ${list.length} 张图片（${selectedImages.size ? '选中' : '全部'}）✓` : '没有可打包的图片',
    '后台操作：新建「图片消息」→ 粘贴标题文案 → 上传解压后的图片（顺序即文件名顺序）。',
  ].join(' ｜ ');
  toast('已准备就绪，去公众号后台粘贴');
};

/* ================= Markdown 渲染 / 文风体检 / KaTeX（与 Node 版前端同款） ================= */

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
    if (listType) {
      html += `</${listType}>`;
      listType = null;
    }
  };
  const flushPara = () => {
    if (para.length) {
      html += `<p>${inline(para.join(' '))}</p>`;
      para = [];
    }
  };

  let i = 0;
  while (i < lines.length) {
    const t = lines[i].trimEnd().trim();
    if (!t) {
      flushPara();
      closeList();
      i++;
      continue;
    }
    if (t.startsWith('```')) {
      flushPara();
      closeList();
      const lang = t.replace(/^```/, '').trim().toLowerCase();
      const code = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        code.push(lines[i]);
        i++;
      }
      i++;
      const body = code.join('\n');
      if (lang === 'svg') html += `<div class="diagram">${body}</div>`;
      else html += `<pre><code>${escapeHtml(body)}</code></pre>`;
      continue;
    }
    if (t.startsWith('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      flushPara();
      closeList();
      const header = splitRow(t);
      const body = [];
      let j = i + 2;
      while (j < lines.length && lines[j].trim().startsWith('|')) {
        body.push(splitRow(lines[j]));
        j++;
      }
      let tbl = '<table><thead><tr>';
      header.forEach((h) => {
        tbl += `<th>${inline(h)}</th>`;
      });
      tbl += '</tr></thead><tbody>';
      body.forEach((row) => {
        tbl += '<tr>';
        row.forEach((c) => {
          tbl += `<td>${inline(c)}</td>`;
        });
        tbl += '</tr>';
      });
      tbl += '</tbody></table>';
      html += tbl;
      i = j;
      continue;
    }
    const h = t.match(/^(#{1,4})\s+(.+)$/);
    if (h) {
      flushPara();
      closeList();
      const level = Math.min(h[1].length + 2, 6);
      html += `<h${level}>${inline(h[2])}</h${level}>`;
      i++;
      continue;
    }
    const ul = t.match(/^[-*]\s+(.+)$/);
    if (ul) {
      flushPara();
      if (listType !== 'ul') {
        closeList();
        html += '<ul>';
        listType = 'ul';
      }
      html += `<li>${inline(ul[1])}</li>`;
      i++;
      continue;
    }
    const ol = t.match(/^\d+[.)]\s+(.+)$/);
    if (ol) {
      flushPara();
      if (listType !== 'ol') {
        closeList();
        html += '<ol>';
        listType = 'ol';
      }
      html += `<li>${inline(ol[1])}</li>`;
      i++;
      continue;
    }
    const bq = t.match(/^>\s?(.+)$/);
    if (bq) {
      flushPara();
      closeList();
      html += `<blockquote>${inline(bq[1])}</blockquote>`;
      i++;
      continue;
    }
    para.push(t);
    i++;
  }
  flushPara();
  closeList();
  return html;
}

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

function setCopyView(view) {
  $('#copy-render').hidden = view !== 'preview';
  $('#copy-src').hidden = view !== 'src';
  $('#view-preview').classList.toggle('active', view === 'preview');
  $('#view-src').classList.toggle('active', view === 'src');
}
$('#view-preview').addEventListener('click', () => setCopyView('preview'));
$('#view-src').addEventListener('click', () => setCopyView('src'));

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

const lightbox = $('#lightbox');

function openLightbox(img) {
  $('#lightbox-img').src = img.objectUrl;
  $('#lightbox-dl').onclick = () => downloadBlob(img.blob, img.filename);
  lightbox.hidden = false;
}
$('#lightbox-close').addEventListener('click', () => (lightbox.hidden = true));
lightbox.addEventListener('click', (e) => {
  if (e.target === lightbox) lightbox.hidden = true;
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') lightbox.hidden = true;
});

/* ================= 启动 ================= */

initApiPanel();
setMode('pic');
