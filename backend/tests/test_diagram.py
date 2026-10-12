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
