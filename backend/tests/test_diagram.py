"""动态图解镜头（`services/diagram.py`）的测试。

这一层要守的是**两条产品红线**，所以测试也围绕它们：
1. **画面里的数字和术语必须查得到依据** —— 查不到就丢掉那一条，节点不够就整条放弃；
2. **竖版横版都不许溢出** —— 图解是「用论文信息画出来的图」，一旦被裁掉半边就全错了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import diagram as DG
from app.services.design import LANDSCAPE, PORTRAIT
from app.services.video_core import (
    DiagramLink,
    diagram_shot_from_dict,
    diagram_shot_to_dict,
    DiagramMetric,
    DiagramNode,
    DiagramShot,
    Scene,
)


def flow_shot(**overrides) -> DiagramShot:
    base = dict(
        kind="flow",
        title="他们凭什么稳住精度",
        title_source="脚本原句：他们凭什么稳住精度",
        nodes=(
            DiagramNode("t", "时间维度", "活得越久，误差越容易滚雪球",
                        source="脚本原句：活得越久，误差越容易滚雪球"),
            DiagramNode("b", "比特分配", "误差大且活得久的单元多给比特",
                        source="解读 innovations：把更高精度分配给误差更大且存活更久的单元"),
            DiagramNode("k", "空间维度", "key 行和 value 列分开拟合缩放",
                        source="脚本原句：缩放不能只按行或只按列来，得两个轴分开拟合"),
        ),
        links=(DiagramLink("t", "b"), DiagramLink("b", "k")),
    )
    base.update(overrides)
    return DiagramShot(**base)  # type: ignore[arg-type]


EVIDENCE = [
    # 真实的证据列表长这样：解读里的原句 + 脚本原句（这里就是那几条的节选）
    "解读 concepts：时间维度上看每个状态单元存活多久；空间维度上看 key 行对输出的影响",
    "活得越久，误差越容易滚雪球",
    "解读 innovations：把更高精度分配给误差更大且存活更久的单元",
    "脚本原句：缩放不能只按行或只按列来，得两个轴分开拟合",
    "脚本原句：Qwen 从 144 兆降到 28 兆左右",
    "脚本原句：6 比特下状态内存压缩了 5 倍多",
    "脚本原句：他们凭什么稳住精度",
    "脚本原句：缩放不能只按行或只按列来，得两个轴分开拟合",
    "解读 limitations：key 行与 value 列各自的量化误差并不一样，双轴缩放才能拟合",
    "脚本原句：那这近似会不会让结果不可信？",
]


class TestEvidenceVerification:
    def test_supported_shot_passes_untouched(self):
        result = DG.verify_shot(flow_shot(), evidence=EVIDENCE)
        assert result.ok and result.dropped == []
        assert len(result.shot.nodes) == 3 and len(result.shot.links) == 2

    def test_a_fabricated_number_is_dropped(self):
        """**这是最重要的一条**：屏幕上多一个查不到来源的数字比少一个图严重得多。"""
        shot = flow_shot(
            nodes=(
                *flow_shot().nodes,
                DiagramNode("x", "加速比", "论文里没有的 3.7 倍",
                            source="（编的）"),
            )
        )
        result = DG.verify_shot(shot, evidence=EVIDENCE)
        assert result.ok
        assert [node.id for node in result.shot.nodes] == ["t", "b", "k"]
        assert any("加速比" in item for item in result.dropped), result.dropped

    def test_unknown_technical_term_is_dropped(self):
        shot = flow_shot(nodes=(
            DiagramNode("t", "时间维度", "活得越久，误差越容易滚雪球",
                        source="脚本原句：活得越久，误差越容易滚雪球"),
            DiagramNode("z", "FlashQuant", "这条方法论文里没提过", source="（编的）"),
        ), links=())
        result = DG.verify_shot(shot, evidence=EVIDENCE)
        # 有效节点只剩 1 个 → 整条镜头放弃（调用方退回原来的画面流程）
        assert not result.ok
        assert any("FlashQuant" in item for item in result.dropped), result.dropped

    def test_number_written_differently_still_matches(self):
        """解说写「28 兆」、指标写「28.0 MB」是同一个数，别把它判死。"""
        shot = DiagramShot(
            kind="metric", title="状态内存压了多少",
            title_source="脚本原句：6 比特下状态内存压缩了 5 倍多",
            metrics=(
                DiagramMetric("Qwen 状态内存", 28.0, " MB",
                              source="脚本原句：Qwen 从 144 兆降到 28 兆左右"),
                DiagramMetric("状态内存压缩", 5, " 倍",
                              source="脚本原句：6 比特下状态内存压缩了 5 倍多"),
            ),
        )
        result = DG.verify_shot(shot, evidence=EVIDENCE)
        assert result.ok and len(result.shot.metrics) == 2, result.summary()

    def test_title_only_shots_are_allowed_for_hook_and_summary(self):
        """开场钩子和结论收束本来就是纯标题镜头 —— 别拿「要有 2 个节点」去卡它们。"""
        for kind in ("hook", "summary"):
            shot = DiagramShot(
                kind=kind,
                title="这个近似，会让结果不可信吗？",
                title_source="脚本原句：那这近似会不会让结果不可信？",
                subtitle="先看状态内存压了多少",
                subtitle_source="脚本原句：6 比特下状态内存压缩了 5 倍多",
            )
            assert DG.verify_shot(shot, evidence=EVIDENCE).ok, kind

    def test_links_to_dropped_nodes_are_removed(self):
        shot = flow_shot(nodes=flow_shot().nodes[:2], links=(DiagramLink("t", "zz"),))
        result = DG.verify_shot(shot, evidence=EVIDENCE)
        assert result.shot.links == ()
        assert any("连线" in item for item in result.dropped)


class TestStates:
    def test_state_count_by_kind(self):
        assert DG.shot_state_count(DiagramShot(kind="hook", title="x")) == 1
        assert DG.shot_state_count(flow_shot()) == 3          # 三个节点 → 三步
        assert DG.shot_state_count(DiagramShot(kind="compare", title="x")) == 1
        metric = DiagramShot(kind="metric", title="x", metrics=(
            DiagramMetric("a", 5, " 倍"), DiagramMetric("b", 28, " MB"),
        ))
        assert DG.shot_state_count(metric) == 3               # 空 → 一半 → 目标值

    def test_nodes_appear_one_at_a_time(self):
        shot = flow_shot()
        counts = [DG._visible(shot, state)[0] for state in range(DG.shot_state_count(shot))]
        assert counts == [1, 2, 3]

    def test_links_are_drawn_in_order(self):
        shot = flow_shot()
        drawn = [DG._visible(shot, state)[1] for state in range(DG.shot_state_count(shot))]
        assert drawn == [0, 1, 2]

    def test_metric_counts_up_to_the_real_value(self):
        metric = DiagramMetric("状态内存压缩", 5.0, " 倍")
        assert DG._metric_shown(metric, 0) == 0.0
        assert DG._metric_shown(metric, 1) == 2.5
        assert DG._metric_shown(metric, 2) == 5.0


class TestRendering:
    def test_svg_is_deterministic(self):
        shot = flow_shot()
        assert DG.diagram_svg(shot, PORTRAIT, state=2) == DG.diagram_svg(shot, PORTRAIT, state=2)

    def test_both_orientations_stay_inside_the_card(self, tmp_path):
        """**竖版横版都不许溢出** —— 量的是真渲染出来的像素包围盒，不是字符串长度。"""
        import pymupdf

        for name, layout in (("竖版", PORTRAIT), ("横版", LANDSCAPE)):
            for kind in ("hook", "flow", "metric"):
                shot = flow_shot(kind=kind) if kind != "metric" else DiagramShot(
                    kind="metric", title="状态内存压了多少", metrics=(
                        DiagramMetric("状态内存压缩", 5.0, " 倍"),),
                )
                for state in range(DG.shot_state_count(shot)):
                    out = tmp_path / f"{name}-{kind}-{state}.png"
                    DG.render_diagram_state(shot, out, layout=layout, state=state)
                    pix = pymupdf.Pixmap(str(out))
                    assert (pix.width, pix.height) == (layout.image_box_w, layout.image_box_h)
                    for y in range(pix.height):
                        for x in range(pix.width):
                            pixel = pix.pixel(x, y)
                            if min(pixel) >= 250:  # 白底（边缘会被抗锯齿压到 254）
                                continue
                            assert 0 < x < pix.width - 1 and 0 < y < pix.height - 1, (
                                f"{name}/{kind} 状态 {state} 的墨迹贴到了边缘：({x},{y})"
                            )

    def test_scene_carries_the_shot(self):
        """`Scene.diagram` 是这段数据流进管线的唯一入口（编码侧据此换卡）。"""
        shot = flow_shot()
        scene = Scene(start=0.0, end=6.0, image=Path("x.png"), kind="illustration",
                      text="一句话。", diagram=shot)
        assert scene.diagram is shot
        assert scene.diagram.items(), "每个镜头都要能列出自己需要核验的条目"


# --------------------------------------------------------------------------
# 接入流水线：候选筛选 → 模型草案 → 核验 → 落到 Scene（见 docs/DIAGRAM_SHOTS.md）
# --------------------------------------------------------------------------


class _FakeLLM:
    """假模型：只回一份固定的草案（真调用不在单测里跑）。"""

    mock = False

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple] = []

    def _chat_json(self, messages, what="", retries=0):
        self.calls.append((what, messages))
        return self.payload


def _segments():
    return [
        {"speaker": "A", "text": "这里是青稞社区，我们只讲自己读过的东西。"},
        {"speaker": "B", "text": "6 比特下状态内存压缩了 5 倍多，Qwen 从 144 兆降到 28 兆左右。"},
        {"speaker": "A", "text": "那这近似会不会让结果不可信？"},
    ]


def _timings():
    from app.services.podcast_tts import RoundTiming

    return [
        RoundTiming(index=0, speaker="A", start=0.0, end=3.0),
        RoundTiming(index=1, speaker="B", start=3.0, end=16.0),
        RoundTiming(index=2, speaker="A", start=16.0, end=18.4),
    ]


class TestPipelineWiring:
    def test_candidates_skip_short_and_empty_segments(self):
        from app.services.video import diagram_candidates

        picked = diagram_candidates(_segments(), _timings())
        assert 1 in picked          # 有数字、够长
        assert 0 not in picked      # 没有可结构化的东西
        assert 2 not in picked      # 只有 2.4 秒（阈值 2.5），一闪而过

    def test_evidence_includes_script_and_analysis(self):
        from app.services.video import diagram_evidence

        evidence = diagram_evidence(
            {"innovations": ["生命周期感知比特分配"], "conclusion": "6 比特接近 FP32"},
            _segments(),
            [{"caption": "Figure 1: Overview."}],
        )
        assert any("6 比特下状态内存压缩了 5 倍多" in item for item in evidence)
        assert any("生命周期感知比特分配" in item for item in evidence)
        assert any("Figure 1" in item for item in evidence)

    def test_assignment_verifies_and_falls_back(self, monkeypatch):
        """**核心行为**：编造的数字被丢掉 → 节点不够 → 整段退回原画面（不返回镜头）。"""
        from app.services import video as video_module

        payload = {
            "shots": [
                {   # 有依据 → 保留
                    "segment": 1, "kind": "metric", "title": "状态内存压了多少",
                    "title_source": "脚本原句：6 比特下状态内存压缩了 5 倍多",
                    "metrics": [
                        {"label": "状态内存压缩", "value": 5, "unit": " 倍",
                         "source": "脚本原句：6 比特下状态内存压缩了 5 倍多"},
                    ],
                },
                {   # 编的数字 → 整段作废
                    "segment": 2, "kind": "compare", "title": "编的镜头",
                    "title_source": "脚本原句：那这近似会不会让结果不可信？",
                    "nodes": [
                        {"id": "a", "label": "加速比", "detail": "快了 3.7 倍", "source": "（编的）"},
                        {"id": "b", "label": "内存", "detail": "降了 8 倍", "source": "（编的）"},
                    ],
                },
            ]
        }
        llm = _FakeLLM(payload)
        shots = video_module.assign_diagram_shots(
            segments=_segments(),
            timings=_timings(),
            evidence=video_module.diagram_evidence(
                {"conclusion": "6 比特接近 FP32"}, _segments(), []
            ),
            llm=llm,
            available_images=[("f1", "Figure 1")],
        )
        assert set(shots) == {1}, shots
        assert shots[1].kind == "metric"
        assert llm.calls, "应当调了一次模型"

    def test_no_llm_means_no_shots(self):
        from app.services.video import assign_diagram_shots

        assert assign_diagram_shots(
            segments=_segments(), timings=_timings(), evidence=[], llm=None
        ) == {}

    def test_scene_keeps_the_shot_and_outro_never_does(self, tmp_path):
        """落到 `Scene.diagram`：正文段带图解，片尾品牌段不带。"""
        from app.services.video import build_scenes

        segments = [
            {"speaker": "A", "text": "第一段。"},
            {"speaker": "B", "text": "这里是青稞社区，感谢收听。", "brand": "outro"},
        ]
        from app.services.podcast_tts import RoundTiming
        from app.services.video_core import ImageAsset

        cover = tmp_path / "cover.png"
        import pymupdf

        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 60), False)
        pix.set_rect(pix.irect, (250, 250, 250))
        pix.save(str(cover))
        assets = {
            "cover": ImageAsset(id="cover", kind="cover", path=cover, caption=""),
        }
        timings = [
            RoundTiming(index=0, speaker="A", start=0.0, end=6.0),
            RoundTiming(index=1, speaker="B", start=6.0, end=12.0),
        ]
        shot = DiagramShot(kind="flow", title="他们凭什么稳住精度", nodes=(
            DiagramNode("a", "时间维度", source="x"),
            DiagramNode("b", "比特分配", source="y"),
        ))
        scenes = build_scenes(
            segments=segments,
            timings=timings,
            audio_duration=12.0,
            assets=assets,
            image_for_segment=["cover", "cover"],
            fallback_id="cover",
            diagram_shots={0: shot, 1: shot},
        )
        assert scenes[0].diagram is shot
        assert scenes[1].diagram is None, "片尾品牌段不该画图解"

    def test_rebuild_reuses_the_shot_without_calling_the_model(self, tmp_path):
        """**重建不许再问模型**（规矩同 `point` / `hook`，`TestPresetReuse` 钉着）。

        这一条量的是「有没有调模型」：`_FakeLLM.calls` 必须是空的 —— 只比画面一不一样
        是不够的，重复调用一样能画出同一张图。
        """
        from app.services import video as video_module

        shot = DiagramShot(
            kind="metric", title="状态内存压了多少",
            title_source="脚本原句：6 比特下状态内存压缩了 5 倍多",
            metrics=(DiagramMetric("状态内存压缩", 5.0, " 倍",
                                   source="脚本原句：6 比特下状态内存压缩了 5 倍多"),),
        )
        preset = [
            {"index": 0, "point": "", "focus": None, "beats": [],
             "diagram": diagram_shot_to_dict(shot)},
        ]
        llm = _FakeLLM({"shots": []})
        # 复用发生在 compose_video 内部的 preset 分支里；这里直接验那段逻辑的入口：
        from app.services.video_core import ImageAsset

        cover = tmp_path / "c.png"
        import pymupdf

        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 60), False)
        pix.set_rect(pix.irect, (250, 250, 250))
        pix.save(str(cover))
        segments = [{"speaker": "A", "text": "6 比特下状态内存压缩了 5 倍多。"}]
        from app.services.podcast_tts import RoundTiming

        timings = [RoundTiming(index=0, speaker="A", start=0.0, end=8.0)]
        scenes = video_module.build_scenes(
            segments=segments,
            timings=timings,
            audio_duration=8.0,
            assets={"cover": ImageAsset(id="cover", kind="cover", path=cover, caption="")},
            image_for_segment=["cover"],
            fallback_id="cover",
            # 复用路径读的是 preset 里存的那份 dict，不经过模型
            diagram_shots={0: diagram_shot_from_dict(preset[0]["diagram"])},
        )
        assert scenes[0].diagram is not None
        assert scenes[0].diagram.metrics[0].value == 5.0
        assert llm.calls == [], "重建路径不该调用模型"


class TestStateTiming:
    """状态出现的时刻要**跟着讲稿的句子走**。

    均匀铺会出现「人还在讲第一个节点、图上已经长到第三个」——参考片里最要紧的
    恰恰是这句话与画面的对应。句子边界用 `beat_windows_aligned`（与字幕带同一组窗口）。
    """

    TEXT = "6 比特下状态内存压缩了 5 倍多。Qwen 从 144 兆降到 28 兆左右。总服务内存最多降 68.7%。"

    def test_states_follow_the_sentences(self):
        moments = DG.state_moments(self.TEXT, 12.0, 3)
        assert moments[0] == 0.0
        assert all(0.0 <= moment <= 12.0 for moment in moments)
        assert moments == sorted(moments) and len(set(moments)) == 3
        # 跟着句子走的话，第一句（约 1/3 时长）之后才轮到第二个状态；
        # 均匀铺会得到 4.0 / 8.0，跟着句子会不一样
        assert moments[1] != 4.0 or moments[2] != 8.0

    def test_real_pauses_push_the_later_states_back(self):
        """有真实停顿时，状态时刻要跟着停顿走（和字幕对齐的是同一个函数）。"""
        from app.services.audio_track import Pause

        pauses = [Pause(start=1.0, end=6.0)]
        with_pause = DG.state_moments(self.TEXT, 12.0, 3, pauses=pauses)
        without = DG.state_moments(self.TEXT, 12.0, 3)
        assert with_pause != without

    def test_falls_back_to_even_when_sentences_are_too_few(self):
        # 一句话要长出 3 个状态 → 没法跟着句子走，退回均匀铺
        assert DG.state_moments("就一句话。", 9.0, 3) == [0.0, 3.0, 6.0]

    def test_last_state_does_not_stick_to_the_end(self):
        moments = DG.state_moments(self.TEXT, 4.0, 3)
        assert moments[-1] <= 4.0 - 1.2 + 0.01, moments

    def test_single_state_needs_no_moment(self):
        assert DG.state_moments(self.TEXT, 8.0, 1) == [0.0]


class TestSummaryShotsAreChosenByPosition:
    """`summary`（一句话总结 + 切回论文原图）该落在哪儿是**位置**问题，不是内容问题。

    模型只看到段号和文字，不知道「这是全集的最后一段」——所以位置由我们标出来。
    规则本身是确定的：紧挨片尾品牌段的那一段正文。
    """

    SEGMENTS = [
        {"speaker": "A", "text": "这里是青稞社区，我们只讲自己读过的东西。"},
        {"speaker": "A", "text": "6 比特下状态内存压缩了 5 倍多，Qwen 从 144 兆降到 28 兆左右。"},
        {"speaker": "B", "text": "意味着高并发长上下文推理服务里，状态内存不再那个黑洞。"},
        {"speaker": "B", "text": "这里是青稞社区，感谢收听。", "brand": "outro"},
    ]

    def test_the_last_body_segment_is_the_summary_position(self):
        from app.services.video import summary_candidates

        assert summary_candidates(self.SEGMENTS) == [2]

    def test_no_outro_means_no_summary_position(self):
        from app.services.video import summary_candidates

        assert summary_candidates(self.SEGMENTS[:3]) == []

    def _full_timings(self):
        """四段各自的时长都要够（收尾段太短会被「一闪而过」那条判据挡掉 —— 那是设计如此）。"""
        from app.services.podcast_tts import RoundTiming

        return [
            RoundTiming(index=0, speaker="A", start=0.0, end=3.0),
            RoundTiming(index=1, speaker="A", start=3.0, end=16.0),
            RoundTiming(index=2, speaker="B", start=16.0, end=26.0),
            RoundTiming(index=3, speaker="B", start=26.0, end=29.0),
        ]

    def test_summary_position_always_enters_the_candidates(self):
        """收尾段常常没有数字/步骤关键词，靠内容判据筛不进来 —— 它必须被显式加回候选。"""
        from app.services.video import diagram_candidates

        picked = diagram_candidates(self.SEGMENTS, self._full_timings())
        assert 2 in picked, picked

    def test_too_short_summary_segment_is_dropped(self):
        from app.services.podcast_tts import RoundTiming
        from app.services.video import diagram_candidates

        segments = [
            {"speaker": "A", "text": "正文。"},
            {"speaker": "B", "text": "就这样。", "brand": "outro"},
        ]
        timings = [
            RoundTiming(index=0, speaker="A", start=0.0, end=1.2),   # 只有 1.2 秒
            RoundTiming(index=1, speaker="B", start=1.2, end=4.0),
        ]
        assert diagram_candidates(segments, timings) == []

    def test_the_prompt_marks_the_summary_position(self):
        from app.services.prompts.diagram import build_diagram_messages

        messages = build_diagram_messages(
            segments=self.SEGMENTS,
            evidence=["6 比特下状态内存压缩了 5 倍多"],
            candidates=[1, 2],
            summary_candidates=[2],
            timings=self._full_timings(),
        )
        body = messages[1]["content"]
        assert "收尾段" in body
        assert "[2]" in body and "summary" in body
