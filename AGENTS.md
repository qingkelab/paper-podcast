# AGENTS.md — 论文解读 AI 播客

## 这是什么

把论文变成双人对谈播客的全栈应用。前端 Vue3 + 后端 FastAPI + DeepSeek（文本解读/脚本）+ 豆包语音播客（音频合成）。
豆包方舟（Ark）作为大模型备选，用 `LLM_PROVIDER` 切换。
**无密钥也能跑**：缺哪个密钥，哪一步就自动降级为 Mock。

## 常用命令

```bash
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
```

## 关键约定

- **`docs/API.md` 是前后端的唯一契约**。改接口先改它，字段名不许各自发明。
  列表接口要省略 `analysis`/`script` 大字段（否则列表响应会到 MB 级）。
- **密钥只在后端**（`.env`，已被 gitignore）。前端任何地方不得出现
  `DEEPSEEK_API_KEY` / `ARK_API_KEY` / `DOUBAO_*`。
- **Prompt 改动要跑测试**。`backend/app/services/prompts.py` 是本产品的核心资产，
  `tests/test_services.py::TestPromptConstraints` 锁定了「反 AI 腔负面清单」和字数预算，
  别把它当装饰删掉。
- **音频接口必须支持 Range 请求**。播放器拖动进度条依赖 `206 + Content-Range`，
  改成普通 `FileResponse` 会让 Safari 完全不能播。

## 踩过的坑（别重犯）

- **播客 TTS 不是 HTTP 提交+轮询**。是 WebSocket 长连（`wss://…/api/v3/sami/podcasttts`），
  二进制帧 + 事件流，音频按「轮次」流式返回后自行拼接。细节见 `podcast_tts.py` 与
  `tests/test_protocol.py`。别照着网上那些 `/api/v3/tts/submit` 的写法改。
- **`request.form()` 返回的是 Starlette 的 `UploadFile`，而 `fastapi.UploadFile` 是它的子类**。
  用 `fastapi.UploadFile` 做 `isinstance` 判断恒为假——曾经导致所有 PDF 上传被 400 拒绝。
  判据统一用 `starlette.datastructures.UploadFile`。
- **闭包内不要用 `out += [...]`**。augmented assignment 会把变量变成闭包局部变量，
  抛 `UnboundLocalError`。用 `extend()` 或改成纯函数返回列表（`_markdown_section` 就是这么改的）。
- **正文清洗判定用字符位置而不是行号**。PDF 抽出来的正文常是少数超长行，
  按行号算「是否进入后半段」会漏掉参考文献。
- **播客合成必须收到 150 后立刻发 FinishSession(102)**。服务端只有收到它才会在
  合成完毕后发 152。放进 finally（循环结束后才发）会导致永远等不到 152、
  每次都误判超时，并把已经完整收到的音频整个丢掉。实测过，这是唯一正确的时序。
- **362（轮次结束）没有 round_id**，只能从 360 跟踪；且 round_id 会出现
  -1/9999/10000（片头音乐/片尾音乐/水印），不能计入进度回调。
- **豆包返回的是 24000Hz 的 MPEG2 MP3**。做时长估算必须区分 MPEG1/2/2.5 与
  Layer I/II/III 的比特率表——按 MPEG1 表算会把 58.52 秒算成 35.11 秒。
  现在用逐帧累计采样数，对 CBR/VBR 都精确。
- **时长靠字数控制，而模型不守字数预算**。语速常量（350 字/分钟）和音乐时长
  （17 秒）是从真实结果反推的，别凭「中文播客常识」改。光靠 prompt 约束字数
  不可靠（实测欠过 35%），所以有 `_repair_length_if_needed` 做生成后兜底。
- **macOS 系统代理开着 SOCKS 时**，websockets 会自动读取系统代理，缺 python-socks
  会直接连接失败。

## 结构

```
backend/app/services/     ingest(预处理) prompts(Prompt) llm(DeepSeek/方舟) podcast_tts(语音) pipeline(编排)
backend/app/worker.py     asyncio 队列，串行消费 + 分类重试
backend/tests/            126 项，改完必须全绿
frontend/src/api/         index(适配器) real(真实) mock(浏览器端模拟)
```

## Mock 模式

`Settings.llm_mode` / `tts_mode` 会根据密钥是否齐全自动判断，也可用 `FORCE_MOCK=true` 强制。
前端另有独立的浏览器端 Mock（`VITE_USE_MOCK=1`），不依赖后端，用于静态站点演示。
