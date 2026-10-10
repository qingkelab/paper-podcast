# 前后端接口契约（冻结版 v2）

后端：FastAPI，默认 `http://127.0.0.1:8000`，所有业务接口前缀 `/api`。
前端：Vue3 + Vite，开发态用 Vite proxy 把 `/api` 转发到后端。

> 本文件是前后端唯一契约。前端不得依赖契约之外的字段；后端新增字段必须保持向后兼容。

---

## 1. 数据模型

### Episode（播客单集）

```jsonc
{
  "id": "3f2a9c1e",                     // 8位短id
  "title": "Attention Is All You Need", // 论文标题，解析失败时回退为用户输入/文件名
  "source_type": "pdf",                 // "pdf" | "url" | "text"
  "source_ref": "attention.pdf",        // 文件名 / URL / null
  "status": "completed",                // 见 §3 状态机
  "stage_label": "已完成",               // 中文阶段描述，直接用于UI显示
  "progress": 100,                      // 0-100 整数
  "error": null,                        // 失败原因，成功时为 null
  "options": {
    "duration_min": 5,                  // 3 | 5 | 10
    "level": "intro",                   // "intro" | "advanced" | "expert"
    "voice_a": "zh_male_dayixiansheng_v2_saturn_bigtts",
    "voice_b": "zh_female_mizaitongxue_v2_saturn_bigtts",
    "language": "zh",                   // 主语言
    "languages": ["zh"]                 // 这一集实际产出的语言版本
  },
  "paper_meta": {                       // 全部字段可为 null
    "title": "...", "authors": ["..."], "abstract": "...",
    "year": 2017, "venue": "NeurIPS", "arxiv_id": "1706.03762",
    "keywords": ["Transformer", "Self-Attention"]
  },
  "analysis": {                         // 结构化论文解读，见 §4
    "background": "...", "innovations": ["..."], "method": "...",
    "experiments": "...", "conclusion": "...", "limitations": ["..."],
    "value": "...", "future": ["..."]
  },
  "script": {                           // 双人播客脚本
    "segments": [
      { "speaker": "A", "text": "欢迎收听……", "round": 0 }
    ],
    "word_count": 1840,
    "est_duration_sec": 300
  },
  "cover_url": "/api/episodes/3f2a9c1e/cover",  // 封面：PDF 第一页渲染图；无则 null
  "cover_width": 935,
  "cover_height": 1210,
  "figures": [                                  // 从论文 PDF 提取的原图/表
    // caption 是**整段图注**（跨行），不是只有标签那一行：图注里的 `(a) (b) (c)` 枚举
    // 是「这张图有几个子图」的唯一可靠来源，视频靠它决定聚光灯框哪一块。
    // 子图标注的位置（panel_marks）是内部数据，**不在响应里**（见下面视频那节的说明）。
    { "id": "f1", "kind": "figure", "label": "Figure 1",
      "caption": "Figure 1: Why recurrent state needs compressed state. (a) … (b) … (c) …",
      "page": 3,
      "url": "/api/episodes/3f2a9c1e/figures/f1",
      "width": 473, "height": 690 }
  ],
  "illustration": {                             // 模型生成的信息图
    "png_url": "/api/episodes/3f2a9c1e/illustration.png",
    "svg_url": "/api/episodes/3f2a9c1e/illustration.svg",
    "width": 1280, "height": 720,
    "source": "model"                           // "model" | "fallback"
  },
  "video": {                                    // 视频解读播客，无则 null
    "url": "/api/episodes/3f2a9c1e/video",
    "duration_sec": 206.86,
    "scene_count": 23,
    "bytes": 4801376,
    // 封面上的大字标题（爆款标题，**逐语言各一份**）。空串 = 没自定义过，
    // 封面会退回显示论文原题。可以用 PATCH /cover 改，**改完要重新合成才生效**
    "hook": "4 比特超 INT8，显存降七成"
  },
  "audio_url": "/api/episodes/3f2a9c1e/audio",  // 无音频时 null
  "audio_duration_sec": 302.5,
  "audio_bytes": 2411724,
  "created_at": "2025-01-01T12:00:00+00:00",
  "updated_at": "2025-01-01T12:03:11+00:00"
}
```

### 双语版本（bilingual）

每集可以有多个**语言版本**。**封面和论文原图是跨语言共用的**（都来自同一份 PDF，
换个语言不会换图），但**脚本、解读、音频、视频、信息图各自独立** ——
信息图要单独出一版是因为图上写着字，英文版配一张中文标注的图会很割裂。

