# AGENTS.md — 论文解读 AI 播客

## 这是什么

把论文变成双人对谈播客的全栈应用。前端 Vue3 + 后端 FastAPI + DeepSeek（文本解读/脚本）+ 豆包语音播客（音频合成）。
豆包方舟（Ark）作为大模型备选，用 `LLM_PROVIDER` 切换。
**无密钥也能跑**：缺哪个密钥，哪一步就自动降级为 Mock。
**同一集可以内嵌中英两版**（`LANGUAGES=zh,en`），前端切换器切语言，配图跨语言共用。
**V2 起有账号**：每集归属个人、默认只有自己可见；点「分享」拿到免登录公开链接。
另有个人专辑与批量生成。认证实现在 `backend/app/auth.py`。

## 常用命令

```bash
# 系统依赖：视频合成需要 ffmpeg（不是 Python 包）
brew install ffmpeg        # macOS；Linux 用 apt install ffmpeg

# 后端（首次）
cd backend
uv venv --python 3.13 ../.venv
UV_CACHE_DIR=$PWD/../.uvcache uv pip install --python ../.venv/bin/python -r requirements.txt

# 后端开发
cd backend && ../.venv/bin/python -m uvicorn app.main:app --reload --port 8000

# 后端测试（必须全绿后再提交）
cd backend && ../.venv/bin/python -m pytest

# 前端开发（Vite proxy 把 /api 转发到 8000）
cd frontend && pnpm install && pnpm dev

# 前端类型检查 + 构建
cd frontend && pnpm typecheck && VITE_USE_MOCK=1 pnpm build

# 前端纯逻辑自检（目前只有分享文案；Node 自带 TS 剥离，不引第三方依赖）
cd frontend && pnpm test:units
```

## 关键约定

- **`docs/API.md` 是前后端的唯一契约**（当前 v2）。改接口先改它，字段名不许各自发明。
  列表接口要省略 `analysis`/`script` 大字段（否则列表响应会到 MB 级）。
- **新增受保护接口时必须挂鉴权**。`auth_lib.user_or_401(request)` 是统一入口：
  已登录返回 user、未登录且库里一个用户都没有（开放模式）返回 None 放行、否则 401。
  单集/专辑一律用 `_require_episode()` / `_require_album()` 取 —— 它们**顺带校验归属**。
  漏一个资源接口就是一条免费的下载通道，加接口时先想清楚这条。
- **越权返回 404，不返回 403**。403 等于告诉对方「这个 id 真实存在，只是不是你的」，
  拿它枚举一遍就能摸出别人的数据规模。
- **开放模式要留「创建账号」入口**。开放模式（库里还没用户）只是「不强制登录」，
  不是「全部功能都能用」—— 专辑和分享需要一个归属者，所以必须有账号。
  把注册入口也藏起来的话，用户点「新建专辑」会拿到 401 却无处可去，是条死路。
- **免登录白名单只有这几个**：`/api/health`（前端要靠它判该不该跳登录页）、
  `/api/options`（音色目录，首页登录前也要能显示）、`/api/auth/{me,login,register,logout}`、
  `/api/share/*`、`/api/showcase`、`/api/library`（**公开库列表**，`/library` 这个页面
  访客也能看）。往这个名单里加东西前先问一句「这真的可以公开吗」。

  **`/api/library` 只给公开的那一类**：收录条件唯一来源是 `db._PUBLIC_WHERE`
  （`visibility='public'` 且 `share_token IS NOT NULL` 且 `status='completed'`），
  返回的是白名单结构 `PublicEpisodeItem` —— **不含内部 id、user_id、options、
  `source_ref`（那可能是磁盘绝对路径）、raw_text**。它和「我的库」`/api/episodes`
  是两条路：后者按 `user_id` 过滤、还能按专辑筛，公开库一个都不带 ——
  混成一个函数之后，任何一次「顺手加个条件」都可能把别人的私有单集漏出去。
- **密钥只在后端**（`.env`，已被 gitignore）。前端任何地方不得出现
  `DEEPSEEK_API_KEY` / `ARK_API_KEY` / `DOUBAO_*`。
- **Prompt 改动要跑测试**。`backend/app/services/prompts/` 是本产品的核心资产（见下面「代码地图」），
  `tests/test_services.py::TestPromptConstraints` 锁定了「反 AI 腔负面清单」和字数预算，
  别把它当装饰删掉。**解读的字段有四处必须同步**（prompt 里的 JSON / `Analysis` 模型 /
  `_normalize_analysis` / Mock 素材），有测试钉着；**作者机构只许摘自论文原文**——
  猜错的机构比不写更糟（`background` 会照它讲「这是谁的工作」）。
- **音频接口必须支持 Range 请求**。播放器拖动进度条依赖 `206 + Content-Range`，
  改成普通 `FileResponse` 会让 Safari 完全不能播。
- **生成的信息图要走 `<object type="image/svg+xml">` 引用**，不要用 `<img>`
  （部分浏览器不会跑 `<img>` 里 SVG 的 SMIL 动画），也**不要内联进 HTML**
  （内容是模型生成的）。后端已做白名单清洗并加了 CSP 兜底。
