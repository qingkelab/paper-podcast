# 论文解读 AI 播客

上传一篇论文，自动生成一期双人对谈的学术播客音频。

**流程**：PDF / arXiv 链接 / 粘贴文本 → 大模型深度解读 → 双人口语化脚本 → 豆包语音合成 → 可播放、可下载、可归档的播客。

**配图**：每期会自动带上三种图 —— ①封面是**论文 PDF 第一页的整页渲染**；
②正文里**按图注提取的论文原图**（最多 6 张）；③一张**模型生成的信息图**
（SVG，带 SMIL 动画，详情页里会动）。

**视频解读播客**：最后把配图和音频合成一个完整 MP4。画幅就是论文首页尺寸
（936×1210 竖版），画面按脚本逐段切换、底部带分色字幕条，**用服务端返回的
轮次时间戳精确对齐音频**（不是猜语速）。需要系统安装 ffmpeg。

画面里的图遵循「**图文相符**」：能对上论文原图的段落用原图；对不上的段落
（背景铺垫、复杂度讨论、不足与展望等）会**现场生成一张只讲这段内容的示意图**
（默认最多 4 张，连续几段讲同一件事共用一张）；最后才用中性的概括图兜底。

> 区别于普通的「论文总结」：这里产出的是**双主播对谈**形态——主播A讲解、主播B追问质疑，
> 有附和、有停顿、有语气起伏，适配通勤、自习、复盘等碎片化收听场景。

---

## 快速开始（零密钥，30 秒）

没配置任何密钥时，系统自动降级为 **Mock 模式**，整条流水线照样跑通
（解读和脚本用内置示例数据，音频是 Web Audio 合成的占位音），可以直接看界面、走流程。

```bash
# 0. 系统依赖：视频合成需要 ffmpeg
brew install ffmpeg          # macOS；Linux 用 apt install ffmpeg

# 1. 后端
cd backend
uv venv --python 3.13 ../.venv          # 或 python3 -m venv ../.venv
uv pip install --python ../.venv/bin/python -r requirements.txt
../.venv/bin/python -m uvicorn app.main:app --reload --port 8000

# 2. 前端（另开一个终端）
cd frontend
pnpm install
pnpm dev                                 # http://127.0.0.1:5173
```

打开 http://127.0.0.1:5173 ，粘贴一段论文文本就能看到完整流程。
后端 API 文档在 http://127.0.0.1:8000/docs 。

---

## 接入真实豆包 API

```bash
cp .env.example .env      # 然后填入密钥
```

需要两组密钥，可以从**任意一组**开始（未配置的那部分继续走 Mock）：

| 用途 | 变量 | 去哪拿 |
|---|---|---|
| 论文解读 + 脚本生成 | `DEEPSEEK_API_KEY` | [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) |
| 播客音频合成 | `DOUBAO_API_KEY`（新版）<br>或 `DOUBAO_APP_ID`+`DOUBAO_ACCESS_KEY`（旧版） | 火山引擎控制台 → 豆包语音 → 应用管理 |

文本解读默认走 **DeepSeek**（`deepseek-chat`）。也支持豆包方舟，两者都是
OpenAI 兼容接口，切换只改 `LLM_PROVIDER`，代码路径完全一致：

```bash
LLM_PROVIDER=deepseek   # 默认（auto 时 DeepSeek 优先）
LLM_PROVIDER=doubao     # 改用方舟，需另配 ARK_API_KEY + ARK_MODEL
LLM_PROVIDER=auto       # 谁配了密钥用谁
```

配置完成后重启后端，启动日志会打印当前模式：

```
  文本解读：DeepSeek API
  音频合成：豆包语音播客
```

`GET /api/health` 也会返回 `{"modes": {"llm": "deepseek", "tts": "doubao"}}`。

### ⚠️ 关于播客接口的重要更正

原始方案文档里写的 `/api/v3/tts/submit` + `/api/v3/tts/query`（HTTP 提交 + 轮询）**并不存在**。
豆包语音播客的真实接口是：