```jsonc
{
  // 顶层字段保持**向后兼容**：镜像主语言（primary）那一版的值。
  // 老前端不改也能正常显示主语言。
  "language": "zh",              // 主语言
  "languages": ["zh", "en"],     // 本集已有的语言版本，按顺序
  "versions": {                  // 按语言索引；没有的版本不出现
    "zh": {
      "language": "zh",
      "script": { "segments": [...], "word_count": 1146, "est_duration_sec": 190 },
      "analysis": { /* 同 §1 的 Analysis，该语言版本 */ },
      "paper_meta": { /* 同 §1；标题作者本身多为英文，两版通常一致 */ },
      "illustration": { /* 同 §1；png_url / svg_url 都带该语言的 ?lang= */ },
      "audio_url": "/api/episodes/xxx/audio?v=1712345678-2304806",
      "audio_duration_sec": 191.69,
      "audio_bytes": 2304806,
      "video": { "url": "/api/episodes/xxx/video?v=...", "duration_sec": 191.69,
                 "scene_count": 21, "bytes": 4700000, "stale": false }
    },
    "en": {
      "language": "en",
      "...": "结构同上，但 audio_url / video.url / illustration 的 URL 带 ?lang=en"
    }
  }
}
```

`languages` 的顺序 = 生成顺序（主语言一定排第一）。

**主语言的资源 URL 不带 `?lang=`**，只有其他语言才带 —— 这样已经发布出去的
主语言链接保持稳定，缓存也不会因为加参数而整体失效。

**资源接口按 `?lang=` 取对应语言**，省略时取主语言：

| 接口 | 说明 |
|---|---|
| `GET /api/episodes/{id}/audio?lang=en` | 该语言的音频 |
| `GET /api/episodes/{id}/video?lang=en` | 该语言的视频 |
| `POST /api/episodes/{id}/video/rebuild?lang=en` | 重新合成该语言的视频 |
| `GET /api/episodes/{id}/script.txt?lang=en` | 该语言的脚本下载（含语言后缀的文件名） |
| `GET /api/episodes/{id}/analysis.md?lang=en` | 该语言的解读下载 |
| `GET /api/episodes/{id}/illustration.png?lang=en` | 该语言的信息图（`illustration.svg` 同理） |

`lang` 传了这一集没有的语言 → `404`；`lang` 取值只接受 `zh` / `en`。

**老数据的兼容**：双语之前生成的集在库里没有 `versions`，
接口会**把顶层字段当成主语言那一版**回填成 `versions["zh"]`。
所以前端只需要处理一种形状，不必为历史数据写特例。

**新建时用 `options.languages` 指定要哪些版本**（见下）。

列表接口返回的 Episode **省略** `analysis`、`script`、`figures`、`illustration`、`video`
（分别置为 `null` / `[]`），且 `versions` 里也只保留语言与音频信息。
详情接口才返回完整内容。但 **`cover_url` 在列表里保留**，
因为列表卡片要显示封面缩略图。

### 账号、归属与可见性（V2）

V2 起每集属于一个用户，并且可以对外分享。三个字段：

```jsonc
{
  "user_id": "u_3f2a1b",        // 归属用户；null = 无主（V1 时代留下的数据）
  "visibility": "private",        // "private" | "public"
  "share_token": null             // visibility=public 时才有值，公开分享用的随机串
}
```

- **归属**：`GET /api/episodes` 只返回**自己的**（`user_id` 等于当前用户）单集。
  别人的单集一律 404 —— 不用 403，避免把「存在但不是你的」这个信息漏出去。
- **可见性**：`private` 只有作者能看；`public` 任何人拿到 `share_token` 都能看（免登录）。
- **分享链接**：`/api/share/{token}`，`token` 可以重置（换链接等于失效旧链接）。

### 开放模式（bootstrap）

**库里一个用户都没有时，应用处于开放模式**：不需要登录，所有单集对所有访问者可见，
行为与 V1 完全一致。这是为了「刚部署完、还没建账号」时能直接用。

**第一个注册成功的用户会认领所有无主单集**（`user_id IS NULL` → 归他）。
认领之后应用立刻进入需要登录的状态，其他人访问会拿到 401。

这样 V1 时代攒下的数据不会因为上 V2 而变成谁也看不到的孤儿。

### 状态机

| status | progress | stage_label（示例） | 含义 |
|---|---|---|---|
| `queued` | 0 | 排队中 | 已入库，等待 worker |
| `parsing` | 10 | 正在解析论文 | PDF/链接/文本 → 干净文本、封面、配图 |
| `analyzing` | 15~99 | 正在深度解读（英文） | 调 LLM 出结构化解读 |
| `scripting` | 15~99 | 正在生成播客脚本（英文） | 调 LLM 出双人对谈脚本 |
| `synthesizing` | 15~99 | 正在合成播客音频（英文） | 调豆包播客 TTS |
| `completed` | 100 | 已完成 | 全部产物就绪 |
| `failed` | 保持失败时进度 | 失败 | `error` 字段有值 |

