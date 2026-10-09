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
- **免登录白名单只有这几个**：`/api/health`（前端要靠它判该不该跳登录页）、
  `/api/options`（音色目录，首页登录前也要能显示）、`/api/auth/{me,login,register,logout}`、
  `/api/share/*`、`/api/showcase`。往这个名单里加东西前先问一句「这真的可以公开吗」。
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
- **首屏右下角才是放角标的地方**。左下角被「音频」浮动卡整块盖住（实测重叠 3701px²，
  等于白摆一个角标），右上角被「配图」浮动卡压着。

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
- **有些论文的图里文字全是竖排的**（典型是注意力可视化的词对齐网格）。
  实测 Attention 那篇 Figure 3/4/5 是 108 行文字全部方向 (0,-1)、没有一行横排。
  代码会把这类图转正（`_text_direction` + `_rotate_pixmap`），判定很保守
  （行数够多且竖直占比 ≥90%），ResNet 那类有横排文字的图一张都不会被误转。
- **PyMuPDF 的 `Page.get_pixmap()` 没有 `rotate` 参数**（传了直接 TypeError）。
  要旋转只能把 pixmap 放回临时 PDF 页、设页面 /Rotate 再渲染。
  实测 `set_rotation(90)` 是顺时针（用不对称色块验证过方向）。
- **不要把「提取到 0 张配图」写回数据库**。我在手工重建某一集时踩过：
  抓错了 URL（abs 页面而非 PDF）→ 提取到 0 张 → 直接写回 → 那一集 5 张配图全没了。
  `pipeline.py` 里有 `if figures:` 保护，任何批量重建脚本也必须带上同样的判断。
- **静态资源的 URL 必须带版本号**。配图/封面/音频的 URL 是固定的
  （`/figures/f3` 之类），但内容会因为「重新提取」「改进算法」「人工校正」而变化。
  只按 URL 做长缓存的话，客户端会一直用旧图 —— 实测踩过：修正了配图方向并重新生成，
  浏览器里看到的还是没转过的旧图，白排查一轮。
  现在所有资源 URL 都带 `?v=<mtime>-<size>`，内容一变 URL 就变。
- **「图正不正」最终只能靠人眼**，所以有 `POST /figures/{fid}/rotate` 人工校正接口。
  自动判定（文字方向 + 墨迹分布）不可能总对，别指望把它调到 100% 准，
  要保证「判错了人能一键改回来」。PNG 转 90° 无损，转多了转回来即可。
- **别用宽泛的 `except: continue` 吞掉异常**。配图渲染那里正是因为这么写，
  把「get_pixmap 不认识 rotate 参数」这个真实错误藏了整整一轮，
  表现为「提取到的配图数量为 0」而没有任何报错。
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
  选不出对应图时填伪 id `generate`，由系统**现场生成一张专门的图**
  （`generate_topic_images`）。另外图注是英文、脚本是中文，
  跨语言匹配只能靠语义，关键词匹配无效。
- **现场生成配图必须先合并连续段落**（`group_generate_runs`）。
  一段一张的话 21 段会生成十几张，每张一次模型调用（约 10 秒 / 3000+ token），
  成本和耗时都会失控。连续几段通常本来就在讲同一件事，共用一张更贴切。
  超过上限（`MAX_TOPIC_IMAGES`）时**合并而不是丢弃** —— 丢弃会让那些段落
  退回中性图，又变回图文不符。
- **服务端的片头/片尾音乐默认关掉**（`PODCAST_HEAD_MUSIC` / `PODCAST_TAIL_MUSIC`），
  换成社区自己的品牌话术（`app/branding.py`）。实测关掉后片头静音从 7.01s 变 0，
  片尾从 12.25s 降到约 2s。**剩下的那点尾巴不是音乐**：0.84s 是 AIGC 水印
  （合规要求，别关），其余经测是说话特征（过零率 0.117，与说话区 0.076~0.102 同档）。
- **品牌话术要从时长预算里扣掉**。它和音乐一样是固定开销，不扣的话正文写满就超时。
  `prompts.compute_padding_sec()` 统一算，别在别处再手写一遍。
- **社区 logo 是浅色字标，必须垫深色底**。它来自社区自己的视频项目
  （`qingkelab/qingke-video/assets/logo.png`，已内置到 `backend/app/assets/`）。
  配色取社区视频的品牌规范：青稞绿 `#7CB342` + 麦金 `#F5C842` + 深底 `#0a120c`。
  正文页白底 + 右上角深色圆角底块垫 logo；**片尾整张做成品牌深色卡**。
  别把 logo 反相 —— 它含金色的麦穗元素，反相会变成蓝紫色。
- **品牌段要带 `brand` 标记传到视频层**。`build_script_payload` 会保留
  `brand: intro/outro`，视频据此把片尾渲染成品牌卡；丢掉这个标记片尾就退化成普通配图页。
