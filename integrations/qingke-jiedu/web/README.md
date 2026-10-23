# 青稞解读 · 浏览器版（本目录即静态站点）

这一版把整个应用搬进浏览器：**不需要 Node 后端**，直接部署到 GitHub Pages。

- **API key 在页面上输入，只存内存**：刷新/关闭页面即失效，不写入 localStorage / Cookie / IndexedDB。
- LLM 请求从浏览器**直连**你配置的服务商（DeepSeek / OpenAI / 任意 OpenAI 兼容端点），不经过第三方服务器。
- PDF 转图用 [pdf.js]（CDN），网页正文用 [Readability]，并**零改动复用仓库 `src/` 里的模块**（取源、切片、提示词、文风体检）；`web/index.html` 里的 import map 把 `jsdom` / `node:path` / `dotenv` 等 Node 依赖垫成浏览器实现。

## 功能对照（浏览器版 vs Node 版）

| 功能 | 浏览器版 | Node 版 |
| --- | --- | --- |
| PDF 转图（链接 / 上传） | ✅ pdf.js | ✅ pdf.js |
| 网页转图（整页截图） | ❌（无无头 Chrome，降级为抽正文出文案） | ✅ puppeteer |
| 解读文案 + 爆款标题 | ✅ | ✅ |
| 最新论文搜索 | ✅（填关键词免代理；按分类/日期需 CORS 代理） | ✅ |
| 图片勾选 / 选中打包下载 | ✅ | ✅ |
| 公众号同步 | ⚠️ 半自动：打包选中图 + 复制文案 + 打开后台手动粘贴 | ✅ 凭证直连（access_token → 素材 → 草稿） |
| 论文播客视频 | ❌（需要 TTS / ffmpeg） | ✅ |
| Ollama 本地模型 | ❌（浏览器跨域受限，可自行挂代理） | ✅ |

## 跨域（CORS）说明

实测过的四件事：

- **arXiv 的 HTML 全文页 `/html/<id>` 带 `Access-Control-Allow-Origin: *`**，PDF 同样可直连 → 单篇论文的正文抓取不需要代理；
- `arxiv.org/abs|list|search` 与搜索 API `export.arxiv.org` **都不放 CORS** → 想按「分类 + 精确日期」查最新论文，需要代理；
- `api.openalex.org` **带 CORS** → **填关键词**的搜索可以免代理（返回结果里只保留能定位到 arXiv 的条目）；
- `api.semanticscholar.org` 带 CORS，但无 key 时容易 429，只作兜底。

所以浏览器版的检索顺序是：**同源 Node 后端 → 用户配置的代理 + arXiv 官方 API → OpenAlex（关键词）→ Semantic Scholar**。
每一步失败都会继续下一级，全部失败才会报错，并且错误里会列出「试过哪些渠道、分别为什么失败」以及两条出路
（① 填关键词走免代理搜索；② 在「接口设置 → CORS 代理」填代理，可一键填入公共代理，格式 `https://corsproxy.io/?url=` 或任何含 `{url}` 占位符的地址）。

代理只转发公开页面，与 API key 无关；key 依然只存在内存里。

## 本地预览

```bash
cd link2post
python3 -m http.server 8080   # 或 npx serve .
# 打开 http://127.0.0.1:8080/web/
```

## 冒烟测试（真实 Chrome 跑一遍 PDF→图片）

```bash
# 先起静态服务（上面那条），另开一个终端：
node web/smoke.mjs            # 可用 CHROME_PATH / WEB_BASE / PDF_URL 覆盖默认值
# 期望输出：tiles ≥ 1、标题正确、控制台错误「无」、✅ 冒烟通过
```

> 注意：pdf.js 渲染依赖 `requestAnimationFrame`，**后台/隐藏标签页会被浏览器节流导致卡在「正在抓取并转图…」**。
> 把窗口切到前台，或像上面的冒烟脚本那样带上 `--disable-background-timer-throttling --disable-renderer-backgrounding` 参数启动 Chrome 即可。

## 部署

GitHub 仓库 → Settings → Pages → Build and deployment 选 **Deploy from a branch**，
Branch 选 `main` / 目录选 **`/ (root)`**。站点地址为 `https://<owner>.github.io/<repo>/web/`
（仓库根目录的 `index.html` 会自动跳转过去）。
