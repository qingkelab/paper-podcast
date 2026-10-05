# AGENTS.md — 论文解读 AI 播客

## 这是什么

把论文变成双人对谈播客的全栈应用。前端 Vue3 + 后端 FastAPI + DeepSeek（文本解读/脚本）+ 豆包语音播客（音频合成）。
豆包方舟（Ark）作为大模型备选，用 `LLM_PROVIDER` 切换。
**无密钥也能跑**：缺哪个密钥，哪一步就自动降级为 Mock。

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
- **生成的信息图要走 `<object type="image/svg+xml">` 引用**，不要用 `<img>`
  （部分浏览器不会跑 `<img>` 里 SVG 的 SMIL 动画），也**不要内联进 HTML**
  （内容是模型生成的）。后端已做白名单清洗并加了 CSP 兜底。

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
- **论文里的图大多是矢量图形，不是位图**。只调 `page.get_images()` 会漏掉几乎
  所有真正的图（实测 Attention 那篇只有 3 张位图，但单页有 600~1000 个矢量绘图）。
  必须「按图注定位 + 图形包围盒渲染区域」。
- **图注在图下方，表注在表上方**。方向搞反会截到一片空白。
- **`page.get_text(clip=...)` 返回的是相交的整个文本块，不是严格裁剪的文字**。
  取图注要用 `get_text("words")` 逐词过滤，否则会把图里的坐标轴标签带进来。
- **`Figure 4` 和 `Fig. 4` 指同一张图**，正文里的交叉引用又常落在行首，
  正则分不开，必须后处理去重 + 优先全文写法。
- **清洗模型生成的 SVG 时不要手动 `set("xmlns")`**。ElementTree 在注册了默认
  命名空间后会自动输出 xmlns，手动再加会产生重复属性 → XML 非法
  （resvg 宽容能渲染，浏览器直接报错）。
- **本环境无法做 HTML→图片**：Chrome headless 会挂死，Playwright 装浏览器超时。
  需要「生成配图」时走 SVG + resvg-py（自包含，且 SVG 原生支持 SMIL 动画）。
- **视频合成必须分两步**（先出纯视频轨，再用 `-c:v copy` 封音频）。
  一条命令把 concat 幻灯片和音频一起编码**无法对齐长度**：
  `-r 30 -shortest` 会多 2.4 秒，不加 `-r` 会少 12 秒，输入端 `-r 30` 直接崩到 0.8 秒。
  另外**绝对不要用 `-t` 钳总长**——实测 `-t 4.000` 会把 4 秒的片子砍成 2.03 秒。
- **视频画幅必须是偶数**：H.264 的 yuv420p 要求宽高能被 2 整除。
  论文首页是 935×1210，取 **936**×1210，直接用 935 会被 libx264 拒绝
  （`width not divisible by 2`）。
- **配图要逐段指定，不能只给「起始段号」**。早期做法是「从第 N 段开始显示某图、
  一直用到下一张图开始」，中间几段话题变了图却没变，直接导致图文不符。
  现在由模型**为每一段**选图（`_normalize_per_segment`），
  选不出对应图时用中性的信息图，**不硬凑不相关的图**。
  另外图注是英文、脚本是中文，跨语言匹配只能靠语义，关键词匹配无效。
- **精简脚本时要检查实际缩减量**：只判「比原来短」不够，
  实测出现过 1175 → 1171 这种「改了等于没改」，白花一次调用。要求至少减 3%。

## 结构

```
backend/app/services/     ingest(预处理) prompts(Prompt) llm(DeepSeek/方舟) podcast_tts(语音)
                          figures(PDF封面+论文原图) illustration(生成信息图)
                          video(视频合成) pipeline(编排)
backend/app/worker.py     asyncio 队列，串行消费 + 分类重试
backend/tests/            203 项，改完必须全绿
frontend/src/api/         index(适配器) real(真实) mock(浏览器端模拟)
```

## Mock 模式

`Settings.llm_mode` / `tts_mode` 会根据密钥是否齐全自动判断，也可用 `FORCE_MOCK=true` 强制。
前端另有独立的浏览器端 Mock（`VITE_USE_MOCK=1`），不依赖后端，用于静态站点演示。