单语言时进度大致还是 35 / 55 / 75；**双语时两个阶段各自占一段区间**
（15→99 平均切开），这样进度条不会「中文跑完 75、英文又回 35」地倒退。
`stage_label` 会标出当前是哪一版。

---

## 2. 接口清单

### `GET /api/health`

```json
{ "status": "ok", "version": "0.2.0",
  "modes": { "llm": "deepseek", "tts": "doubao" },
  "mode": "auth" }
```

`modes.llm` 取值 `"deepseek"` | `"doubao"` | `"mock"`。
`modes.tts` 取值 `"doubao"` | `"mock"`。
前端在顶栏显示对应角标（Mock 时提示「Mock 模式」）。

`mode`（V2）取值 `"open"` | `"auth"`：库里还没有任何用户时是 `"open"`（见「开放模式」），
此时前端不显示登录入口、也不带 cookie 直接访问。**这个接口本身免登录**，
否则前端没法知道该不该跳登录页。

### `GET /api/options`

返回可选项，供设置页渲染。

```json
{
  "durations": [ { "value": 3, "label": "3 分钟" },
                 { "value": 5, "label": "5 分钟" },
                 { "value": 10, "label": "10 分钟" } ],
  "levels": [ { "value": "intro", "label": "入门" },
              { "value": "advanced", "label": "进阶" },
              { "value": "expert", "label": "专业" } ],
  "voices": [ { "id": "zh_male_dayixiansheng_v2_saturn_bigtts",
                "label": "大义先生（男声·学术沉稳）",
                "gender": "male", "pair": "mizai-dayi", "language": "zh" } ],
  "languages": [ { "value": "zh", "label": "中文" },
                 { "value": "en", "label": "English" } ]
}
```

`languages` 是**服务端支持**的语言（用于新建时选择要产出哪些版本）；
`voices[].language` 决定该音色属于哪一种版本，前端按当前语言过滤音色列表。

### `POST /api/episodes`

新建生成任务，**立即返回**（不阻塞）。

两种 Content-Type：

**A. `multipart/form-data`**（PDF 上传）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | File | 是 | 仅接受 `.pdf` |
| `duration_min` | int | 否 | 默认 5 |
| `level` | str | 否 | 默认 `intro` |
| `voice_a` | str | 否 | 默认见 §5 |
| `voice_b` | str | 否 | 默认见 §5 |
| `language` | str | 否 | 主语言，`zh` 或 `en`。默认取服务端 `DEFAULT_LANGUAGE` |
| `languages` | str | 否 | 逗号分隔，如 `zh,en`。默认取服务端 `LANGUAGES`（默认只有 `zh`） |

**B. `application/json`**（链接 / 纯文本）

```jsonc
{
  "source_type": "url",           // "url" | "text"
  "url": "https://arxiv.org/abs/1706.03762",   // source_type=url 时必填
  "text": "……",                    // source_type=text 时必填
  "options": {
    "duration_min": 5, "level": "intro",
    "language": "zh",                  // 可选，主语言
    "languages": ["zh", "en"]          // 可选，要产出哪些版本
  }
}
```

响应 `201`，body 为完整 Episode（`status="queued"`）。

错误：`400` `{"detail": "..."}`（文件类型错误、缺字段、链接不可抓取）。

### `GET /api/episodes`

查询参数：`limit`（默认 20，最大 100）、`offset`（默认 0）、`status`（可选）、`q`（可选，按标题模糊搜索）、
`sort`（可选，默认 `created_desc`）。

```json
{ "items": [ /* Episode，省略 analysis/script */ ], "total": 42 }
```

`status` 除 7 个真实状态外还接受一个**伪状态 `running`**：所有非终态
（`queued` / `parsing` / `analyzing` / `scripting` / `synthesizing`）。
用户脑子里的分类是「在跑的 / 完成的 / 失败的」，不该让他点五个状态各看一遍。

`sort` 的取值是**封闭白名单**，传别的值一律 `400`（不静默退回默认排序）：

| 取值 | 含义 |
|---|---|
| `created_desc` | 新建时间倒序（默认，新的在前） |
| `created_asc` | 新建时间正序（老的在前） |
| `updated_desc` | 最近更新在前（任务在跑时，跑得最勤的那集浮到最上面） |
| `title_asc` | 标题字母序（大小写不敏感，`apple` 排在 `Banana` 前） |
| `duration_desc` | 音频时长从长到短；**还没有音频的条目（进行中 / 失败）排在最后**，不会被 `NULL` 顶到最前 |

每一档都带一个稳定的次级排序键（`rowid`）。这不是洁癖：批量导入时几集常常落在
同一秒，没有次级键的排序在翻页时会出现「同一条出现两次、另一条再也不出现」。

### `GET /api/episodes/{id}`

返回完整 Episode（含 `analysis`、`script`）。`404` 表示不存在。

前端任务进度页轮询此接口，间隔 1500ms。

