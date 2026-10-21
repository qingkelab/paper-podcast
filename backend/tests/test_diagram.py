"""动态图解镜头（`services/diagram.py`）的测试。

这一层要守的是**两条产品红线**，所以测试也围绕它们：
1. **画面里的数字和术语必须查得到依据** —— 查不到就丢掉那一条，节点不够就整条放弃；
2. **竖版横版都不许溢出** —— 图解是「用论文信息画出来的图」，一旦被裁掉半边就全错了。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.services import diagram as DG
from app.services.llm_parse import LLMError
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


def _ink_box(svg: str, layout) -> tuple[float, float, float, float, float]:
    """量一张卡上墨迹的包围盒（相对卡片宽高的比例）+ 空白行占比。

    用真像素量，不看字符串 ——「画面上有没有东西」只有渲出来才知道。
    """
    import tempfile

    import pymupdf

    from app.services.video_render import _rasterize_custom

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "card.png"
        _rasterize_custom(svg, out, width=layout.image_box_w, height=layout.image_box_h)
        pix = pymupdf.Pixmap(str(out))
    n, data, w, h = pix.n, pix.samples, pix.width, pix.height
    ink = lambda x, y: min(data[(y * w + x) * n], data[(y * w + x) * n + 1], data[(y * w + x) * n + 2]) < 246
    cols = [x for x in range(w) if any(ink(x, y) for y in range(0, h, 3))]
    rows = [y for y in range(h) if any(ink(x, y) for x in range(0, w, 3))]
    blank = sum(1 for y in range(h) if not any(ink(x, y) for x in range(0, w, 2)))
    return cols[0] / w, cols[-1] / w, rows[0] / h, rows[-1] / h, blank / h


class TestNoNearlyEmptyFrames:
    """**「避免出现这样的画面」**：一张只画了一个框（或只有一行标题）、其余大片留白的卡。

    用户截图里那张就是这样：标题 + 一个节点方框，画面 60% 是白的。三条改法各守一条：
    还没出现的节点画占位框、节点块竖向居中、没有节点时走大字版式。
    """

    def test_state_zero_draws_every_node_box_even_the_ones_not_yet_revealed(self):
        """状态 0 只亮第一个节点，但**占位框要先摆好** —— 否则构图从第 0 帧就是残缺的。"""
        shot = flow_shot()
        svg = DG.diagram_svg(shot, PORTRAIT, state=0)
        cards = [line for line in svg.splitlines() if "<rect" in line and 'rx="14"' in line]
        assert len(cards) == len(shot.nodes), f"状态 0 应该有 {len(shot.nodes)} 个框，实际 {len(cards)}"
        ghosts = [line for line in cards if 'stroke-dasharray="7 7"' in line]
        assert len(ghosts) == len(shot.nodes) - 1, "未出现的节点用虚线占位（第一个是实线高亮）"

    def test_the_node_block_sits_in_the_middle_not_against_the_top(self):
        """节点块竖向居中：上下留白对称，而不是「贴着标题、下面空一大片」。"""
        _, _, top, bottom, _ = _ink_box(DG.diagram_svg(flow_shot(), PORTRAIT, state=2), PORTRAIT)
        above, below = top, 1.0 - bottom
        assert abs(above - below) < 0.12, f"上下留白差太多：上 {above:.0%} / 下 {below:.0%}"

    def test_a_shot_with_only_a_title_becomes_a_big_centred_card(self):
        """hook / summary 常常只有一句话 —— 那是大字卡，不是「标题 + 一片白」。"""
        shot = DiagramShot(
            kind="summary",
            title="高方差是内在代价",
            title_source="脚本原句：高方差是内在代价",
            bullets=("优势要在在线自适应框架下才兑现得出来。",),
            bullet_sources=("脚本原句：优势要在在线自适应框架下才兑现得出来。",),
        )
        svg = DG.diagram_svg(shot, PORTRAIT, state=0)
        assert 'font-size="92"' in svg, "只有一句话时标题要放到最大一档"
        assert "· 优势要在在线自适应框架下才兑现得出来。" in svg, "要点并进居中那块"
        _, _, top, bottom, blank = _ink_box(svg, PORTRAIT)
        assert abs(top - (1.0 - bottom)) < 0.15, "整块要竖向居中"
        # 大字卡本来就只有两行字，**空白行占比天然高**（这不是「空画面」的判据）；
        # 要量的是「这一块有没有占住卡片中间」—— 上下跨度够不够。
        assert bottom - top > 0.35, f"整块只占了卡片高度的 {bottom - top:.0%}，还是薄薄一条"

    def test_a_single_metric_reaches_its_target_value(self):
        """只有一个指标时也要给满三个状态 —— 否则末状态停在「一半」，目标值永不出现。"""
        shot = DiagramShot(
            kind="metric", title="最高5.3倍，吞吐高66%",
            metrics=(DiagramMetric("吞吐提升", 66.0, "%"),),
        )
        assert DG.shot_state_count(shot) == 3
        shown = [DG._metric_shown(shot.metrics[0], state) for state in range(3)]
        assert shown[-1] == 66.0, f"末状态没到目标值：{shown}"

    def test_a_stored_shot_with_nothing_at_all_is_not_reused(self):
        """重建时复用旧镜头也要拦：什么都没的卡渲染出来就是一张纯白。"""
        from app.services.video_core import diagram_shot_is_drawable

        assert diagram_shot_is_drawable(flow_shot())
        assert diagram_shot_is_drawable(DiagramShot(kind="hook", title="一句话"))
        assert not diagram_shot_is_drawable(DiagramShot(kind="hook", title="   "))


# --------------------------------------------------------------------------
# 接入流水线：候选筛选 → 模型草案 → 核验 → 落到 Scene（见 docs/DIAGRAM_SHOTS.md）
# --------------------------------------------------------------------------


class _FakeLLM:
    """假模型：只回一份固定的草案（真调用不在单测里跑）。"""

    mock = False

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple] = []

    def _chat_json(self, messages, *, max_tokens: int, temperature: float = 0.6):
        """签名**必须**和 `LLMClient._chat_json` 一致。

        踩过的坑：假模型写成 `what=`/`retries=` 时测试全绿，而线上每次生成都在
        视频合成这一步 `TypeError` —— 用户看到的是「音频有、视频没有」。
        假件不照真实签名写，测的就只是它自己。
        """
        self.calls.append((max_tokens, messages))
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


class TestFallbackCardRenders:
    """`fallback_image` 那条路必须真的能渲染出来。

    踩过的坑：`_render_scene_layers` 里把素材池写成了 `pool`（那一层的参数叫 `assets`），
    于是**每次真出片**都在这一步 `NameError`，被 `_compose_video_safely` 兜住 ——
    用户看到的是「音频有、视频没有」。所以这里直接调那个函数，别再等真出片才发现。
    """

    def test_the_fallback_card_is_rendered_from_the_asset_pool(self, tmp_path):
        import pymupdf
        from app.services.video import _diagram_fallback_card
        from app.services.video_core import ImageAsset

        figure = tmp_path / "f1.png"
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 90), False)
        pix.set_rect(pix.irect, (240, 240, 240))
        pix.save(str(figure))
        shot = DiagramShot(
            kind="summary", title="效率优势明显",
            nodes=(DiagramNode("a", "时间维度", source="x"),),
            fallback_image="f1",
        )
        scene = Scene(
            start=0.0, end=6.0, image=tmp_path / "base.png", kind="illustration",
            text="一句话。", diagram=shot,
        )
        out = _diagram_fallback_card(
            scene,
            {"f1": ImageAsset(id="f1", kind="figure", path=figure, caption="Figure 1")},
            layout=PORTRAIT,
            session=None,   # 没有 Chrome 也应能出图（走 SVG 光栅化）
        )
        assert out is not None and out.exists(), "论文原图那张卡没渲染出来"
        assert pymupdf.Pixmap(str(out)).width == PORTRAIT.image_box_w

    def test_missing_asset_is_a_no_op(self, tmp_path):
        from app.services.video import _diagram_fallback_card

        shot = DiagramShot(kind="summary", title="x", fallback_image="不存在")
        scene = Scene(
            start=0.0, end=6.0, image=tmp_path / "base.png", kind="illustration",
            text="一句话。", diagram=shot,
        )
        assert _diagram_fallback_card(scene, {}, layout=PORTRAIT, session=None) is None


class _RepairingLLM:
    """第一次回一份**核验不过**的草案，第二次回一份能过的 —— 用来量「带原因再问一次」。"""

    mock = False

    def __init__(self, first, second):
        self.first, self.second = first, second
        self.calls: list[list[dict]] = []

    def _chat_json(self, messages, *, max_tokens: int, temperature: float = 0.6):
        self.calls.append(messages)
        return self.first if len(self.calls) == 1 else self.second


class TestRejectedDraftsAreRepairedWithTheRealReason:
    """核验不过的镜头要**带着机器的具体原因再问一次**，而不是整条丢掉。

    参考做法（Fibona agent）里最硬的一条：修复的输入是真实错误。
    我们这边的错误本来就有原文 —— 「指标「999兆」查不到依据」这种句子直接喂回去，
    模型才知道自己哪个数字不在材料里（原来它只知道「没过」）。
    """

    @staticmethod
    def _evidence():
        return ["6 比特下状态内存压缩了 5 倍多，Qwen 从 144 兆降到 28 兆左右。"]

    @staticmethod
    def _good_metric():
        # 标题与指标名都是**纯中文**，所以要凑够 2 个二字片段命中：
        # 「状态内存压缩」→ 状态 / 内存 / 压缩 三个都在这句话里（见 `diagram._supported`）
        return {
            "segment": 1, "kind": "metric", "title": "状态内存压缩", "source": "第 1 段",
            "metrics": [{"value": "144", "unit": "兆", "label": "状态内存", "source": "第 1 段"}],
        }

    @staticmethod
    def _bad_metric():
        return {
            "segment": 1, "kind": "metric", "title": "状态内存压缩", "source": "第 1 段",
            "metrics": [{"value": "999", "unit": "兆", "label": "状态内存", "source": "第 1 段"}],
        }

    @staticmethod
    def _bad_flow(segment=0):
        return {
            "segment": segment, "kind": "flow", "title": "凭空的两步流程", "source": "第 0 段",
            "nodes": [
                {"id": "a", "label": "编造的节点", "source": "第 0 段"},
                {"id": "b", "label": "另一个编造", "source": "第 0 段"},
            ],
        }

    def _run(self, first, second, evidence=None):
        from app.services.video import assign_diagram_shots

        llm = _RepairingLLM(first, second)
        report: dict = {}
        shots = assign_diagram_shots(
            segments=_segments(), timings=_timings(),
            evidence=evidence if evidence is not None else self._evidence(),
            llm=llm, report=report,
        )
        return shots, report, llm

    def test_a_rejected_shot_is_retried_with_the_reason_in_the_prompt(self):
        shots, report, llm = self._run(
            {"shots": [self._bad_metric()]}, {"shots": [self._good_metric()]}
        )
        assert len(llm.calls) == 2, "第一次不过就该再问一次"
        prompt = llm.calls[1][1]["content"]
        assert "999" in prompt, "修复提示里要有被拒的原话"
        assert "查不到依据" in prompt, "要有机器给的具体原因"
        assert report["repair"]["called"] is True
        assert report["repair"]["fixed"] == [1]
        assert 1 in shots, "救回来的镜头要真的用上"

    def test_the_repair_prompt_does_not_rewrite_what_already_passed(self):
        first = {"shots": [self._good_metric(), self._bad_flow()]}
        shots, report, llm = self._run(first, {"shots": []})
        assert report["repair"]["attempted"] == [0], "只重提被作废的那一条"
        assert "已经通过核验的镜头不要重写" in llm.calls[1][1]["content"]
        assert 1 in shots, "通过的那条不受修复影响"

    def test_no_rejected_shot_means_no_second_call(self):
        shots, report, llm = self._run({"shots": [self._good_metric()]}, {"shots": []})
        assert len(llm.calls) == 1, "没有作废的镜头就不该多花一次调用"
        assert report["repair"]["called"] is False
        assert report["kept"] == [1]

    def test_a_shot_that_loses_one_item_is_kept_and_recorded(self):
        """只删掉一项、镜头还能立住的：照用，但**台账里要留下删了什么、为什么**。"""
        partial = {
            "segment": 1, "kind": "flow", "title": "状态内存压缩", "source": "第 1 段",
            "nodes": [
                {"id": "a", "label": "状态内存压缩", "source": "第 1 段"},
                {"id": "b", "label": "Qwen 模型", "source": "第 1 段"},
                {"id": "c", "label": "编造的第三块", "source": "第 1 段"},
            ],
        }
        shots, report, _ = self._run({"shots": [partial]}, {"shots": []})
        assert 1 in shots
        assert report["partial"], report
        assert "编造的第三块" in str(report["partial"]), report["partial"]



def _round(index: int, start: float, end: float) -> Any:
    from app.services.podcast_tts import RoundTiming

    return RoundTiming(index=index, speaker="A", start=start, end=end)


def _plain_png(path: Path, w: int = 400, h: int = 300) -> Path:
    """一张纯色 PNG（要真文件：图片卡那一层会读它算落点）。"""
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, (30, 60, 100))
    pix.save(str(path))
    return path


class TestDiagramRowsStayAlignedWithScenes:
    """图解状态是**按段号**取的，所以「一段一行」必须永远成立。

    实测踩过的那个 bug（用户报的是「第二个画面有问题，而且又会跳回到封面」）：
    `_render_scene_layers` 只在**有图解**和**片尾**的段上 `append`，
    普通段不 append —— 于是 `diagram_states` 比段数短，编码侧按段号取就**整体错位**：
    这一集本来是第 11 段的图解，被叠到了**第 1 段**上。观众看到的是
    「封面被一张图解盖住，下一段又回到封面」。
    """

    @staticmethod
    def _shot(title: str) -> DiagramShot:
        return DiagramShot(
            kind="metric", title=title,
            title_source="脚本原句：6 比特下状态内存压缩了 5 倍多",
            metrics=(DiagramMetric("状态内存压缩", 5.0, " 倍",
                                   source="脚本原句：6 比特下状态内存压缩了 5 倍多"),),
        )

    def _render(self, tmp_path, scenes):
        from app.services.video import _render_scene_layers

        work = tmp_path / "layers"
        work.mkdir()
        return _render_scene_layers(scenes, PORTRAIT, work, assets={}, pauses=None)

    def test_a_scene_without_a_diagram_still_occupies_a_row(self, tmp_path):
        base = tmp_path / "base.png"
        import pymupdf

        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 90), False)
        pix.set_rect(pix.irect, (250, 250, 250))
        pix.save(str(base))
        shot = self._shot("状态内存压了多少")
        scenes = [
            Scene(start=0.0, end=4.0, image=base, kind="illustration", text="一句话。"),
            Scene(start=4.0, end=8.0, image=base, kind="illustration", text="两句话。", diagram=shot),
            Scene(start=8.0, end=12.0, image=base, kind="illustration", text="三句话。"),
            Scene(start=12.0, end=14.0, image=base, kind="illustration", text="收尾。", brand="outro"),
        ]
        layers = self._render(tmp_path, scenes)
        assert len(layers.diagram_states) == len(scenes), (
            f"图解状态行数（{len(layers.diagram_states)}）必须等于段数（{len(scenes)}）——"
            "少一行就会整体错位（实测把第 11 段的图解叠到了第 1 段）"
        )
        assert layers.diagram_states[1], "第 2 段自己有图解，那一行不能是空的"
        assert layers.diagram_states[0] == [] and layers.diagram_states[2] == []
        assert layers.diagram_states[3] == [], "片尾品牌卡不叠图解，但也得占一行"

    def test_the_encoder_refuses_to_guess_when_the_rows_do_not_match(self, caplog, tmp_path):
        """对不上时必须**不叠**并报警，而不是照样按段号取（那正是错位的成因）。"""
        import logging

        from app.services.video_encode import encode_video

        frame = tmp_path / "f.png"
        import pymupdf

        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 60), False)
        pix.set_rect(pix.irect, (250, 250, 250))
        pix.save(str(frame))
        audio = tmp_path / "a.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             "anullsrc=r=24000:cl=mono", "-t", "2", str(audio)],
            check=True,
        )
        scenes = [
            Scene(start=0.0, end=1.0, image=frame, kind="figure", text="一。"),
            Scene(start=1.0, end=2.0, image=frame, kind="figure", text="二。"),
        ]
        diagram = tmp_path / "d.png"
        pix2 = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 60), False)
        pix2.set_rect(pix2.irect, (255, 255, 255))
        pix2.save(str(diagram))
        with caplog.at_level(logging.WARNING):
            encode_video(
                scenes, [frame, frame], audio, tmp_path / "out.mp4",
                diagram_states=[[(diagram, 0.2)]],   # 只有一行，段数却是 2
            )
        assert any("对不上" in record.getMessage() for record in caplog.records), caplog.text


class _RouteLLM:
    """按 prompt 内容分派的假模型：图解那条返回一份能过核验的草案，其余一律空。"""

    mock = False

    def __init__(self):
        self.calls: list[list[dict]] = []

    def _chat_json(self, messages, *, max_tokens: int, temperature: float = 0.6):
        self.calls.append(messages)
        blob = " ".join(str(m.get("content") or "") for m in messages)
        if "动态图解镜头" not in blob:
            return {}
        return {
            "shots": [
                {
                    "segment": 0, "kind": "metric", "title": "状态内存压缩",
                    "title_source": "脚本原句：状态内存压缩了 5 倍多",
                    "metrics": [
                        {"label": "状态内存压缩", "value": 5, "unit": " 倍",
                         "source": "脚本原句：状态内存压缩了 5 倍多"}
                    ],
                }
            ]
        }


class TestTheShotSurvivesARebuild:
    """图解镜头要**存进 `video.scenes[i].diagram`**，重新合成时读回来复用。

    原来这里漏了：`build_scenes` 会读 `preset_scenes[i]["diagram"]`，
    但写 `result.scenes` 的那段**没有把图解写进去** —— 于是每次「重新合成」都**静默丢掉图解**。
    少的不是几张图，而是「重新合成不该换画面」这条保证（用户会觉得画面自己变了）。
    """

    def test_a_composed_video_writes_the_shot_into_its_scene_presets(self, tmp_path):
        import subprocess
        import wave

        from app.services.video import compose_video

        pool_dir = tmp_path / "assets"
        pool_dir.mkdir(exist_ok=True)
        cover = _plain_png(pool_dir / "cover.png")
        illu = _plain_png(pool_dir / "illu.png")
        audio = tmp_path / "a.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 4)

        result = compose_video(
            segments=[{"speaker": "A", "text": "状态内存压缩了 5 倍多。"}],
            timings=[_round(0, 0.0, 4.0)],
            audio_path=audio,
            audio_duration=4.0,
            cover_path=str(cover),
            figures=[],
            illustration_png=str(illu),
            work_dir=tmp_path / "work",
            output_path=tmp_path / "out.mp4",
            title="t",
            llm=_RouteLLM(),
            allow_point_llm=True,
        )
        entry = result.scenes[0]
        assert entry.get("diagram"), (
            "图解镜头没有被写进 `video.scenes[i].diagram` —— 重新合成会静默丢掉它"
        )
        # 读回来必须是同一个镜头（`build_scenes` 走的就是这条读回来的路）
        from app.services.video_core import diagram_shot_from_dict

        assert diagram_shot_from_dict(entry["diagram"]).kind == "metric"


class TestATruncatedDraftIsRetriedWithTheReason:
    """草案被截断（输出不是合法 JSON）时要**带着这个真实错误**再问一次。

    实测踩过：一集 41 段、模型一口气提 6 个带节点/连线/指标的镜头，`max_tokens=1600`
    把 JSON 从中间截断 → `parse_json_response` 报「不是合法 JSON」→ 兜底 `except`
    直接返回 `{}` → **整集一个图解都没有**，而日志里只有一条 warning。
    """

    class _TruncatingLLM:
        mock = False

        def __init__(self, good):
            self.good = good
            self.calls: list[list[dict]] = []

        def _chat_json(self, messages, *, max_tokens: int, temperature: float = 0.6):
            self.calls.append(messages)
            if len(self.calls) == 1:
                raise LLMError("模型输出不是合法 JSON：{ \"shots\": [ { \"segment\": 3,")
            return self.good

    def test_the_second_call_says_the_output_was_truncated(self):
        from app.services.llm_parse import LLMError  # noqa: F401  (本测试要用它构造错误)
        from app.services.video import assign_diagram_shots

        good = {
            "shots": [
                {
                    "segment": 1, "kind": "metric", "title": "状态内存压缩",
                    "title_source": "脚本原句：状态内存压缩了 5 倍多",
                    "metrics": [{"label": "状态内存压缩", "value": 5, "unit": " 倍",
                                 "source": "脚本原句：状态内存压缩了 5 倍多"}],
                }
            ]
        }
        llm = self._TruncatingLLM(good)
        shots = assign_diagram_shots(
            segments=_segments(), timings=_timings(),
            evidence=["状态内存压缩了 5 倍多。"], llm=llm,
        )
        assert len(llm.calls) == 2, "截断后应当再问一次"
        assert "被截断" in llm.calls[1][1]["content"]
        assert "只提最重要的 3 个镜头" in llm.calls[1][1]["content"]
        assert 1 in shots, "第二次拿到的草案要用上"

    def test_it_gives_up_after_the_retry(self):
        """第二次还是不行就退回原画面 —— 不许无限重试。"""
        from app.services.video import assign_diagram_shots

        llm = self._TruncatingLLM({"shots": []})
        llm._chat_json = lambda messages, *, max_tokens, temperature=0.6: (_ for _ in ()).throw(
            LLMError("还是坏的")
        )
        report: dict = {}
        assert assign_diagram_shots(
            segments=_segments(), timings=_timings(), evidence=["状态内存压缩了 5 倍多。"],
            llm=llm, report=report,
        ) == {}
