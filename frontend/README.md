# 论文解读 AI 播客 · 前端

Vue 3（`<script setup>` + TypeScript）+ Vite + Vue Router 4 + Pinia 实现的前端。
上传论文（PDF / arXiv 链接 / 粘贴文本）→ 后端深度解读 → 生成双人对谈脚本 → 合成双人播客音频。
界面是深色学术风，手写 CSS（原生 CSS 变量组织），不引入任何 UI 组件库与联网字体。

接口严格对齐 `docs/API.md`（冻结版 v1），页面只通过 `src/api/index.ts` 这个适配器访问数据。

---

## 三条命令

```bash
cd frontend

# 1) Mock 开发（纯浏览器离线：不连后端、不需要密钥，内置 4 篇示例论文）
pnpm install
pnpm dev:mock                 # http://localhost:5173

# 2) 真实后端开发（Vite proxy 把 /api 转发到 http://127.0.0.1:8000）
pnpm dev

# 3) 构建
VITE_USE_MOCK=1 pnpm build     # 离线演示版（GitHub Pages 用）
pnpm build                     # 真实后端版（base 为相对路径，必要时用 -- --base=/ 覆盖）
pnpm preview                   # 本地预览构建产物
```

类型检查：`pnpm typecheck`（等价于 `pnpm vue-tsc --noEmit`）。
`pnpm build` 会先跑一遍类型检查，类型不过就不会产出文件。

---

## 页面与路由（契约 §3）

| 页面 | 路由 | 用到的接口 |
|---|---|---|
| 首页 / 导入 | `/` | `POST /api/episodes`、`GET /api/options`、`GET /api/health` |
| 任务进度 | `/task/:id` | `GET /api/episodes/{id}`（每 1500ms 轮询）、`POST .../retry` |
| 播客库 | `/library` | `GET /api/episodes`、`DELETE /api/episodes/{id}` |
| 详情播放 | `/episode/:id` | `GET /api/episodes/{id}`、`.../audio`、`.../script.txt`、`.../analysis.md` |
| 设置 | `/settings` | `GET /api/options`、`GET /api/health`（偏好存 localStorage） |

Mock 模式下路由用 hash 历史（`/#/library`），这样静态托管刷新任意页面都不会 404；
真实后端模式用 history 模式，URL 就是 `/library` 这样的干净路径。

---

## 目录结构

```
src/
  api/
    index.ts       适配器：按 VITE_USE_MOCK 选择 real / mock，页面只 import 这里
    real.ts        真实后端（fetch，/api 前缀）
    mock.ts        浏览器端 Mock：内存状态机 + Blob 音频 + localStorage 持久化
    mockAudio.ts   OfflineAudioContext 合成正弦波 → 16bit PCM WAV Blob
    mockPapers.ts  4 篇内置示例（Attention / ResNet / LoRA + 1 个通用模板）
    types.ts       契约 §1 的数据模型
  components/      AppHeader、AudioPlayer、ScriptView、AnalysisView、StageTimeline、EpisodeCard、StatusBadge、ConfirmDialog
  views/           LandingView(首页·产品介绍)、CreateView(导入表单)、TaskView、
                   LibraryView、EpisodeView、SettingsView、NotFoundView
  stores/          preferences（localStorage 偏好）、meta（options + health + Mock 角标）
  utils/           format（时间/大小格式化）、stages（状态机与阶段定义）
  styles/main.css  设计系统（CSS 变量）
```

---

## Mock 模式（`VITE_USE_MOCK=1`）

- `src/api/index.ts` 是唯一入口，构建/运行时读 `VITE_USE_MOCK`：`1` 走 `mock.ts`，否则走 `real.ts`（真实 `fetch`，开发态由 Vite proxy 转发）。
- Mock 复刻契约全部语义：`POST /api/episodes` 后立即返回 `queued`，随后在内存里按 **每阶段约 700ms**
  推进 `queued → parsing(10%) → analyzing(35%) → scripting(55%) → synthesizing(75%) → completed(100%)`，
  `stage_label` 与进度值都和契约表格一致。
- 音频用 **Web Audio API（OfflineAudioContext）现场合成**一段约 36 秒的正弦波（按脚本段落切换音色、
  叠加 4.5Hz 音节调制），编码成 WAV Blob URL 当 `audio_url`；音频不可用时退化为纯 JS 正弦合成，
  所以播放器在离线站点上始终可用。
- 内置 4 篇示例：Attention Is All You Need、ResNet、LoRA（三篇真实论文，元信息与关键数字对齐原文），
  外加 1 篇通用模板（用户上传 PDF / 粘贴文本 / 任意链接时会套用它生成解读与脚本）。
  链接里带 `1706.03762` / `1512.03385` / `2106.09685` 会自动匹配到对应论文。
- 数据落 localStorage（`paper-podcast:mock:episodes:v1`），刷新后仍在；音频在加载时重新合成。
  设置页有「重置 Mock 数据」按钮可恢复初始示例。
- **演示失败与重试**：来源（文件名 / 链接 / 文本）里带 `fail-demo` 字样，任务会在「深度解读」阶段
  以 35% 失败，点「重新生成」后正常跑完。

---

## 播放器说明

`components/AudioPlayer.vue` 用 HTML5 `<audio>` 但隐藏原生控件，自己实现播放条：

- 播放/暂停、可拖动进度条（拖动过程只更新显示，松手才 seek）、当前时间/总时长、倍速 0.75x/1x/1.25x/1.5x、音量与静音。
- **Range 请求下的 duration 处理**：分块/Range 响应时 `loadedmetadata` 拿到的 `duration` 可能是
  `Infinity` 或 `NaN`；此时按标准做法先 seek 到 `1e101` 逼浏览器算出真实时长，拿到后跳回开头，
  并回退到契约里的 `Episode.audio_duration_sec`。
- 音频元素全程隐藏（`.audio-hidden`），播放状态由组件内的 `audio` 事件驱动。

---

## 已知限制

- Mock 音频固定约 36 秒（真实音频由后端豆包 TTS 合成，时长与 `est_duration_sec` 对应）；
  这是为了让浏览器端合成足够快，不影响播放器功能验证。
- Mock 模式下 `source_ref` 保留用户输入，解读/脚本是内置示例数据（页面上有明确提示），不是真实分析。
- 列表页的「加载更多」按 `limit`/`offset` 分页；内置示例只有 3~4 条，需要先多导入几篇才能看到分页效果。