### `DELETE /api/episodes/{id}`

`204` 无 body。同时删除音频文件。

### `POST /api/episodes/{id}/retry`

仅 `status="failed"` 时允许；重置为 `queued` 并重新入队。返回完整 Episode。
`409` 表示当前状态不可重试。

### `GET /api/episodes/{id}/cover`

封面图，`image/png`。内容 = **论文 PDF 第一页的整页渲染**（首页有标题、作者、
摘要和 teaser 图，辨识度最高）。text 来源没有 PDF，则退回用生成的信息图当封面。
无封面时 `404`。

### `GET /api/episodes/{id}/figures/{figure_id}`

论文原图，`image/png`。按 `Figure N:` 图注定位后从 PDF 渲染出的区域。
`figure_id` 形如 `f1`（图）/ `t1`（表）。不存在时 `404`。

### `POST /api/episodes/{id}/figures/{figure_id}/rotate`

人工校正配图方向。

```json
{ "direction": "cw" }     // "cw" 顺时针 90° / "ccw" 逆时针 90°
```

返回更新后的完整 Episode。PNG 旋转 90° 无损，转多了转回来即可。

**为什么需要它**：论文配图千奇百怪，自动判定「图正不正」不可能总对 ——
最终还是要靠人眼看一眼。看的人觉得歪了，转一下就好。

另一个必要原因是**缓存**：配图 URL 是固定的，如果重生成过内容而 URL 没变，
浏览器会一直用旧图（实测踩过这个坑）。所以所有静态资源 URL 上都带
`?v=<mtime>-<size>` 版本号，内容一变 URL 就变，缓存自动失效。

### `DELETE /api/episodes/{id}/figures/{figure_id}`

删掉一张不需要的配图（例如提取到的装饰性图表）。只从这一集移除，
不删源 PDF。返回更新后的完整 Episode。

### `PATCH /api/episodes/{id}/cover`

改**封面上的标题**（视频第一帧上那两行字）。

```json
{ "headline": "4 比特超 INT8，显存降七成",
  "paper_title": "STEPQuant: When and Where Errors Matter" }
```

| 字段 | 作用范围 | 说明 |
|---|---|---|
| `headline` | **逐语言一份** | 封面上的大字（爆款标题）。空串 = 清掉自定义、封面退回显示论文原题 |
| `paper_title` | 整集（跨语言共用） | 封面下方那行小字的论文原题，也就是这一集的名字。省略或空串 = 不动 |

`?lang=` 指定改哪一版（省略 = 主语言）；未知语言返回 404。

写进去的值会过一遍和「模型写的那条」**同一套清理**：剥掉引号、`封面标题：` 前缀、
`反差型：` 这种风格名，超长在分句处收短（中文 16 字 / 英文 9 个词）——
所以返回的 `video.hook` 可能和你传进去的不完全一样，**界面要显示返回值**。

**只写数据、不重新合成**：封面是烘焙进 MP4 的画面，改完标题要再调
`POST /video/rebuild` 才看得到。分成两步是因为重新合成要一分钟上下，
用户通常想先把标题改满意、再合成一次，而不是每敲一个字都重跑一遍编码。

### `POST /api/episodes/{id}/video/rebuild`

用现有素材**重新合成视频**（人工校正配图后用）。

音频、脚本、解读都不动，只重新渲染画面并编码。**复用上次的画面分配**，
不再调用模型 —— 否则「我只转了一张图，怎么画面全变了」，而且现场生成的主题图
会被重新生成一遍（4 次模型调用 + 几十秒）。

实测约 1 分钟（40~95 秒：每帧一块 936×1210 的画面要渲染 + H.264 编码；片长越长越久）。
返回更新后的完整 Episode。没有视频时 `409`。

**为什么需要它**：视频是把配图**烘焙进 MP4** 的，你校正了配图，已生成的视频里
还是旧画面。`video.scenes` / `video.assets` / `video.asset_versions` 这三个字段
就是为此存下来的（前两个用于复用分配，第三个用于判断过时）。

`video.scenes[i]` 是**内部数据**（不在响应里）：它记着每段用哪张图，以及 `point`
（本段要点文案）和 `focus`（图内聚光灯的框，相对整图 0~1）。重新合成时这两个都**复用**，
一个字都不该变。`focus` 的来源是「模型从图注里挑出这一段讲的是哪个子图」+
「我们按像素把那个子图切出来」（几何细节见 AGENTS.md 的「图内聚光灯」一节），
所以同一个框在重合成时是稳定的 —— `REBUILD` 不会再问模型。
**子图标注位置（`panel_marks`）同样只在库里**，和 scenes 一样不对外暴露；
封面上的大字标题也存在 `video.hook` 里（同样是内部数据，重合成时复用，不换一句）。

### `video.stale` 字段

