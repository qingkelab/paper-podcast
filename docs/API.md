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
    "voice_b": "zh_female_mizaitongxue_v2_saturn_bigtts"
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
  "audio_url": "/api/episodes/3f2a9c1e/audio",  // 无音频时 null
  "audio_duration_sec": 302.5,
  "audio_bytes": 2411724,
  "created_at": "2025-01-01T12:00:00+00:00",
  "updated_at": "2025-01-01T12:03:11+00:00"
}
```

列表接口返回的 Episode **省略** `analysis` 和 `script` 两个大字段（置为 `null`），详情接口才返回。

### 状态机

| status | progress | stage_label（示例） | 含义 |
|---|---|---|---|
| `queued` | 0 | 排队中 | 已入库，等待 worker |
| `parsing` | 10 | 正在解析论文 | PDF/链接/文本 → 干净文本 |
| `analyzing` | 35 | 正在深度解读 | 调 LLM 出结构化解读 |
| `scripting` | 55 | 正在生成播客脚本 | 调 LLM 出双人对谈脚本 |
| `synthesizing` | 75 | 正在合成播客音频 | 调豆包播客 TTS |
| `completed` | 100 | 已完成 | 全部产物就绪 |
| `failed` | 保持失败时进度 | 失败 | `error` 字段有值 |

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
                "gender": "male", "pair": "mizai-dayi" } ]
}
```

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

**B. `application/json`**（链接 / 纯文本）

```jsonc
{
  "source_type": "url",           // "url" | "text"
  "url": "https://arxiv.org/abs/1706.03762",   // source_type=url 时必填
  "text": "……",                    // source_type=text 时必填
  "options": { "duration_min": 5, "level": "intro" }
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
| 详情播放 | `/episode/:id` | `GET /api/episodes/{id}`、`.../audio`、`.../script.txt`、`.../analysis.md` |
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