```
wss://openspeech.bytedance.com/api/v3/sami/podcasttts    (WebSocket 长连接)
```

差异不止是地址，架构上也不一样：

| 方案文档假设 | 官方实际协议 |
|---|---|
| HTTP 提交任务，拿 task_id | WebSocket 长连接，一次会话即一个任务 |
| 轮询 query 接口查状态 | 服务端**流式推送**事件，无需轮询 |
| 一次性返回音频 URL | 按「轮次(round)」流式返回音频字节，客户端自行拼接 |
| 自定义音色/语速等参数 | 双主播固定两个音色；`speaker_info.speakers` 必须**正好两个** |

本项目的实现（`backend/app/services/podcast_tts.py`）按官方协议完整实现了二进制帧编解码：

- 帧结构：`4 字节头 + event + session_id + connect_id + payload`
- 事件码：`1/2` 建连与断连、`50` 连接就绪、`100/102` 会话起止、`150` 会话就绪、
  `360` 轮次开始、`361` 音频数据、`362` 轮次结束、`152` 会话结束
- 鉴权：`X-Api-Key`（新版）或 `X-Api-App-Id`+`X-Api-App-Key`+`X-Api-Access-Key`（旧版），
  外加 `X-Api-Resource-Id: volc.service_type.10050`

这套帧格式无法在本地对接真实服务验证，所以 `backend/tests/test_protocol.py` 用测试把字节布局钉死了。

### 为什么用 `action=3`（from_script）

播客接口有三种模式：`0` 从原文自动总结、`3` 用调用方给的脚本、`4` 从一句话主题生成。
本项目用 `action=3`：解读和脚本由我们自己的 Prompt 生成，再送进 TTS 合成。
这样才能保证解读深度和文风可控，而不是把论文丢给 TTS 服务让它自由发挥。

---

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Vue 3 + Vite + TypeScript + Pinia |
| 后端 | Python 3.13 + FastAPI |
| 存储 | SQLite（标准库 sqlite3，WAL 模式） |
| 大模型 | DeepSeek（默认）/ 豆包方舟，均为 OpenAI 兼容 Chat Completions |
| 语音合成 | 豆包语音播客 PodcastTTS（WebSocket） |

**为什么用标准库 sqlite3 而不是 ORM**：整个应用只有一张表，ORM 带来的迁移和版本
负担大于收益。

---

## 项目结构

```
paper-podcast/
├── docs/API.md                    前后端接口契约（唯一权威）
├── backend/
│   ├── app/
│   │   ├── main.py                FastAPI 入口，托管前端静态文件
│   │   ├── config.py              配置与 Mock 判定
│   │   ├── db.py                  SQLite 存储层
│   │   ├── models.py              请求/响应模型
│   │   ├── voices.py              音色目录
│   │   ├── worker.py              异步任务队列（串行消费 + 自动重试）
│   │   ├── api/routes.py          HTTP 接口
│   │   └── services/
│   │       ├── ingest.py          PDF/链接/文本 → 干净正文
│   │       ├── prompts.py         Prompt 工程（本产品的核心资产）
│   │       ├── llm.py             方舟客户端 + JSON 容错 + Mock
│   │       ├── podcast_tts.py     播客 WebSocket 协议 + Mock
│   │       ├── figures.py         封面渲染 + 论文原图提取
│   │       ├── illustration.py    生成信息图（SVG）+ 清洗 + 栅格化
│   │       ├── video.py           视频合成（时间轴 / 配图分配 / 字幕 / 编码）
│   │       └── pipeline.py        流水线编排 + 产物导出
│   └── tests/                     81 项测试
└── frontend/
    └── src/                       Vue3 应用，含浏览器端 Mock 适配器
```

---

## 关键设计决策

**两段式生成**。先出结构化解读（JSON），再基于解读写脚本，而不是一次生成「解读+脚本」。
一次性输出时模型会把注意力放在文风上，解读深度明显下降；分开后每步约束更紧，
且任一步失败可以单独重试。