`true` 表示**视频里的画面已经跟不上当前配图**了（你旋转/删除过配图，
或重新提取过）。前端应显示提示并给出「重新合成视频」入口。

判定方式是比较存下来的素材版本号与当前文件（`mtime-size`）。
另外：**没有版本记录的旧视频会被保守判为过时** —— 无法证明它跟当前配图一致，
与其假装没问题，不如提示重做一次。

### `GET /api/episodes/{id}/illustration.png` / `.svg`

模型生成的论文信息图。
- `.png` 用于列表缩略图等静态场景
- `.svg` **保留 SMIL 动画**，详情页请用 `<object type="image/svg+xml">` 引用，
  这样动画会播放；用 `<img>` 引用时部分浏览器不会跑动画。
  不要内联进页面 HTML（内容是模型生成的，虽然已在入库前白名单清洗）。

### 视频画幅（横版 / 竖版）

视频有**两种画幅**，可以同时存在：

| orientation | 画幅 | 用途 |
|---|---|---|
| `portrait`（默认） | 936×1210（论文首页比例） | 手机全屏 |
| `landscape` | 1920×1080（16:9） | 投屏、B 站、X |

- **竖版是默认形态**：`video` / `video_path` / `<id>.mp4` 全部保持原样，
  老链接、老缓存、老客户端一个字都不用改。
- **横版是额外产出的一份**：文件名 `<id>.landscape.mp4`（双语非主语言是
  `<id>.<lang>.landscape.mp4`），字段是 `video_landscape`（Episode 顶层镜像主语言那一版，
  `versions[lang].video_landscape` 是各语言自己的）。
- 生成：`POST /api/episodes/{id}/video/rebuild?orientation=landscape`
  （不带参数 = 竖版）。**复用已有的画面分配与强调行，不调用模型**。
  未知取值 `400`。
- 取用：`GET /api/episodes/{id}/video?orientation=landscape`
  （公开页同理：`GET /api/share/{token}/video?orientation=landscape`）。
  没有这一份时 `404`，`detail` 里明说是「还没有横版视频」。
- `VideoInfo.orientation` 标出这一份是哪种画幅；老后端不返回，缺失按 `portrait` 处理。

### `GET /api/episodes/{id}/video`

视频解读播客，`video/mp4`（H.264 + AAC）。

**画幅 936×1210**，即论文 PDF 首页的尺寸（原始首图是 935×1210，宽度取 936 是因为
H.264 的 yuv420p 要求宽高都能被 2 整除，935 会被编码器直接拒绝）。

画面构成：每条脚本对应一个画面，**用服务端返回的轮次时间戳精确对齐音频**——
片头音乐期间显示论文首页，正文按内容相关性切换论文原图，片尾显示生成的信息图。
字幕条按主播分色（主播A 冷蓝 / 主播B 暖橙）。

同样支持 Range 请求（视频拖动进度比音频更依赖它，播放器还会先发一个小的
range 请求探测 moov box）。无视频时 `404`。

### `GET /api/episodes/{id}/audio`

返回音频文件流，`Content-Type` 依格式（`audio/mpeg`）。
**必须支持 HTTP Range 请求**（播放器拖动进度条依赖），不支持的响应头会导致 Safari 无法播放。
无音频时 `404`。

### `GET /api/episodes/{id}/script.txt`

`text/plain; charset=utf-8`，`Content-Disposition: attachment`，内容为格式化脚本：

```
【主播A】欢迎收听……
【主播B】……
```

### `GET /api/episodes/{id}/analysis.md`

`text/markdown`，结构化解读的 Markdown 版，用于下载归档。

---

---

## 2.5 账号与会话（V2）

### `POST /api/auth/register`

```jsonc
{ "username": "guo", "password": "至少 8 位", "display_name": "Guo", "signup_code": "可选" }
```

- `201` → **裸 `User` 对象**（不是 `{"user": …}` 包一层），
  响应同时 `Set-Cookie: pp_session=...`（等于注册即登录）。
- `409` 用户名已被占用；`400` 参数不合法；`403` 邀请码不对。
- 服务端设了 `SIGNUP_CODE` 时 `signup_code` 必填且必须一致；没设则开放注册。
- **第一个用户会认领所有无主单集**（见「开放模式」）。

### `POST /api/auth/login`

`{ "username": "...", "password": "..." }` → `200` + **裸 `User` 对象** + `Set-Cookie`。
用户名或口令错都是 `401 {"detail": "用户名或口令不正确"}`（不区分，别提示是哪一个错）。

**登录限流**：按「用户名 + 来源 IP」记失败次数，默认 8 次后锁 15 分钟，
返回 `429` + `Retry-After`（秒）。锁住期间**正确口令也进不来**，
否则限流形同虚设。登录成功会清零计数，不会把之后的正常登录也锁住。
阈值见 `.env` 的 `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_MINUTES`。