- **关注引导画面和语音都要有**。只出字幕会漏掉纯听的场景，只说语音则画面没有落点。
- **视频画面是白底**。论文配图多数本身就是白底图表，深色画布会把它们衬得像贴图；
  白底更接近读论文的观感，也方便投屏和截图。**不要显示「主播A / 主播B」标签** ——
  谁在说话听声音就知道，画面上多一行标签既分散注意力又占掉字幕空间。
- **视频是把配图烘焙进 MP4 的**，所以人工校正配图后，已生成的视频里还是旧画面。
  为此存了 `video.scenes`（每段用哪张图）、`video.assets`（id→路径）、
  `video.asset_versions`（合成时的素材版本号）。前两个让 `POST /video/rebuild`
  能**复用画面分配**而不必再问模型；第三个用来判断 `video.stale`。
  没有版本记录的旧视频保守判为过时 —— 无法证明它跟当前配图一致。
- **重新合成视频必须复用画面分配**。再问一次模型结果会变（用户会觉得
  「我就转了个图，怎么画面全变了」），而且主题图会被重新生成一遍
  （4 次调用 + 几十秒）。实测复用后重合成只要 11 秒。
- **`compose_video` 必须用 `asyncio.to_thread` 跑**。它是同步函数，
  加上现场生成配图后会阻塞几十秒，直接 await 会让事件循环卡死，
  前端轮询拿不到响应，看起来像服务挂了。
- **精简脚本时要检查实际缩减量**：只判「比原来短」不够，
  实测出现过 1175 → 1171 这种「改了等于没改」，白花一次调用。要求至少减 3%。

### 双语（zh + en）

- **`word_count` 是「时长预算的单位数」，不是一个物理量**。中文数字符，英文数词。
  两者量纲不同（350 字/分 vs 141 词/分），必须跟 `prompts.effective_rate()` 对齐。
  曾经统一按字符数统计，英文时长被高估 4~5 倍（2081 字符当成 2081 词 → 13.9 分钟，
  实际 2.4 分钟）。
- **英文语速常量是实测反推的，不是「英文播客常识」**：真跑一期 3 分钟英文播客，
  正文 496 词 / 211.0 秒语音 = 141 词/分钟。早先按 150 估会短 6%。
- **豆包播客 TTS 对单个 round 的文本长度有硬上限（300 字符）**，越界直接拒绝：
  `40000010 nlp_texts round text length 309 is greater than max char length 300`。
  它数的是**字符**：中文一段 120 字 ≈ 120 字符撞不到，**英文一段 50 个词就有 300 字符**，
  所以这个坑是英文版上线时才炸出来的。而且炸在 TTS 阶段会触发 worker
  **整条流水线重跑**（重新解读 + 重新生成配图 + 重合成中文音频），实测白烧 10 分钟。
  除了 prompt 约束（`SCRIPT_SYSTEM_EN` 里的 HARD LIMIT），还必须有代码兜底
  `llm.split_long_segments()`。
- **英文版的 prompt 必须整段是英文**，包括那句长度预算和输出语言规则。
  中文指令混在英文输出任务里，模型会跟着中文语感走 —— 实测把「120 字」当成
  120 个词，脚本写到目标的 130%。见 `_build_script_messages_en` / `LEVEL_GUIDE_EN`。
- **解读的 system prompt 也必须整段英文**（`ANALYSIS_SYSTEM_EN`）。
  一开始只给中文 `ANALYSIS_SYSTEM` 追加一句「请用英文输出」，实测**不管用**：
  英文版的 `background` 有 83% 是中文，而且这个错误不会报错、不会失败，
  只是内容悄悄是中文 —— 直到首页把两种语言的解读并排展示才看出来。
  脚本那边因为一开始就写了完整的 `SCRIPT_SYSTEM_EN`，反而没踩到。
  `tests/test_services.py::TestAnalysisPromptLanguage` 现在盯着这件事：
  两版 prompt 的 JSON 字段必须完全一致，且英文那版一个中文字都不能有。
- **「服务端支持双语」≠「每一集都有英文版」**。判断 `?lang=` 是否合法只能看
  **这一集实际产出了什么**（`Pipeline.available_languages()`）。拿配置的语言列表去判，
  老数据的 `?lang=en` 会**静默返回中文内容**（200 + 主语言），前端以为切成功了。
- **主语言的资源 URL 不带 `?lang=`**。带上会让已经发布出去的链接和缓存全部失效。
- **配图分两类**：封面（PDF 第一页）和论文原图**跨语言共用**（同一份 PDF）；
  信息图**跟随语言**（图上写着字，英文版配中文标注的图很割裂）。
