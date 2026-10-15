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
- ~~**`kind=summary` 的自动挑选**~~ → **已做**（见下）：收尾段由**位置**定，不由模型猜。
- ~~**状态时机**：均匀铺在本段时长里~~ → **已改成跟着讲稿的句子走**（见下）。


## 收尾镜头落在哪儿：由位置定，不由模型猜

`summary`（一句话总结 + 切回论文原图）该出现在**全集的最后**，但模型只看到段号和文字，
它不知道「这是最后一段」。所以位置由 `video.summary_candidates(segments)` 算出来 ——
规则是确定的：**紧挨片尾品牌段的那一段正文**（没有片尾段就没有这个位置）。

- 收尾段**始终进候选**，哪怕它没有数字、没有步骤词 —— 「结论该落在哪」是位置问题，
  不是内容问题；但**太短**（< 2.5 秒）仍然会被丢掉（一两秒的收尾挂一张图反而更差）。
- prompt 里那一段会被标上「← **收尾段**（优先提 `summary`，`fallback_image` 填论文原图 id）」，
  并写明收尾段优先提 summary。
- 测试：`TestSummaryShotsAreChosenByPosition` 五条（位置判定、没有片尾就没有、
  收尾段始终进候选、太短的收尾段仍然丢掉、prompt 里确实标了）。

## 两条「静默错位」：行数必须与段数 1:1，镜头必须写回 `video.scenes`

**先说症状**（用户原话：「生成的视频，第二个画面有问题，而且又会跳回到封面」）：
成片里**第 2 段**（本该是封面那几帧）突然被一张图解盖住，下一段又回到封面，
看起来像「画面自己跳了一下又跳回来」。

根因在渲染循环的这一处不对称：

```python
if scene.brand == "outro":      result.diagram_states.append([])     # 片尾 → 占一行
elif scene.diagram is not None: result.diagram_states.append(states) # 有图解 → 占一行
else:                           _render_card_layer(...)             # 没有图解 → **没占行**
```

编码侧是按**段号**取的（`diagram_rows[index]`），少一行就**整体错位**：
这一集本来属于第 11 段的图解，被叠到了**第 1 段**上。图解的后续状态是
「按句子的时间点淡入 + 上升」（`enable='gte(t,at)'`，`at` 是**段内相对时刻**），
所以它在第 1 段的第 3.2 秒才冒出来 —— 正好是用户看到的那个「第二个画面有问题」。

两处都修了：

1. **渲染侧**：没有图解的段也 `append([])`，行数与段数永远 1:1；
2. **编码侧**：`len(diagram_rows) != len(scenes)` 时**不叠图解并报警**（
   `video_encode.encode_video`）—— 错位这种事不该静默发生。

**第二条错位是数据层的**：写 `result.scenes` 的那段**没有把图解写进去**
（`build_scenes` 会读 `preset_scenes[i]["diagram"]`，却没人写）。后果是每次
「重新合成」都**静默丢掉图解** —— 少的不是几张图，而是「重新合成不该换画面」这条保证。
现在写进 `video.scenes[i].diagram`，重建时原样复用。

三条测试（`TestDiagramRowsStayAlignedWithScenes` / `TestTheShotSurvivesARebuild`）：
没有图解的段也要占一行、片尾也要占一行、行数对不上时编码侧拒绝按段号取、
以及**合成一次之后 `video.scenes[i].diagram` 必须能读回同一个镜头**。

## 核验不过的镜头：带着**机器的具体原因**再问一次

原来核验不过就只有一个动作 —— 整条镜头作废、退回论文原图，**写日志**。
问题是模型根本不知道自己写的哪个数字不在材料里，下一次提还是同样的写法；
而人回头看「这一集怎么一个图解都没有」时，日志早就滚没了。

现在分两种处理（`video._verify_diagram_drafts` + `assign_diagram_shots`）：

| 核验结果 | 处理 |
|---|---|
| 有条目被删、镜头还立得住 | **照用**（删就是对的安全动作），但记进台账：删了哪一项、为什么 |
| 整条作废 | **带原因再问一次**（只一次、只重提被作废的那几条） |

修复提示长这样（`prompts/diagram.build_diagram_repair_note`，逐字来自 `verify_shot`）：

```
【上一次的草案里，下面这些条目没有通过机器核验】**这一次只需要重提这几条**：
- 第 12 段「三类评分项」（flow）：指标「999兆」查不到依据；节点「…」查不到依据
这些条目要么改成**材料里原样出现过的**说法（数字、术语一个字都不要自己改），
要么直接删掉那一条 —— 不要凑整、不要推算、不要换一个相近的数字。
已经通过核验的镜头不要重写，段落编号必须和上面一致。
```

这条来自参考做法（[Fibona agent](https://github.com/FibonaAI/fibona-agent)）里最硬的一条规矩：
**修复的输入是真实的错误，不是「再试一次」**（它的宿主在 cell 抛异常时，
会把失败的代码和错误一起交给模型重写）。我们的错误本来就是机器逐条生成的，
比它的还干净 —— 直接喂回去就行。

台账存进 `video.diagram_report`（`video_json` 里，空的时候是 `null`）：

```json
{"drafts": 6, "kept": [7, 11, 13],
 "partial": [{"segment": 11, "title": "三类评分项", "dropped": ["节点「…」查不到依据"]}],
 "rejected": [{"segment": 19, "title": "…", "dropped": ["标题无依据 → 整条镜头放弃"]}],
 "repair": {"called": true, "attempted": [19], "fixed": [19], "still_rejected": []}}
```

测试：`tests/test_diagram.py::TestRejectedDraftsAreRepairedWithTheRealReason` 四条
（被拒的要带原话与原因重问、已通过的不重写、没有作废就不多花一次调用、
只删一项时照用并记账）。
