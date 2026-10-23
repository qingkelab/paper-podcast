# 青稞解读 · QingKe JieDu

输入论文 / 网页链接，一键产出：

- **🖼 图文转图**：PDF / 网页 → 逐页图片 + 1000 字内解读文案 + 20 字内爆款标题，可一键同步到公众号「贴图」（多账号 + 主题配色）。
- **🎙️ 论文播客**：论文链接 → 3–5 分钟第一人称科普视频（AI 写稿 → 语音合成 → ffmpeg 合成 mp4，论文图/页面作画面）。
- **🕒 最新论文**：按时间 + 分类 + 关键词检索 arXiv，列表里直接「转图 / 🎬 视频 / 复制」。

两种形态，同一套代码：

| 形态 | 地址 / 启动 | 能力 |
| --- | --- | --- |
| **Node 服务**（本机或服务器） | `npm start` → http://127.0.0.1:4780 | 全部功能（含网页截图、播客视频、公众号同步） |
| **浏览器版**（纯静态，已部署） | https://qingkelab.github.io/qingke-jiedu/web/ | PDF 转图 + 文案 + arXiv 检索；key 只存内存（对照表见下文） |

## 快速开始

环境要求：Node.js ≥ 20；网页截图默认用系统 Chrome（`CHROME_PATH` 可覆盖）；播客需要 `ffmpeg`。

```bash
npm install
cp .env.example .env      # 填 DEEPSEEK_API_KEY 或 OPENAI_API_KEY（本机 Ollama 亦可）
npm start                 # 浏览器打开 http://127.0.0.1:4780
```

质量门禁：

```bash
npm run dev       # node --watch 热重载
npm run lint      # oxlint（0 error / 0 warning；.oxlintrc.json 开启 no-undef，能拦住未定义引用）
npm test          # node --test（单元 + 服务端冒烟）
npm run check     # lint + test
npm run smoke:web # 真实 Chrome 跑一遍浏览器版 PDF→图片（需先起静态服务，见下）
```

「图文转图」分两个阶段、两个独立接口：模型没配 key / 超时 / 报错时**只影响文案**，
图片 / ZIP / summary.md 照常产出，页面上可一键重试补文案；侧边栏还有「只转图，不生成文案」开关（完全不调用模型）。

## 配置

全部通过 `.env` / 环境变量，示例见 `.env.example`：

| 分组 | 关键项 |
| --- | --- |
| 文案引擎 | `LLM_PROVIDER=deepseek\|openai\|ollama`、`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`OLLAMA_BASE_URL` / `OLLAMA_MODEL` |
| 生成约束 | `MAX_COPY_CHARS=1000`、`MAX_TITLE_CHARS=20`、`QUALITY_REVIEW=0`（关掉生成后自检自修） |
| 渲染 | `MAX_PDF_PAGES`、`PDF_SCALE`、`WEB_VIEWPORT_WIDTH`、`WEB_SEGMENT_HEIGHT`、`CHROME_PATH` |
| 播客配音 | `TTS_ENGINE=auto\|minimax\|edge`、`TTS_RATE=+8%`、`MINIMAX_API_KEY`、`PODCAST_MAX_SECONDS=310`、`VIDEO_WIDTH/HEIGHT`、`XFADE_SECONDS` |
| 公众号同步 | `WECHAT_APP_ID/SECRET/NAME/THEME`，第 2 个账号用 `WECHAT2_*`（最多 5 个） |
| 其他 | `PORT=4780`、`OUTPUT_DIR`、`FETCH_TIMEOUT_MS`、`UPLOAD_MAX_MB` |

## 浏览器版（web/，可直接部署到 GitHub Pages）

仓库里的 `web/` 是把整个应用搬进浏览器的**纯静态版本**，访问 https://qingkelab.github.io/qingke-jiedu/web/ 即可使用：

- **API key 在页面输入、只存内存**：刷新/关闭即失效，不写 localStorage / Cookie / IndexedDB；LLM 请求从浏览器直连服务商。
- **零改动复用 `src/`**：`web/index.html` 的 import map 把 `jsdom` / `node:path` / `dotenv` 垫成浏览器实现，取源、切片、提示词、文风体检等模块与 Node 版共用同一份代码。
- PDF 转图走 [pdf.js]（CDN），网页正文走 [Readability]，arXiv 检索有多级 CORS 回退（同源后端 → 自配代理 → OpenAlex 免代理）。
- 图片支持**勾选 / 全选 / 清空**与「下载选中（ZIP）」；「📦 准备同步」会**打包选中图片 + 复制标题文案 + 打开公众号后台**，粘贴即可发布。

| 功能 | 浏览器版 | Node 版 |
| --- | --- | --- |
| PDF 转图（链接 / 上传） | ✅ pdf.js | ✅ pdf.js |
| 网页转图（整页截图） | ❌（降级为抽正文出文案） | ✅ puppeteer |
| 解读文案 + 爆款标题 | ✅（key 只存内存） | ✅ |
| 最新论文检索 | ✅ | ✅ |
| 论文播客视频 | ❌（需 ffmpeg / TTS） | ✅ |
| 图片勾选 / 选中打包 | ✅ | ✅ |
| 公众号同步 | ⚠️ 半自动（打包选中图 + 复制文案 + 打开后台手动粘贴） | ✅ 凭证直连（access_token → 素材 → 草稿） |
| Ollama 本地模型 | ⚠️ 视 CORS 而定（浏览器直连 localhost 可行） | ✅ |

本地预览与冒烟：

```bash
python3 -m http.server 8080     # 或 npx serve .
open http://127.0.0.1:8080/web/
node web/smoke.mjs              # 真实 Chrome 跑一遍 PDF→图片（需要 CHROME_PATH）
```

> pdf.js 渲染依赖 `requestAnimationFrame`：页面处于后台/隐藏状态会被浏览器节流，表现为卡在「正在抓取并转图…」，切到前台即可。

## 目录结构

```
server.js              Express 主服务（路由 + SSE 进度 + 产物静态服务）
public/                Node 版前端：模式导航、图库、文案、播客进度与播放
web/                   浏览器版（纯静态，Pages 直接发布）
  app.js               前端主逻辑（表单式 key 面板 / 转图 / 文案 / 检索）
  lib/                 pdf.js 封装、抓取（CORS 多级回退）、arXiv 检索、ZIP、Readability
  shims/               jsdom / node:path / node:url / dotenv 的浏览器垫片
  smoke.mjs            端到端冒烟（真实 Chrome 跑 PDF→图片）