> 实现是**进程内内存**（`app.auth.LoginThrottle`），单进程部署够用。
> 多进程 / 多实例部署必须换成 Redis 或数据库表 —— 否则每个进程各记各的，
> 实际阈值会被放大好几倍。

### `POST /api/auth/logout`

`204`，清除 cookie 并删除会话。未登录时也返回 `204`（幂等）。

### `POST /api/auth/password`

`{ "current_password": "...", "new_password": "..." }` → `204`。

必须带旧口令（否则会话 cookie 被偷就等于永久接管账号）。
改完**删除该用户的其他所有会话，只保留当前这一个** ——
口令换了但别处还挂着旧会话，是最常见的「明明改了密码还是被盗」。

### `GET /api/auth/me`

`200` + **裸 `User` 对象**；未登录 `401`。前端启动时用它判断登录态。

> 上面三个接口的响应体都是**裸 User**，没有 `{"user": …}` 外壳。
> 前端两种都兼容，但契约以本文为准 —— 要加外层结构得同时改两边。

```jsonc
// User
{
  "id": "u_3f2a1b",
  "username": "guo",
  "display_name": "Guo",
  "created_at": "2025-01-01T12:00:00+00:00"
}
```

### 鉴权规则

- 会话走 **httpOnly cookie** `pp_session`（`SameSite=Lax`，`Path=/`，有效期 30 天）。
  前端拿不到 token，也不该拿 —— XSS 偷不走。
- **需要登录的接口**：除了下面这几个，其余全部。包括音频/视频/配图这些资源接口 ——
  否则别人猜个 id 就能下载全部内容。
- **免登录接口**：`GET /api/health`、`GET /api/options`、`GET /api/auth/me`、
  `POST /api/auth/login`、`POST /api/auth/register`、`POST /api/auth/logout`、
  `GET /api/share/*`、`GET /api/showcase`。
- 未登录访问受保护接口 → `401 {"detail": "需要登录"}`；开放模式下不返回 401。
- `GET /api/health` **始终**带 `"mode": "open" | "auth"`（登录与否都带）。
  前端启动顺序依赖它，所以它必须免登录、也不能只在登录后才出现。
  缺失时前端按 "unknown" 处理 —— 不拦人。

### 用户信息

| 字段 | 说明 |
|---|---|
| `id` | `u_` + 6 位十六进制 |
| `username` | 登录名，3-32 位，只允许字母数字下划线连字符，唯一 |
| `display_name` | 展示名，可为空（前端回退到 `username`） |

口令**永不返回**。服务端用标准库 `hashlib.scrypt` 加盐哈希（每个用户独立 salt），
不引入 bcrypt/argon2 依赖。

---

## 2.6 个人专辑（V2）

专辑是「把几期播客归到一起」的简单分组，用于做主题合集。

```jsonc
// Album
{
  "id": "al_9c1d2e",
  "title": "Transformer 系列",
  "description": "从 Attention 到后续跟进工作",   // 可空
  "episode_count": 3,
  "cover_url": "/api/episodes/xxx/cover?v=...",  // 取专辑里最新一集的封面，没单集时为 null
  "created_at": "...",
  "updated_at": "..."
}
```

| 接口 | 说明 |
|---|---|
| `GET /api/albums` | 自己的专辑列表，按 `updated_at` 倒序 |
| `POST /api/albums` | `{ "title": "...", "description": null }` → `201 Album` |
| `GET /api/albums/{id}` | `200 Album`（**附带 `episodes: Episode[]`**，只含 `include_large=false` 的摘要） |
| `PATCH /api/albums/{id}` | `{ "title"?, "description"? }` → `200 Album` |
| `DELETE /api/albums/{id}` | `204`。**只删专辑，不删里面的单集**（单集的 `album_id` 置空） |
| `POST /api/albums/{id}/episodes` | `{ "episode_ids": ["a", "b"] }` → `200 Album`（批量加入，幂等） |
| `DELETE /api/albums/{id}/episodes/{episode_id}` | `200 Album`（移出） |

别的用户的专辑一律 404。往专辑里加别人的单集 → `404`。

---

## 2.7 一键分享（V2）

作者把自己的某一集设为公开，拿到一条免登录链接。

### `POST /api/episodes/{id}/share`

`200 Episode`。副作用：`visibility` 置 `public`；
`share_token` 为空时生成一个（已有则**保持不变**，重复调用不会让已发出去的链接失效）。

### `DELETE /api/episodes/{id}/share`

`200 Episode`。`visibility` 置 `private`，`share_token` 置 `null`（链接立即失效）。

### `POST /api/episodes/{id}/share/reset`

`200 Episode`。换一个新 token（旧链接立即失效，用于「链接被传出去了想收回」）。

### `GET /api/share/{token}` （免登录）

