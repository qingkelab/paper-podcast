# 动态图解镜头：用论文里已有的信息画一张会长的图

用户的要求：**借鉴参考片的动效与节奏，用论文中已有的信息生成画面，不让视频模型自由编造技术图表**。
第一版只做一种新镜头，先出一段可验收的样片，再决定要不要铺到整集。

## 三条硬规矩

1. **画面里的每个数字和术语都要有出处**。`DiagramItem.source` 记着它是从哪来的
   （论文解读的某个字段、或脚本原句、或图注）。`verify_shot()` 拿证据文本逐条核验，
   **查不到就丢掉那一条**；节点不够就整条镜头放弃，退回原来的画面流程。
2. **模型只出结构化草案**。模型给的是「标题 / 节点 / 连线 / 指标 + 出处」这一份数据，
   几何、配色、字号、动效全部由 `diagram.py` 决定 —— 这是「不许编造图表」的落法。
3. **动效只有四类**：淡入、位移、连线绘制、数字递增（外加一次轻微缩放）。
   每个镜头共用同一套，不各做一套。

## 数据（`video_core`）

```python
DiagramNode(id, label, detail, source)        # 一个节点 / 一张卡片
DiagramLink(src, dst, label)                  # 状态 k 只画前 k 条 → 一条一条接上去
DiagramMetric(label, value, unit, source)     # 论文里明确给出的数字 + 出处
DiagramShot(kind, title, nodes, links, metrics, bullets, fallback_image)
Scene(..., diagram: DiagramShot | None)       # 唯一的入口：非空时这一段画图解
```

`kind` 只有五种：`hook`（开场钩子）/ `flow`（方法流程）/ `compare`（卡片+箭头）/
`metric`（数字与条形）/ `summary`（结论收束，通常 `fallback_image` 切回论文原图）。

## 怎么动起来（复用现有管线，没有新框架）

一个镜头 = 若干**状态**：状态 0 是「标题 + 第一个节点」，之后每个状态只多一件事
（多一个节点 / 多画一段连线 / 数字滚到下一个值）。

- 状态图由 `diagram_svg()` 画（**HTML 与 SVG 两条路径共用同一份 SVG**，所以
  「有 Chrome 和没 Chrome 出的图不一样」这种问题不存在）；
- 渲染走图片卡那块地方（`layout.image_box_w × image_box_h`），所以转场、边框、位置全部复用；
- 编码侧用 `encode_video(..., diagram_states=[[(path, 相对秒数), …], …])`：
  每张状态图按时间点叠加（`fade` + `enable='gte(t,…)'`）。**状态数与字幕句数解耦** ——
  几个节点就几步，不会被「一句最多两拍」压扁。

## 样片（`data/videos/sample-diagram.mp4`，32.8 秒）

素材：`f7c8af00`（STEPQuant）真实解说的 **137.5–173.5 秒**那一段（问题 → 对比 → 方法 → 指标 → 对比）。
时间窗**直接取自这一集真实的 TTS 时间轴**（`timings_json`），所以「切换与对应句子同步」不是估的。
生成脚本：`/tmp/build_sample.py`（自包含，走的就是生产同一条路）。

| 镜头 | 类型 | 讲什么 | 数字出处 |
|---|---|---|---|
| 1 | hook | 这个近似，会让结果不可信吗？ | 脚本原句 |
| 2 | compare | 6 比特稳、4 比特在 KDA 长生成掉精度 | 脚本原句 |
| 3 | flow | 时间维度 → 比特分配 → 空间维度（连线一条条画） | 脚本 + 解读 innovations |
| 4 | metric | 状态内存压缩 5 倍、Qwen 状态内存 28 MB（数字递增） | 脚本原句 |
| 5 | metric | 总服务内存 68.7%、吞吐提升 20% | 脚本原句 |
| 6 | summary | STEPQuant 6.3 bit vs DAMP 9.9 bit，切回论文原图 | 脚本原句 |

**验收结果（都是量出来的）**：