- **顶层字段镜像主语言那一版**（`paper_meta`/`analysis`/`script`/`audio_*`/`video`），
  另一语言只在 `versions[lang]` 里。老前端一行不改也能正常显示。
- **老数据没有 `versions`**，接口把顶层字段当成主语言那一版回填。
  前端因此只需要处理一种形状，不必为历史数据写特例。
- **双语时进度要按语言切区间**（15→99 平均切开）。两个阶段都从 35 开始会让进度条
  倒退（中文跑完 75、英文又回 35），看起来像卡死重来。`stage_label` 要标出当前是哪一版。
- **`_voices_for()` 按语言换音色**。中文音色念英文虽然也能出声，但口音很明显；
  用户只在前端选过一次音色（主语言那一档），英文版必须换 `DEFAULT_VOICE_A_EN/B_EN`。

### 账号与分享（V2）

- **口令用标准库 `hashlib.scrypt`**，不引 bcrypt/argon2。存 `scrypt$n$r$p$salt$hash`，
  每用户独立 salt。比对必须用 `hmac.compare_digest` —— 用 `==` 会因为提前返回
  而泄漏前缀匹配长度，这是几行代码就能堵上的洞。
- **用户名要显式判 `isascii()`**。`str.isalnum()` 对中日韩字符也返回 True，
  只写 `isalnum()` 的话「中文名」会被当成合法用户名放过去（写测试时逮到的）。
- **登录失败时，用户不存在也要跑一次哈希校验**。否则「用户名不存在」比「口令错」
  快得多，从响应时间上就能枚举出哪些用户名真实存在。
  两种失败的提示必须一模一样。
- **注册即登录**，会话只用 **httpOnly cookie**（`SameSite=Lax`，30 天）。前端拿不到 token，
  XSS 也偷不走。`COOKIE_SECURE=true` 只能在 HTTPS 下开，本地 http 开了会一直登不上。
- **开放模式**：库里一个用户都没有时不需要登录。这样刚部署完能直接用。
  **第一个注册的用户认领所有无主单集**（`claim_orphan_episodes`）——
  没有这一步，V1 时代攒的数据在升级后会变成「文件还在磁盘上、接口永远不返回」的孤儿。
- **`is_open_mode()` 每次都查一次 COUNT**，不做缓存。SQLite 上这是微秒级，
  而缓存需要一个必须手动维护的失效逻辑 —— 那才是真正容易出错的地方。
- **改口令只能删「其他」会话**（`delete_other_sessions`）。第一版写成「先全删再补回当前这条」，
  结果漏了补回那步，用户改完口令自己就被踢出去了 —— 被测试逮住。
- **分享链接是「开关 + 可重置的 token」**：`visibility` 决定看不看得到，
  `share_token` 是地址。取单集时必须**同时**匹配 `visibility='public'`，
  不能只看 token 在不在 —— 否则取消分享那一刻旧链接还是通的。
- **重复点「分享」不能换 token**。用户点两次就把刚发给别人的链接弄失效，是最恼人的那种 bug。
- **公开视图是单独构造的**，不是把 Episode 删几个字段：不含 `id`/`options`/`source_ref`/
  `raw_text`/`error`/`user_id`，作者也只给展示名。白名单式构造，不是黑名单式删减。
- **公开页的资源必须另开一条路**（`/api/share/{token}/…`）。原来的资源接口现在要登录，
  公开页拿不到 cookie。两条路的 URL 拼装**共用同一套函数**（`_asset_base`），
  否则迟早有一边忘了加新字段（比如双语）。
- **专辑是分组不是容器**：删专辑只把单集的 `album_id` 置空，绝不删单集。
- **批量生成要「单项失败不影响其他项」**。一个链接写错就让整批白等，比慢一点糟糕得多。
  抓不到的进 `failed` 并给出原因，能用的照常入队。

## 结构

```
backend/app/services/     ingest(预处理) prompts(Prompt) llm(DeepSeek/方舟) podcast_tts(语音)
                          figures(PDF封面+论文原图) illustration(生成信息图)
                          video(视频合成) pipeline(编排) branding(社区话术)
backend/app/auth.py       账号与会话（scrypt 口令 / 会话 cookie / 归属判定）
backend/app/worker.py     asyncio 队列，串行消费 + 分类重试
backend/tests/            384 项，改完必须全绿
frontend/src/api/         index(适配器) real(真实) mock(浏览器端模拟)
frontend/src/views/        LandingView(首页) CreateView(表单) Library/Episode/Task/Settings
frontend/src/utils/language.ts  语言标签、清洗、按单集记住上次看的语言
```

## Mock 模式

`Settings.llm_mode` / `tts_mode` 会根据密钥是否齐全自动判断，也可用 `FORCE_MOCK=true` 强制。
前端另有独立的浏览器端 Mock（`VITE_USE_MOCK=1`），不依赖后端，用于静态站点演示。