`200`，返回一个**精简**的公开视图（不是完整 Episode）：

```jsonc
{
  "token": "…",
  "title": "Attention Is All You Need",
  "paper_meta": { … },
  "language": "zh",
  "languages": ["zh", "en"],
  "cover_url": "/api/share/{token}/cover?v=…",
  "video": { "url": "/api/share/{token}/video?lang=zh", "duration_sec": 191.7, … },
  "audio_url": "/api/share/{token}/audio?lang=zh",
  "audio_duration_sec": 191.7,
  "script": { "segments": [...], "word_count": 1146, "est_duration_sec": 190 },
  "analysis": { … },
  "figures": [ { "id": "f1", "label": "Figure 1", "url": "/api/share/{token}/figures/f1?v=…" } ],
  "versions": { "zh": { … }, "en": { … } },
  "author": { "display_name": "Guo" },     // 展示用，不含 username/id
  "created_at": "…"
}
```

公开视图里**不含**：`options`（音色/参数）、`source_ref`（可能带内部路径）、`raw_text`、
`error`、`id`（用 token 代替）、作者 id。

### 公开资源（免登录）

`GET /api/share/{token}/cover`、`/video`、`/audio`、`/figures/{fid}`、
`/illustration.png`、`/illustration.svg`、`/script.txt`、`/analysis.md`

都支持 `?lang=`，语义与受保护版本完全一致（含 Range 请求）。token 失效或那一集不再公开 → `404`。

> **公开通道的 URL 一律带 `?lang=`，连主语言也带**（`/video?lang=zh&v=…`）。
> 「主语言不带 `?lang=`」那条规则只针对受保护通道 `/api/episodes/{id}/…` ——
> 那里的初衷是让**已经发布出去的主语言链接保持稳定**；
> 公开通道的 URL 里本来就带 share token、只在链接有效期内存在，没有这个顾虑。

---

## 2.8 批量生成（V2）

一次提交多篇论文，串行入队（worker 本来就是串行消费，批量只是省掉重复操作）。

### `POST /api/episodes/batch`

两种 Content-Type，与 `POST /api/episodes` 一致：

**A. `multipart/form-data`**：`files` 字段**可以出现多次**（多选 PDF），
外加 `duration_min` / `level` / `language` / `languages` 等同 `POST /api/episodes`。

**B. `application/json`**：

```jsonc
{
  "source_type": "url",           // "url" | "text"
  "urls": ["https://arxiv.org/pdf/1706.03762", "…"],
  "texts": null,                  // source_type=text 时用，每个元素是一篇
  "options": { "duration_min": 5, "level": "intro", "languages": ["zh"] }
}
```

响应 `201`：

```jsonc
{
  "created": [ /* Episode（include_large=false 的摘要），status="queued" */ ],
  "failed": [ { "ref": "https://…", "reason": "链接不可抓取" } ],
  "total": 3
}
```

- **单项失败不影响其他项**：抓不到的链接进 `failed`，好的照常入队。
- 上限 **20 篇/次**，超了返回 `400`。
- 单篇参数沿用 `options`；每篇的标题各自推断。

---

## 2.9 生成配额（V2）

一次生成要调大模型 + 语音合成 + 视频编码，是真金白银。有了多账号之后，
不给上限就等于把账单交给任何注册进来的人。

### `GET /api/usage`

```json
{ "used": 3, "limit": 30, "remaining": 27, "resets_at": "2025-01-02T12:00:00+00:00" }
```

**需要登录**。`limit = 0` 表示不限量，此时 `remaining` / `resets_at` 为 `null`。

### 配额规则

- 窗口是**最近 24 小时的滑动窗口**，不是自然日 ——「今天」取决于服务器时区，
  而用户在哪都能用；午夜齐刷刷重置也更容易被集中薅。
- `POST /api/episodes` 扣 1；`POST /api/episodes/batch` **按篇数扣**
  （不按提交次数，否则批量就是绕过配额的后门）。
- 超限返回 `429` + 可读的中文说明 + `X-Quota-Reset`（重置时刻）。
- **开放模式（库里没有账号）不设限** —— 那时本来就只有部署者自己用。
- 阈值见 `.env` 的 `DAILY_GENERATION_LIMIT`。

> 前端应当在生成页显示「今天还能生成 N 期」（`GET /api/usage`）。
> 有配额却不告诉用户还剩多少，等于让人撞 429 才知道，那是很差的体验。

---

## 2.10 关键词高亮（V2）

**没有接口变更**。前端用 `paper_meta.keywords` 在脚本与解读正文里做高亮，
详情页给一个开关（默认开），关掉后是纯文本。

关键词是模型从论文里抽的，可能是英文而正文是中文（或反过来），
所以匹配规则是**大小写不敏感的原文包含**，不做翻译映射 —— 对不上就不高亮，
不要为了实现高亮去编造对应词。