- **首页是产品介绍页（`/`），导入表单在 `/create`**。首页的产品展示区读库里最近一期
  已完成的播客，用**真实产物**（视频/音频/脚本/解读/配图）说话；取不到就整区隐藏，
  不要留空播放器。它是全站唯一不套 `.container/.page` 常规节奏的页面（类名前缀 `lp-`）。
- **首页的产物展示要分两阶段加载**。真实后端下详情响应带视频/音频/配图；
  离线演示（Mock）里视频是 MediaRecorder **实时录制**的。等详情回来才渲染，
  首屏会在骨架上停很久 —— 先用列表项（标题/封面/时长/语言）画出来，详情回来再补。
- **列表接口不带 `versions`**（它内部装的就是脚本/解读/视频这些大字段）。
  首页第一阶段只能用顶层字段，别去读 `summary.versions`。
- **首页有两个语言切换器，而且它们互相独立**（`components/LanguageSwitch.vue`）：
  首屏视频上那个**只换首屏那段视频**，成品展示区标题旁那个换整块
  （视频/音频/脚本/解读/信息图）。共用一个状态的话，在首屏切一下会把下面整块也换掉 ——
  那不是「只切换视频」。状态分别放在 `heroLanguage` / `showcaseLanguage`。
- **两个切换器都只列出真的有产物的语言**（`showcaseLanguages` 过滤掉没有音视频的版本），
  加载完之前不显示，免得点过去一片空白。
- **切换语言要用 `:key` 重建 `<video>`/`<audio>`**：两个语言是两份文件，
  不重建会继续放旧的那一份。换语言还要把「正在播」的状态清掉，否则视频元素重建了、
  播放按钮却还是隐藏的，看起来像还在放同一段。
- **成品展示区：封面和论文原图不跟着切**（跨语言共用同一份 PDF），信息图跟（图上写着字）。
- **叠在视频上的切换器 `z-index` 必须高过全屏播放遮罩**（`.lp-shot__play` 是 `inset: 0`
  的按钮）。不给 z-index 点击会被遮罩吃掉 —— 按钮看得见但点了没反应。
- **首屏右下角才是放角标的地方**。左下角被「音频」浮动卡整块盖住，右上角被「配图」浮动卡压着。


## 文档路由：想深入看什么，开哪个文件

AGENTS.md 只留**结论与入口**；推导、实测数据、踩坑的来龙去脉都在 `docs/` 里。
（原来这些全堆在这一个文件里，涨到 69.7 KB、超过工作区的指令预算后被**截断加载**，
也就是说最末尾那几节对 agent 是不可见的 —— 所以拆了。）

| 想深入看什么 | 打开 |
|---|---|
| 踩坑档案（按主题：数据与 PDF / 配图 / 渲染 / 编码 / TTS / 品牌 / 存储…） | `docs/PITFALLS.md` |
| 画面渲染两条路径、Tier B、配图装填、宽图文字跟图、横版竖版 | `docs/RENDERING.md` |
| 动效（弹簧/缓动、转场、字幕换句、声波条、进度条）与影片语法 | `docs/VIDEO_MOTION.md` |
| 逐句动效、图内聚光灯、配图理由（why） | `docs/BEAT_MOTION.md` |
| 成片自查（静止空档 / 响度 / 音画差）与接缝审计 | `docs/VIDEO_QA.md` |
| 动态图解镜头（证据核验 → 模型草案 → 着色渲染） | `docs/DIAGRAM_SHOTS.md` |
| 标题与封面（爆款标题、毛玻璃封面、poster 静帧） | `docs/TITLE_COVER.md` |
| 双语（zh + en） | `docs/BILINGUAL.md` |
| 账号、分享、播客库、分享到 X | `docs/ACCOUNTS.md` |
| 接口契约（唯一权威） | `docs/API.md` |

## 硬规矩（违反会出静默错误的那几条）

1. **`docs/API.md` 是前后端的唯一契约**；改接口先改它。
2. **后端测试必须全绿再提交**（`cd backend && ../.venv/bin/python -m pytest`）；
   前端 `pnpm typecheck && pnpm test:units && pnpm build` —— **别用 mock 构建**，
   那会把本机真实实例切成演示模式。
3. **重新合成不许调模型**（配图分配、要点、封面标题、图解镜头全部复用已存数据）。
4. **改了后端代码先重启常驻 uvicorn**，再谈验证（它没有 `--reload`）。
5. **算出来的东西要进库**才算数：只打日志的指标事后查不到（`video.qa` / `script.length_repair` /
   `video.diagram_report` 都是这种「台账」）。
6. **代价大的动作先量再改**：这个仓库里所有判据都应该是能复算的数字，不是感觉。

## 代码地图：改哪里，开哪个文件

**一次只改一件事，就只读那一个文件。** 每个模块开头的 docstring 都写着「这个模块管什么」。