**反 AI 腔的负面清单**。播客脚本最容易毁在「首先…其次…最后」「综上所述」「让我们深入探讨」
这类书面语上。`prompts.py` 里显式列出禁用词，并给出正面的口语示范。

**按字数而非按感觉控时长**。中文播客语速约 250 字/分钟，Prompt 里直接把
「3/5/10 分钟」换算成字数区间写进去，并声明为硬约束。

**串行 worker**。默认并发 1，避免触发豆包 API 的并发限流。任务耗时 1-3 分钟，
接口只入库并投递 ID 立即返回，前端轮询进度。

**配图为什么是 SVG 而不是「HTML 截图」**。本项目实测过 HTML→图片路线：
Chrome headless 在本机会挂死，Playwright 装浏览器超时，且截图产物是死图。
改用 SVG 后不依赖浏览器（resvg 栅格化），而且 **SVG 原生支持 SMIL 动画**——
详情页直出 SVG 就是动态的，比截图成 PNG 强。

**失败分类重试**。「PDF 解析不了」「鉴权失败」这类错误重试没有意义，直接放弃；
网络抖动和超时才重试（指数退避，最多 2 次）。

---

## 测试

```bash
cd backend
../.venv/bin/python -m pytest          # 81 passed
```

覆盖范围：

- `test_protocol.py` — 二进制帧编解码、音色映射、session payload 结构
- `test_services.py` — 文本清洗、JSON 容错解析、Prompt 约束、时长探测
- `test_api.py` — 全流程端到端、状态机单调性、Range 请求、错误码边界、真实 PDF 上传

---

## 部署

单端口部署：先构建前端，后端启动时会自动托管 `frontend/dist`。

```bash
cd frontend && pnpm build
cd ../backend && ../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

轻量云服务器 2核4G 足够。注意：

- `.env` 和 `data/` 已在 `.gitignore` 里，**不要**提交密钥和用户论文
- 密钥只在后端使用，前端拿不到任何鉴权信息
- 上线前建议加每日调用次数限制，防止被刷量

---

## 已知限制

- **时长是估算，不是硬保证**。语速常量（350 字/分钟）和片头片尾音乐时长（约 17 秒）
  都是从真实合成结果反推校准的，模型预测时长与实测偏差在 2% 以内。但模型写脚本时
  对字数预算的遵守程度不稳定（实测 3 分钟档超出 7%、5 分钟档一度欠 35%），
  所以加了「生成后检查字数 → 不达标补一次扩写」的兜底。**当前实测落点约 ±10%**：
  选 3 分钟得到约 3.2 分钟，选 5 分钟得到约 4.5 分钟。
- **链接抓取**：知网等需要登录的站点抓不到，会返回明确提示让用户改用 PDF 上传。
  扫描版（图片型）PDF 也会明确报错，需要先做 OCR。
- **语速偏快**：这几个音色在 `speech_rate=0` 下实测约 350 字/分钟，中文人声叙述通常
  是 240~280。听感上可能觉得赶，调 `.env` 里的 `PODCAST_SPEECH_RATE` 到 -20 ~ -30
  会舒服很多（字数预算会随语速自动联动）。
- **音频不落云端**：合成后存在本地磁盘。多实例部署时需要换成对象存储。
- **无断点续传**：协议支持 `retry_info` 从上次完成的轮次恢复，数据库也记录了
  `podcast_task_id` 和 `finished_round`，但客户端还没实现恢复逻辑，目前失败是从头重来。
- **音色目录是硬编码的**：`app/voices.py` 里是播客接口的官方配对音色。
  豆包文档里可用的播客音色不止这四款，接真实账号时建议以控制台为准核对一遍。

---

## 迭代规划

| 版本 | 内容 |
|---|---|
| V1.0（当前） | 论文导入、深度解读、双人播客生成、播放下载、内容归档 |
| V2.0 | 用户登录、个人专辑、一键分享、多语言解读、批量生成、关键词高亮 |
| V3.0 | 付费会员、高清音频、长时长播客、专属音色、论文知识库、订阅推送 |