| 判据 | 结果 |
|---|---|
| 每个数字/术语可追溯 | 6/6 镜头通过 `verify_shot`（编造的数字会被丢掉，有测试钉着） |
| 竖版 936×1210 不裁切 | 墨迹离边缘最近 **0px**（没贴边、没溢出） |
| 横版 1920×1080 不裁切 | 同上，**0px** |
| Chrome 不可用时降级 | SVG 光栅化正常出图（同一份 SVG） |
| 时长与配音一致 | 32.8 秒，取自音频窗口，`-shortest` 无错位 |
| 「每 2–4 秒有新东西」 | 静止空档 **0.0%**（对照：整集现状 21.2%，改造前 59.6%） |
| 响度 | -15.8 LUFS（同一套归一化） |

## 接入流水线（模型只提草案，代码决定画面）

```
diagram_evidence(analysis, segments, figures)      # 允许作为依据的文本（解读 + 脚本原句 + 图注）
        ↓
diagram_candidates(segments, timings)              # 只挑「够长 + 话里有数字/步骤/对比」的段
        ↓
prompts/diagram.build_diagram_messages(...)        # 把候选段 + 证据 + 可用图 id 交给模型
        ↓  llm._chat_json
prompts/diagram.normalize_diagram_shots(raw)       # 认不出的字段直接丢，不抛异常
        ↓
diagram.verify_shot(shot, evidence)                # **逐条核验**：查不到依据的条目丢掉
        ↓                                          # 节点不够 → 整条作废
build_scenes(..., diagram_shots={段号: shot})      # 落到 Scene.diagram；片尾品牌段永不画
        ↓
video_encode(diagram_states=…) / video_render     # 按时间点叠加状态图（已有）
```

**「没有依据就不渲染」是默认行为，不是异常分支**：核验不过的镜头根本不会进入 `Scene`，
那一段照旧画论文原图或示意图。`tests/test_diagram.py::TestPipelineWiring` 钉着这条
（编造的数字 → 整段作废；没有模型 → 一个镜头都不提；片尾段永不画）。

**Prompt 的三条硬要求**（`prompts/diagram.py`）：只写材料里出现过的数字与术语、
每条都带 `source` 原句、只在真的有结构的段落提镜头。规模上限（镜头数 / 节点数 / 指标数 /
标题字数）在 `design.py` 里有一份常量，**prompt 里的数字与代码上限同源**（测试盯着）。

## 复用：重建不再问模型

镜头随段存进 `video.scenes[i].diagram`（`diagram_shot_to_dict/from_dict`），
`compose_video` 在**有 preset 时只读这份数据**，一次模型调用都不发 ——
规矩同 `point` / `hook`：重建不该换画面，也不该再花一次调用。
`tests/test_diagram.py::test_rebuild_reuses_the_shot_without_calling_the_model`
量的是「有没有调模型」（假模型记着 `calls`，必须是空的 —— 只比画面一不一样不够，
重复调用一样能画出同一张图）。

**状态图渲染接在图片卡那一步**：`_render_scene_layers` 里遇见 `scene.diagram` 就渲染
`1..n-1` 个状态（均匀铺在本段时长里），交给 `encode_video(diagram_states=…)`。

## 状态什么时候出现：跟着讲稿的句子走

`diagram.state_moments(text, duration, state_count, pauses=…)`：句子边界用
`beat_windows_aligned`（**与字幕带、逐句卡同一组窗口**，含真实停顿），所以状态和字幕天然同步。
均匀铺会出现「人还在讲第一个节点、图上已经长到第三个」——参考片里最要紧的恰恰是这个对应关系。

两条兜底：**句子数不够分**（一句话要长出 3 个节点）→ 退回均匀铺；
**最后一个状态至少留 1.2 秒**再结束（刚长出来就切走等于白画）。

## 还没做的

- ~~**结论收束镜头**与 `fallback_image` 的联动~~ → **已接**：镜头最后一个状态之后，
  按 `DIAGRAM_FALLBACK_HOLD`(1.6s) 切回 `fallback_image` 指的那张**论文原图**
  （占位时刻 = max(上一状态 + 0.6s, 本段时长 − 1.6s)）。id 指不到真实素材就什么都不做
  —— 宁可停在图解的末状态，也不画一张空卡。
- **`kind=summary` 的自动挑选**：现在由模型决定，还没有「一节结束时优先收束」的规则。
- ~~**状态时机**：均匀铺在本段时长里~~ → **已改成跟着讲稿的句子走**（见下）。
