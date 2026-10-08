# 前后端接口契约（冻结版 v1）

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
    { "id": "f1", "kind": "figure", "label": "Figure 1",
      "caption": "Figure 1: The Transformer - model architecture.",
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
    "bytes": 4801376
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
{ "status": "ok", "version": "0.1.0",
  "modes": { "llm": "deepseek", "tts": "doubao" } }
```

`modes.llm` 取值 `"deepseek"` | `"doubao"` | `"mock"`。
`modes.tts` 取值 `"doubao"` | `"mock"`。
前端在顶栏显示对应角标（Mock 时提示「Mock 模式」）。

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

查询参数：`limit`（默认 20，最大 100）、`offset`（默认 0）、`status`（可选）、`q`（可选，按标题模糊搜索）。

```json
{ "items": [ /* Episode，省略 analysis/script */ ], "total": 42 }
```

按 `created_at` 倒序。

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

### `POST /api/episodes/{id}/video/rebuild`

用现有素材**重新合成视频**（人工校正配图后用）。

音频、脚本、解读都不动，只重新渲染画面并编码。**复用上次的画面分配**，
不再调用模型 —— 否则「我只转了一张图，怎么画面全变了」，而且现场生成的主题图
会被重新生成一遍（4 次模型调用 + 几十秒）。

实测约 11 秒（5 分钟片长）。返回更新后的完整 Episode。没有视频时 `409`。

**为什么需要它**：视频是把配图**烘焙进 MP4** 的，你校正了配图，已生成的视频里
还是旧画面。`video.scenes` / `video.assets` / `video.asset_versions` 这三个字段
就是为此存下来的（前两个用于复用分配，第三个用于判断过时）。

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

## 3. 前端页面与接口映射

| 页面 | 路由 | 用到的接口 |
|---|---|---|
| 首页/导入 | `/` | `POST /api/episodes`、`GET /api/options`、`GET /api/health` |
| 任务进度 | `/task/:id` | `GET /api/episodes/{id}`（轮询）、`POST .../retry` |
| 播客库 | `/library` | `GET /api/episodes`、`DELETE /api/episodes/{id}` |
| 详情播放 | `/episode/:id` | `GET /api/episodes/{id}`、`.../video`、`.../audio`、`.../cover`、`.../figures/{fid}`、`.../illustration.svg`、`.../script.txt`、`.../analysis.md` |
| 设置 | `/settings` | `GET /api/options`、`GET /api/health`（本地存储偏好） |

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