src/
  pipeline.js          「图文转图」主流程：抓取 → 转图 → 抽取正文 → 文案
  podcast/             论文播客：素材 → 写稿 → 配音 → 画面 → ffmpeg 合成
  ai/                  LLM provider（deepseek / openai / ollama）+ JSON 解析
  arxiv.js             arXiv API 元信息（标题 / 作者 / 时间）
  arxivSearch.js       arXiv 最新论文检索
  arxivHtml.js         arXiv HTML 版：正文 + 图片（img / object-svg / 内联 svg）
  arxivSource.js       取源三级回退：HTML → TeX 源码（e-print）→ PDF
  chunker.js           结构化切片（HTML / TeX / PDF 统一成 section + chunk）
  fetchSource.js       链接抓取（PDF / HTML）
  extractText.js       网页正文抽取（Readability）
  pdfjs.js pdfToImages.js   PDF → PNG（页面图）+ 元信息 / 正文抽取
  webToImages.js       网页整页截图（长页分段）+ SVG 栅格化（复用同一个 Chrome）
  markdown.js          Markdown → 纯文本 / 公众号主题 HTML
  wechat.js            公众号草稿：贴图（newspic，多账号）
  wechatBrowser.js     浏览器兜底：复制文案 + 打开公众号后台（免 IP 白名单）
  styleCheck.js        文风体检（段落粒度 / 句长 / 标点密度 / AI 味词）
  store.js history.js memory.js meta.js textUtils.js typography.js config.js