## 3. 前端页面与接口映射

| 页面 | 路由 | 用到的接口 |
|---|---|---|
| 首页（产品介绍） | `/` | `GET /api/episodes?status=completed`、`GET /api/episodes/{id}`（拿一期真实产物做展示）、`GET /api/health` |
| 生成新播客 | `/create` | `POST /api/episodes`、`POST /api/episodes/batch`、`GET /api/options` |
| 任务进度 | `/task/:id` | `GET /api/episodes/{id}`（轮询）、`POST .../retry` |
| 播客库 | `/library` | `GET /api/episodes`、`DELETE /api/episodes/{id}`、`PATCH`（改可见性/专辑） |
| 专辑列表 | `/albums` | `GET /api/albums`、`POST /api/albums` |
| 专辑详情 | `/albums/:id` | `GET /api/albums/{id}`、`PATCH`、`DELETE`、`POST .../episodes` |
| 详情播放 | `/episode/:id` | `GET /api/episodes/{id}`、`.../video`、`.../audio`、`.../cover`、`.../figures/{fid}`、`.../illustration.svg`、`.../script.txt`、`.../analysis.md`、`POST .../share` |
| **公开分享** | `/share/:token` | `GET /api/share/{token}`、`/api/share/{token}/…`（**免登录**） |
| 登录 / 注册 | `/login` | `POST /api/auth/login`、`POST /api/auth/register`、`GET /api/auth/me` |
| 设置 | `/settings` | `GET /api/options`、`GET /api/health`、`GET /api/usage`、`POST /api/auth/password` |

**登录态与路由**：`GET /api/auth/me` 返回 401 时，把用户挡在 `/login`；
`/share/:token` 例外（免登录）。**开放模式**（`mode="open"`）下不挡、不跳转。

**前端启动顺序**：先 `GET /api/health` 拿 `mode`，再 `GET /api/auth/me`。

**开放模式下的入口（重要）**：`mode="open"` 时**不强制登录、不自动跳登录页**，
但顶栏**仍要保留「创建账号」入口**。

理由：开放模式只是「还没有账号也能用」，不是「没有账号也能用全部功能」——
**专辑和分享都需要账号**（它们要一个归属者）。如果连注册入口都藏起来，
用户点「新建专辑」会拿到一个 401，却没有任何地方能去登录，是条死路。

所以：
- `mode="open"`：顶栏显示「创建账号」（不显示登出）；点专辑/分享这类需要账号的操作时，
  提示「这个功能需要账号」并给一个去 `/login` 的入口，**不是**弹一个英文的 401。
- `mode="auth"` 且未登录：跳 `/login`。
- 已登录：显示用户区（展示名 + 登出）。

**首页是产品介绍页，不是表单**：导入表单在 `/create`。
首页的产品展示区读库里最近一期**已完成**的播客，把真实的视频/音频/脚本/解读/配图摆出来；
取不到（空库 / 后端不可用）时整区隐藏，页面退化成纯介绍，不会出现空播放器或破图。

展示区带**中英切换按钮**（只在那一集真的产出了多语言版本时出现）：
切换的是 `versions[lang]` 里的视频 / 音频 / 脚本 / 解读 / 信息图；
**封面与论文原图不切**（跨语言共用）。切换不重新请求接口 —— 详情响应里已经带了全部版本。

首屏视频上还有一个**独立**的中英按钮，只切首屏那一段视频，两个切换器互不影响。

---

## 4. 前端 Mock 模式（重要）

前端必须支持**纯浏览器离线运行**（用于 GitHub Pages 演示，无后端、无密钥）：

- 一个 `src/api/index.ts` 适配器，导出与真实后端同名的函数。
- 构建时通过 Vite 环境变量 `VITE_USE_MOCK=1` 切换到 `src/api/mock.ts`。
- Mock 实现必须复刻上述全部接口语义：`POST /api/episodes` 后在内存里推进状态机
  （queued → parsing → analyzing → scripting → synthesizing → completed，每阶段约 700ms），
  并用 **Web Audio API 现场合成一段正弦波音频** 当作 `audio_url`（用 Blob URL 代替），
  使播放器可用。论文标题/解读/脚本用内置示例数据生成。
- Mock 数据里至少准备 3 篇真实论文的示例（如 Attention Is All You Need、ResNet、LoRA），
  让列表页和详情页看起来是真实产品。

---

## 5. 默认值

| 项 | 默认值 |
|---|---|
| `duration_min` | 5 |
| `level` | `intro` |
| `voice_a`（主讲，主播A） | `zh_male_dayixiansheng_v2_saturn_bigtts` |
| `voice_b`（提问，主播B） | `zh_female_mizaitongxue_v2_saturn_bigtts` |
| 音频格式 | MP3 / 24000Hz |