| 想改什么 | 打开 |
|---|---|
| 解读的字段与口吻（谁的工作 / 研究问题 / 概念通俗解释 / 优缺点） | `services/prompts/analysis.py` |
| 双人脚本怎么写（费曼技巧、作者视角、反 AI 腔、难度档） | `services/prompts/script.py` |
| 字数预算、语速、时长模型 | `services/prompts/units.py` |
| 封面爆款标题（写法 + 清洗 `normalize_hook`） | `services/prompts/cover.py` |
| 画面强调行 + 子图聚光灯的挑法 | `services/prompts/points.py` |
| 逐段选图 + 配图理由 | `services/prompts/assign.py` |
| 脚本写短了 / 写长了怎么补 | `services/prompts/repair.py` |
| 折行、去尾虚词这类文字小工具 | `services/prompts/text.py` |
| 弹簧缓动（过冲多少、公式、ffmpeg/JS 两种形态） | `services/motion.py` |
| 成片自查（静止空档 / 音画长度差）与响度目标 | `services/video_qa.py` / `services/loudness.py` |
| 模型调用、重试、结果解析 | `services/llm.py` / `services/llm_parse.py` |
| 无密钥时的假数据 | `services/llm_mock.py` |
| **维护动作**（批量重合成 / 重跑逐句配图 / 补图解 / 接缝审计） | `python -m app.maintenance.<子命令>`（`app/maintenance/`） |
| 版式与配色令牌（坐标 / 字号 / 颜色 / `Layout`） | `services/design.py` |
| 视频编排：场景、逐句配图、转场、时长对齐 | `services/video.py` |
| 逐句画面动效（什么时候换图/换聚光灯/弹一下，为什么这么定） | `docs/BEAT_MOTION.md` |
| 成片自查与响度（判据怎么标定的、现状多少） | `docs/VIDEO_QA.md` |
| SVG 画面（幻灯片 / 图片卡 / 强调行 / 字幕带 / 片尾） | `services/video_render.py` |
| 视频里的数据类型（`Scene` / `VideoResult` / `VideoError` …） | `services/video_core.py` |
| ffmpeg 编码与静帧 | `services/video_encode.py` |
| HTML 画面 / Chrome 截图 | `services/htmlpage.py` / `htmlframe.py` |
| 流水线各阶段（解析→解读→脚本→TTS→视频） | `services/pipeline.py` |
| 单集接口（建 / 列表 / 详情 / 删除 / 重试） | `api/routes/episodes.py` |
| 账号接口 | `api/routes/auth.py` |
| 专辑接口 | `api/routes/albums.py` |
| 分享与公开页（含免登录资源通道） | `api/routes/share.py` |
| 产物读取（封面 / 原图 / 信息图 / 下载 / Range） | `api/routes/media.py` |
| 封面标题 + 重新合成 + 静帧 | `api/routes/cover.py` |
| 系统信息（health / options / usage / showcase） | `api/routes/system.py` |
| 接口的公共件（router、依赖、序列化、鉴权归属、配额） | `api/routes/shared.py` |
| 账号库（scrypt 口令 / 会话 cookie / 归属判定） | `app/auth.py` |
| 表结构与查询 | `db.py` |
| PDF 抽文本 / 配图与图注 / 子图切分 / 信息图 | `services/{ingest,figures,panels,illustration}.py` |
| 账号以外的前端页面 | `frontend/src/views/*` 与 `components/*` |
| 前端 Mock（离线演示）素材与文案 | `frontend/src/api/mock*.ts` |

**拆分照这三条规矩做，别合回去**：

1. **文案与渲染分开**。文案（prompt + 模型输出的归一化）在 `services/prompts/` 包里；
   渲染在 `video_render.py` / `htmlpage.py`。前者天天改、后者一个月动一次 ——
   混在一个文件里，改一句口吻就得先读一遍滤镜链。
2. **分层单向，别让门面变成必经之路**。视频那条链是
   `design → video_core → video_render → video_encode → video`，
   每一层单独 `import` 都能成立（早期用「在 `video.py` 末尾再导出」换兼容，
   结果 `video_encode` 反向依赖 `video`，直接 import 会炸半初始化模块。
   **不要再用末尾再导出**）。
3. **兼容靠「门面再导出」**：被搬走的名字在 `video.py` / `llm.py` 里再导出一次，
   所以几十处老调用点（含 `tests/test_video.py` 的 60 多个 import）一个字都不用改。
   **不要**为了躲循环导入去写函数内的 import（先把依赖归位）。


## Mock 模式

`Settings.llm_mode` / `tts_mode` 会根据密钥是否齐全自动判断，也可用 `FORCE_MOCK=true` 强制。
前端另有独立的浏览器端 Mock（`VITE_USE_MOCK=1`），不依赖后端，用于静态站点演示。

**本地验证演示版要小心**：`VITE_USE_MOCK=1 pnpm build` 写的是同一个 `frontend/dist`，
而后端正好托管它（`main.py::_mount_frontend`）。也就是说跑一次 mock 构建，
**本地真实实例会当场变成演示模式**（列表里那些真实单集还在，但页面不再调后端）。
验证完记得 `pnpm build` 还原（不用重启 uvicorn，`StaticFiles` 每次请求读盘）。