test/                  node:test 测试（单元 + 服务端冒烟）
output/                产物：图片 / zip / md / 播客 mp4（已 gitignore）
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/process` | 链接 → 图片 + 文案 + 标题（一步到位） |
| POST | `/api/images` | 链接 → 只转图（返回 id，供后续单独生成文案） |
| POST | `/api/copy` | 按 id 生成 / 重新生成文案与标题 |
| POST | `/api/upload` | 上传 PDF / Markdown / 文本，同 `/api/process` |
| POST | `/api/podcast` | 建播客任务 → **立即返回 `{id}`**，用 SSE 收进度 |
| GET | `/api/podcast/events?job=<id>` | SSE：`snapshot` / `stage` / `done`（含 result）/ `fail` |
| GET | `/api/podcast/info` | 配音引擎与可用音色（前端选择用） |
| GET | `/api/podcast/files/:id/podcast.mp4` | 播客视频产物 |
| GET | `/api/arxiv/search` | 最新论文检索（days / category / keyword） |
| GET | `/api/arxiv/meta` | arXiv 论文元信息 |
| POST | `/api/sync/wechat` | 同步选中图片为公众号「贴图」草稿 |
| POST | `/api/sync/wechat-browser` | 浏览器兜底：复制文案 + 打开公众号后台 |
| GET | `/api/wechat/accounts` | 已配置公众号账号（不含密钥） |
| GET | `/api/providers`、`/api/ollama/models` | 可用模型与本地 Ollama 模型列表 |
| GET | `/api/history`、`DELETE /api/history` | 历史记录 |
| GET | `/api/health`、`/api/ip` | 健康检查、出站 IP（微信白名单用） |
| GET | `/files/:id/:name`、`/download/:id/:name` | 图片内联预览 / 强制下载 |
| GET | `/_arxivsrc/<id>/<file>` | TeX 源码图 / SVG 栅格化后的本地素材 |

## 播客工作流

```
链接 ─┬─ arXiv：HTML 取源（正文 + 图）→ 失败回退 TeX 源码（e-print）→ 再回退 PDF
      └─ 普通网页：正文抽取 + 整页截图
        ↓
   写稿模型：一次产出 8–12 个场景（旁白 + 引用哪张图），字数不足自动扩写一次
        ↓
   TTS：有 MINIMAX_API_KEY 用 MiniMax（speech-02-hd），否则 edge-tts；逐场景 mp3 + ffprobe 取时长，超长自动加速重录
        ↓
   画面：canvas 合成 1920×1080 静态帧（封面 / 论文页 / 配图 / 结尾卡）
        ↓
   ffmpeg：逐场景「静帧 + 旁白」→ 视频 xfade、音频 acrossfade 串成全片 mp4
```

进度通过 SSE 实时回传（抽取 → 写稿 → 配音 i/n → 画面 → 合成），前端状态区逐条显示；
「最新论文」列表里点「🎬 视频」也会走同一条链路，结果直接展示在列表下方。

## 同步发布

- **公众号贴图**：前端勾选图片 → `POST /api/sync/wechat` → 草稿箱。需要认证服务号凭证，且调用 **IP 必须在公众号后台的 IP 白名单** 内（当前出站 IP 显示在页面左下角）。
- **浏览器兜底**：`POST /api/sync/wechat-browser` 复制标题 / 文案并打开公众号后台（持久化 Chrome 会话，免白名单）。
- **X**：前端直接打开预填标题的发帖框。

## Docker 运行

```bash
docker compose up --build      # 含 ollama 服务，首次会自动拉模型
# 浏览器打开 http://127.0.0.1:4780
```

## 部署

- **GitHub Pages（已启用）**：发布源为 `main / (root)`，根目录 `index.html` 是落地页，`/web/` 是浏览器版；push 即自动发布（无需构建）。
- **Render（可选）**：使用仓库自带的 `render.yaml`（Docker 运行时，新加坡节点）；需在 Render 面板创建服务或配置 Deploy Hook。
- **Railway**：连仓库即可，Dockerfile 已就绪。
- **CI**：`.github/workflows/deploy.yml` 在 push / PR 时执行 `npm run lint` + `npm test`，并构建一次 Docker 镜像，提前发现镜像问题。

## 局限

- 部分站点有反爬（登录墙、Cloudflare 校验）会抓取失败；网页截图依赖 `networkidle2`，个别懒加载内容可能未展开。
- 文案生成需要真实模型；未配置或调用失败时**只缺文案**，图片 / ZIP 照常产出，可在页面上重试。
- 公众号 API 同步受 IP 白名单限制，换机器或宽带 IP 变化后需在后台更新白名单。
- 播客依赖本机 `ffmpeg` 与 `edge-tts`（或 `MINIMAX_API_KEY`）；Windows 需要自行安装 ffmpeg。
- **浏览器版**：网页整页截图、播客视频、公众号凭证同步三项做不了（浏览器能力所限），分别降级为「抽正文出文案」「无」「复制 + 打开后台」；
  生成时请让页面保持前台 —— pdf.js 渲染依赖 `requestAnimationFrame`，后台标签页会被节流而看起来"卡住"。
- **浏览器版跨域**：arXiv 的 `/html/<id>` 与 PDF 带 CORS 可直连；分类/日期检索需要自配 CORS 代理，或改用「关键词」搜索（走 OpenAlex，免代理）。
