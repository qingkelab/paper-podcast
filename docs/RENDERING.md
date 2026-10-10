# 画面渲染：HTML（Chrome 无头）与 SVG（resvg）两条路径

## 为什么要换

老路径是「Python 手算坐标 → 拼 SVG → resvg」：折行靠一张**近似字宽表**、
放不下就**试六档字号**、数字高亮的底色靠**手工累加每个字的宽度**、
图片等比缩放和居中自己算、封面标题块高和面板高度自己算。
三千多行的 `video.py` 里有相当一部分在做浏览器本来就会做的事，
而且做得**不如浏览器准**（它量的是真实字体度量）。

实测两个例子：

- 封面标题「4 比特反超 INT8，显存降七成」，老路径 `_wrap` 断成了
  「4 比特反超 INT8，显存降七 / 成」——最后一行只有一个字。
  同一个 `Layout`、同一份数据，浏览器一行就放下了。
- 数字高亮：老路径要先算出「这一段文字的像素宽」再在它下面垫一个圆角矩形；
  HTML 里就是 `<span class="num">67%</span>` 一个内联背景。

## 三档：决定「一页要渲染几帧」

| 档 | 做什么 | 成本 |
|---|---|---|
| **A 静态页** | 一页只渲染 **1 帧**，静止时长交给 ffmpeg 重复帧 | 整页 125ms / 图片卡 106ms / 字幕带 83ms |
| **B 内容驱动动画页** | 只对**动画窗口**逐帧抓取，之后停在末帧 | 约 74ms/帧（含 seek） |
| **C 时间轴** | concat / 混音 / 时长对齐仍然归 ffmpeg | 不变 |

**整片逐帧是不划算的**：250 秒 × 30fps = 7500 帧 ≈ 15 分钟 + 约 1.1GB PNG，
而整条管线现在只要 40~95 秒。

## 一条真实出片的实测（250 秒、36 段、936×1210）

```
19:32:00 画面渲染走 HTML（Chrome 无头）
19:32:14 已渲染 36 帧画面（片尾品牌卡 4，字幕分句 18 段，转场 16 处）   ← 渲染阶段 13 秒
19:32:52 视频已重新合成：36 帧 / 250.8 秒                                ← 整条 52 秒
```

`ffprobe`：视频 250.765s / 音频 250.931s（**长度对齐没破**）。

## 现在迁到哪儿了

| 图层 | 状态 |
|---|---|
| 骨架页（白底 + logo + 图注 + 字幕带） | ✅ HTML |
| 图片卡（图片 + 细边框 / 封面毛玻璃 + 玻璃面板） | ✅ HTML |
| 强调行（滑入的独立一层） | ✅ HTML |
| 片尾品牌卡 | ⬜ 还是 SVG |
| 图内聚光灯 | ⬜ 还是 SVG |
| 字幕带（逐句出现） | ⬜ **有意**留在 SVG，理由见下 |

字幕带是**唯一**没搬的一层，理由是算出来的：它是 500 次渲染里的大头
（500 × 83ms ≈ 40 秒），而它又是版式最简单的一层（一行字 + 数字底色）。
**下一步**：把一场戏的所有句子拼成**一张长图**、ffmpeg 用 `crop` 取每一条 ——
渲染次数从 500 降到 30 左右（预计 40s → 2s）。做完再搬才划算。

## 两条路径怎么共存

`video._render_scene_layers()` 是唯一的入口，它开着 `render_session(work_dir)`：

```python
with render_session(work_dir) as session:      # HTML 可用 → ChromeSession；否则 None
    _render_chrome_layer(..., session=session)  # session 是 None 就自己退回 render_chrome()
    _render_card_layer(..., session=session)
    _render_point_layer(..., session=session)
```

- `htmlframe.backend_name()` 是**唯一**的判据来源：`RENDER_BACKEND=svg` 强制走老路；
  要求 `html` 但机器上没有 Chrome 时**自动降级**（并记一条日志）。
  「这条机器没装 Chrome」不该变成「出不了片」。
- `RENDER_HTML_LAYERS=0` 可以单独关掉图层走 HTML（不必回滚代码）。
- 出片用的哪条路径存进 `video.renderer`（`html`/`svg`）——
  否则「同一篇论文两台机器出的画面不一样」光看 MP4 是查不出来的。
- **测试默认走 SVG**（`tests/conftest.py` 里 autouse 设 `RENDER_BACKEND=svg`）：
  既快又确定。真正关心 HTML 的测试自己开（`tests/test_htmlpage.py`）。

## 页面与渲染器的约定

- 页面必须**自包含**（图片走 data URI）—— 这样一份 HTML 丢到哪都能渲染出同样的画面，
  截图失败的现场也能直接丢进浏览器看。
- `window.prepare()`（可选、异步）：在「加载完 + 字体就绪 + 图片解码完」之后、
  第一次截图之前被 await。字号自适应（`fitText`）走这里。
- `window.seek(seconds)`（可选）：JS 动画的 seek 钩子。CSS/WAAPI 动画不用它 ——
  渲染器自己会 `document.getAnimations()` 全暂停再 seek。

## 踩过的坑（都是实测）

- **必须 `--no-sandbox`**：默认沙箱在本机报
  `sandbox initialization failed: Operation not permitted`。
- **必须独立的 `--user-data-dir`**：跟正在运行的浏览器共用一个 profile 会失败。
- **`chrome --headless --screenshot` 写完 PNG 之后进程不退出**（看着像挂死 15 秒）。
  所以走 CDP，不用那条 CLI。
- **CDP 的 `Emulation.setVirtualTimePolicy({policy:"pause", budget})` 会报
  `Can only specify budget for non-Pause policy`**，而且虚拟时间**不推进 CSS 动画**
  （实测进度条一直停在 0px）。动画必须用 WAAPI seek。
- **`Page.setDocumentContent` 的文档源是 `about:blank`**，从那里加载 `file://` 图片
  会被拦掉（跨源）。我们所有页面都把图片内联成 data URI，所以不受影响 ——
  但**别**把图片改成按路径引用。
- **图层页必须显式关掉白底**（`Emulation.setDefaultBackgroundColorOverride`）。
  Chrome 默认给不透明白底，图层多带一整块白就会把下面那层整块盖掉，
  而画面看起来还是「白的」—— 不报错，只是别的东西全不见了。
- **图层页的区域元素必须重置到原点**。图层页的画布**就是**那一块区域，
  而 CSS 里 `.point-row` 的 `top` 是 `878px`（整页坐标）；不重置的话内容整个落在画布外，
  截出来是一张空图。
- **seek 之后要等两帧 rAF**（一帧让样式生效、一帧让合成器跟上）。少这一等会截到**上一帧**。
- **Playwright 装浏览器在这台机器上超时**，别走它。
- **截图本身占 42ms**（936×242 的 PNG），这是每条字幕带 83ms 里的大头 ——
  换 JPEG(95) 只快 4ms，不值（文字边缘还会有压缩痕迹）。
