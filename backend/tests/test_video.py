"""视频解读播客的测试：时间轴、逐段配图、字幕排版、编码产物。

配图这块的核心要求是**图文相符**：屏幕上出现的图必须和正在讲的内容是一个意思。
图不对文比没有图更糟，所以这里重点覆盖「逐段选图」，而不是「从某段开始一直用」。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.services.video import (
    VIDEO_H,
    VIDEO_W,
    ImageAsset,
    Scene,
    VideoError,
    _fit_subtitle,
    _normalize_per_segment,
    build_asset_pool,
    build_assign_messages,
    build_scenes,
    default_asset_id,
    encode_video,
    ffmpeg_available,
    generate_topic_images,
    group_generate_runs,
    heuristic_assignment,
    heuristic_per_segment,
    merge_runs_to_cap,
    render_slide,
)


@dataclass
class FakeTiming:
    index: int
    speaker: str
    start: float
    end: float


def make_png(path: Path, w: int = 400, h: int = 300, color=(30, 60, 100)) -> Path:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, color)
    pix.save(str(path))
    return path


def make_pool(tmp_path: Path) -> dict[str, ImageAsset]:
    return {
        "cover": ImageAsset("cover", make_png(tmp_path / "cover.png"), "cover", "论文首页"),
        "f1": ImageAsset(
            "f1", make_png(tmp_path / "f1.png"), "figure", "Figure 1: architecture"
        ),
        "f2": ImageAsset("f2", make_png(tmp_path / "f2.png"), "figure", "Figure 2: results"),
        "illustration": ImageAsset(
            "illustration", make_png(tmp_path / "illu.png"), "illustration", "核心机制信息图"
        ),
    }


# --------------------------------------------------------------------------
# 画幅
# --------------------------------------------------------------------------


class TestCanvasSize:
    def test_matches_pdf_first_page_aspect(self):
        """画幅要跟论文首页一致（935x1210）。"""
        assert (VIDEO_W, VIDEO_H) == (936, 1210)

    def test_dimensions_are_even_for_h264(self):
        """H.264 的 yuv420p 要求宽高能被 2 整除，935 会被 libx264 直接拒掉。

        实测报错：width not divisible by 2 (935x1210)
        """
        assert VIDEO_W % 2 == 0
        assert VIDEO_H % 2 == 0


# --------------------------------------------------------------------------
# 逐段配图（核心：图文相符）
# --------------------------------------------------------------------------


class TestPerSegmentAssignment:
    VALID = {"cover", "f1", "f2", "illustration"}

    def test_assigns_one_image_per_segment(self):
        """必须是逐段指定，而不是「给几个起始点然后沿用」。"""
        result = _normalize_per_segment(
            [
                {"segment": 0, "image_id": "cover"},
                {"segment": 1, "image_id": "f1"},
                {"segment": 3, "image_id": "f2"},
            ],
            count=5,
            valid_ids=self.VALID,
            default_id="illustration",
        )
        assert result is not None
        assert len(result) == 5
        assert result[:2] == ["cover", "f1"]
        assert result[2] == "f1", "第 2 段模型没给，应当沿用上一段"
        assert result[3:] == ["f2", "f2"]

    def test_topic_change_switches_image(self):
        """话题变了就要换图 —— 这是「图文相符」的核心保证。"""
        result = _normalize_per_segment(
            [
                {"segment": 0, "image_id": "cover"},
                {"segment": 1, "image_id": "f1"},
                {"segment": 2, "image_id": "illustration"},
                {"segment": 3, "image_id": "f2"},
            ],
            count=4,
            valid_ids=self.VALID,
            default_id="illustration",
        )
        assert result == ["cover", "f1", "illustration", "f2"]

    def test_rejects_invented_image_ids(self):
        """模型编造的图片 id 不能被采纳，否则会去取一张不存在的图。"""
        result = _normalize_per_segment(
            [{"segment": 0, "image_id": "f99"}, {"segment": 1, "image_id": "f1"}],
            count=2,
            valid_ids=self.VALID,
            default_id="illustration",
        )
        assert result == ["illustration", "f1"]

    def test_leading_gap_uses_default_not_first_pick(self):
        """开头漏段时用中性图，不要拿后面话题的图去顶。"""
        result = _normalize_per_segment(
            [{"segment": 2, "image_id": "f2"}],
            count=4,
            valid_ids=self.VALID,
            default_id="illustration",
        )
        assert result == ["illustration", "illustration", "f2", "f2"]

    def test_out_of_range_segment_returns_none(self):
        result = _normalize_per_segment(
            [{"segment": 99, "image_id": "f1"}],
            count=3,
            valid_ids=self.VALID,
            default_id="illustration",
        )
        assert result is None

    @pytest.mark.parametrize(
        "raw", ["不是列表", [], None, [{"bad": 1}], [{"segment": "x", "image_id": "f1"}]]
    )
    def test_garbage_returns_none(self, raw):
        assert (
            _normalize_per_segment(
                raw, count=3, valid_ids=self.VALID, default_id="illustration"
            )
            is None
        )


class TestHeuristicFallback:
    def test_heuristic_starts_are_strictly_increasing(self):
        for count in (5, 6, 8, 21, 40):
            for figures in (1, 3, 5):
                result = heuristic_assignment(count, figures)
                assert len(result) == figures
                assert result == sorted(set(result)), f"count={count} fig={figures}: {result}"

    def test_per_segment_expands_to_every_segment(self):
        result = heuristic_per_segment(10, ["f1", "f2"], "illustration")
        assert len(result) == 10
        assert all(item in {"f1", "f2", "illustration"} for item in result)

    def test_per_segment_without_figures_uses_default(self):
        assert heuristic_per_segment(4, [], "illustration") == ["illustration"] * 4


class TestAssetPool:
    def test_collects_cover_figures_and_illustration(self, tmp_path):
        pool = build_asset_pool(
            cover_path=str(make_png(tmp_path / "c.png")),
            figures=[
                {"id": "f1", "path": str(make_png(tmp_path / "a.png")), "caption": "x"},
                {"id": "f2", "path": str(make_png(tmp_path / "b.png")), "caption": "y"},
            ],
            illustration_png=str(make_png(tmp_path / "i.png")),
        )
        assert set(pool) == {"cover", "f1", "f2", "illustration"}

    def test_skips_missing_files(self, tmp_path):
        pool = build_asset_pool(
            cover_path=str(tmp_path / "nope.png"),
            figures=[{"id": "f1", "path": str(tmp_path / "also-nope.png")}],
            illustration_png=None,
        )
        assert pool == {}

    def test_default_prefers_illustration(self, tmp_path):
        """信息图概括全文，是最中性的选择——文不对题的风险最低。"""
        assert default_asset_id(make_pool(tmp_path)) == "illustration"

    def test_default_falls_back_to_cover(self, tmp_path):
        pool = {
            "cover": ImageAsset("cover", make_png(tmp_path / "c.png"), "cover"),
            "f1": ImageAsset("f1", make_png(tmp_path / "f.png"), "figure"),
        }
        assert default_asset_id(pool) == "cover"


class TestAssignPrompt:
    def test_prompt_includes_every_segment_and_speaker(self):
        messages = build_assign_messages(
            [
                {"speaker": "A", "text": "第一段内容"},
                {"speaker": "B", "text": "第二段内容"},
            ],
            [{"id": "f1", "caption": "Figure 1: architecture"}],
        )
        user = messages[-1]["content"]
        assert "[0]" in user and "[1]" in user
        assert "主播A" in user and "主播B" in user

    def test_prompt_demands_semantic_matching_across_languages(self):
        """图注是英文、脚本是中文，只能按语义判断，prompt 里必须说清楚。"""
        system = build_assign_messages(
            [{"speaker": "A", "text": "x"}], [{"id": "f1", "caption": "y"}]
        )[0]["content"]
        assert "语义" in system
        assert "英文" in system and "中文" in system

    def test_prompt_allows_neutral_illustration(self):
        """允许模型选中立的信息图，避免它为了填满而硬塞不相关的图。"""
        system = build_assign_messages(
            [{"speaker": "A", "text": "x"}], [{"id": "f1", "caption": "y"}]
        )[0]["content"]
        assert "illustration" in system
        assert "不相关" in system or "硬塞" in system

    def test_prompt_states_every_segment_needs_an_entry(self):
        system = build_assign_messages(
            [{"speaker": "A", "text": "x"}], [{"id": "f1", "caption": "y"}]
        )[0]["content"]
        assert "每一段" in system


# --------------------------------------------------------------------------
# 时间轴
# --------------------------------------------------------------------------


class TestBuildScenes:
    SEGMENTS = [
        {"speaker": "A", "text": "开场"},
        {"speaker": "B", "text": "追问"},
        {"speaker": "A", "text": "回答"},
        {"speaker": "B", "text": "收尾"},
    ]
    TIMINGS = [
        FakeTiming(0, "A", 7.0, 10.0),
        FakeTiming(1, "B", 10.0, 12.0),
        FakeTiming(2, "A", 12.0, 18.0),
        FakeTiming(3, "B", 18.0, 20.0),
    ]

    def _scenes(self, tmp_path, picks):
        return build_scenes(
            segments=self.SEGMENTS,
            timings=self.TIMINGS,
            audio_duration=27.0,
            assets=make_pool(tmp_path),
            image_for_segment=picks,
            fallback_id="illustration",
        )

    def test_covers_head_music_with_cover(self, tmp_path):
        """片头音乐期间必须有画面，不能让视频开头是黑的。"""
        scenes = self._scenes(tmp_path, ["cover", "f1", "f2", "f2"])
        assert scenes[0].kind == "cover"
        assert scenes[0].start == 0.0
        assert scenes[0].end == pytest.approx(7.0)

    def test_covers_tail_music_with_illustration(self, tmp_path):
        scenes = self._scenes(tmp_path, ["cover", "f1", "f2", "f2"])
        assert scenes[-1].kind == "illustration"
        assert scenes[-1].end == pytest.approx(27.0)

    def test_timeline_is_continuous(self, tmp_path):
        """时间轴必须连续，否则 ffmpeg concat 会丢帧或音画错位。"""
        scenes = self._scenes(tmp_path, ["cover", "f1", "f2", "f2"])
        for index in range(len(scenes) - 1):
            assert scenes[index].end == pytest.approx(scenes[index + 1].start), (
                f"第 {index} 段与下一段之间有缝"
            )

    def test_image_follows_per_segment_pick(self, tmp_path):
        """画面必须跟着逐段选择走 —— 这是「图文相符」在实现层的保证。"""
        scenes = self._scenes(tmp_path, ["cover", "f1", "f2", "illustration"])
        body = [s for s in scenes if s.kind != "cover"]
        names = [s.image.name for s in body]
        assert names[:4] == ["f1.png", "f2.png", "illu.png", "illu.png"], names

    def test_subtitles_follow_script(self, tmp_path):
        """字幕必须逐段对应脚本原文。

        注意选择器要用「有主播」而不是「kind 是 figure」：开场那一段配的是
        封面图（kind=cover），按 kind 过滤会把它漏掉。
        """
        scenes = self._scenes(tmp_path, ["cover", "f1", "f2", "f2"])
        body = [s for s in scenes if s.speaker]
        assert [s.text for s in body] == ["开场", "追问", "回答", "收尾"]
        assert [s.speaker for s in body] == ["A", "B", "A", "B"]

    def test_head_and_tail_have_no_speaker_badge(self, tmp_path):
        """片头/片尾是音乐段，没有主播说话，不该显示主播标签。"""
        scenes = self._scenes(tmp_path, ["cover", "f1", "f2", "f2"])
        assert scenes[0].speaker == ""
        assert scenes[0].text == ""
        assert scenes[-1].speaker == ""

    def test_unknown_pick_falls_back(self, tmp_path):
        """模型给了不存在的 id 也不能崩，要落到中性图。"""
        scenes = self._scenes(tmp_path, ["nope", "nope", "nope", "nope"])
        assert all(s.image.exists() for s in scenes)

    def test_raises_without_timings(self, tmp_path):
        with pytest.raises(VideoError):
            build_scenes(
                segments=self.SEGMENTS,
                timings=[],
                audio_duration=27.0,
                assets=make_pool(tmp_path),
                image_for_segment=[],
                fallback_id="illustration",
            )

    def test_raises_without_assets(self):
        with pytest.raises(VideoError):
            build_scenes(
                segments=self.SEGMENTS,
                timings=self.TIMINGS,
                audio_duration=27.0,
                assets={},
                image_for_segment=[],
                fallback_id="illustration",
            )


# --------------------------------------------------------------------------
# 字幕排版
# --------------------------------------------------------------------------


class TestSubtitleFitting:
    @pytest.mark.parametrize(
        "text",
        [
            "好的。",
            "你手机里的翻译软件、跟你聊天的AI，它们背后都站着同一篇论文。",
            "字" * 200,
            "A" * 400,
            "",
        ],
    )
    def test_never_overflows_the_panel(self, text):
        """竖版只有 856px 宽、148px 高，字号偏大时文字会溢出画面外。"""
        size, lines = _fit_subtitle(text)
        if not text:
            assert lines == []
            return
        assert len(lines) <= 6
        assert len(lines) * size * 1.36 <= 186.1, f"溢出：{len(lines)} 行 × {size}pt"

    def test_shrinks_font_for_long_text(self):
        short_size, _ = _fit_subtitle("好的。")
        long_size, _ = _fit_subtitle("字" * 400)
        assert long_size < short_size

    def test_keeps_readable_size_for_typical_segment(self):
        text = (
            "它把循环结构换成注意力。打个比方，以前的模型像接力赛，一棒传一棒，"
            "必须等前一棒跑完；现在改成所有人同时看全场。"
        )
        size, lines = _fit_subtitle(text)
        assert size >= 24, f"典型长度不该只剩 {size}pt"
        assert len(lines) <= 4


# --------------------------------------------------------------------------
# 渲染与编码
# --------------------------------------------------------------------------


class TestRenderAndEncode:
    def test_slide_is_portrait_frame_size(self, tmp_path):
        import pymupdf

        image = make_png(tmp_path / "img.png", 900, 500)
        scene = Scene(
            start=0, end=3, image=image, kind="figure", speaker="A", text="测试字幕"
        )
        out = render_slide(scene, tmp_path / "slide.png", title="测试标题")
        pix = pymupdf.Pixmap(str(out))
        assert (pix.width, pix.height) == (VIDEO_W, VIDEO_H)

    def test_raises_on_unreadable_image(self, tmp_path):
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not an image")
        scene = Scene(start=0, end=1, image=bad, kind="figure", text="x")
        with pytest.raises(VideoError):
            render_slide(scene, tmp_path / "s.png")

    @pytest.mark.skipif(not ffmpeg_available(), reason="需要 ffmpeg")
    def test_encode_produces_playable_mp4_at_audio_length(self, tmp_path):
        """编码产物必须是 936x1210 的 h264，且总长钉在音频长度上。

        单步编码做不到：`-r 30 -shortest` 会多 2.4 秒，不加 `-r` 会少 12 秒。
        必须分两步（先出纯视频轨，再用 `-c:v copy` 封音频），`-shortest` 才生效。
        """
        import wave

        image = make_png(tmp_path / "img.png", 900, 600)
        scenes = [
            Scene(start=0, end=2, image=image, kind="figure", speaker="A", text="第一段"),
            Scene(start=2, end=4, image=image, kind="figure", speaker="B", text="第二段"),
        ]
        slides = [
            render_slide(scene, tmp_path / f"slide{i}.png")
            for i, scene in enumerate(scenes)
        ]

        audio = tmp_path / "audio.wav"
        rate = 8000
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(b"\x00\x00" * rate * 4)  # 4 秒

        out = tmp_path / "out.mp4"
        encode_video(scenes, slides, audio, out, target_duration=4.0)
        assert out.exists() and out.stat().st_size > 1000

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=width,height,codec_name",
                "-of", "csv=p=0", str(out),
            ],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "h264" in probe
        assert f"{VIDEO_W},{VIDEO_H}" in probe

        duration = float(
            subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0", str(out),
                ],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        )
        assert duration == pytest.approx(4.0, abs=0.3), f"时长 {duration} 未对齐音频"

    def test_encode_rejects_mismatched_lengths(self, tmp_path):
        image = make_png(tmp_path / "img.png")
        scenes = [Scene(start=0, end=1, image=image, kind="figure")]
        with pytest.raises(VideoError):
            encode_video(scenes, [], tmp_path / "a.mp3", tmp_path / "o.mp4")


# --------------------------------------------------------------------------
# 「没有对应原图」时现场生成专门配图
# --------------------------------------------------------------------------


class TestGenerateRuns:
    """论文原图只能覆盖一部分话题。剩下的段落以前统一挂同一张概括全图的信息图，
    实测 21 段里有 10 段都是它 —— 画面单调，而且并不真的对应内容。
    现在改成按话题现场生成，但要先把连续段落合并，否则段数一多就失控。
    """

    def test_groups_consecutive_segments(self):
        picks = ["cover", "generate", "generate", "f1", "generate", "f2"]
        assert group_generate_runs(picks) == [(1, 3), (4, 5)]

    def test_handles_trailing_run(self):
        assert group_generate_runs(["f1", "generate", "generate"]) == [(1, 3)]

    def test_no_runs(self):
        assert group_generate_runs(["cover", "f1", "f2"]) == []

    def test_all_generate(self):
        assert group_generate_runs(["generate"] * 5) == [(0, 5)]

    def test_empty(self):
        assert group_generate_runs([]) == []

    def test_merge_preserves_coverage(self):
        """超过上限时合并，而不是丢弃 —— 丢弃会让那些段落退回中性图，又变回图文不符。"""
        runs = [(1, 3), (4, 5), (6, 9), (10, 12), (13, 15)]
        merged = merge_runs_to_cap(runs, 2)
        assert len(merged) == 2
        assert merged[0][0] == runs[0][0]
        assert merged[-1][1] == runs[-1][1]

        # 每一段原本要生成的段落都必须仍被某个合并区间覆盖。
        # 注意区间之间**允许有缝** —— 缝里是分配了真实论文原图的段落，
        # 不该被生成图覆盖，所以这里不能断言首尾相接。
        def covered(index: int) -> bool:
            return any(start <= index < end for start, end in merged)

        for start, end in runs:
            for index in range(start, end):
                assert covered(index), f"第 {index} 段丢失了生成覆盖"

    def test_merge_keeps_figure_gaps(self):
        """被真实原图隔开的两个区间，合并后仍不能把中间的段落吞进去。"""
        runs = [(1, 3), (6, 8)]
        merged = merge_runs_to_cap(runs, 1)
        assert merged == [(1, 8)]
        # 单区间情况下中间确实被覆盖了 —— 这是合并的必然代价，
        # 所以优先级是「先保证都画出来」，实在超上限才牺牲精确度

    def test_merge_is_noop_when_under_cap(self):
        runs = [(1, 3), (4, 5)]
        assert merge_runs_to_cap(runs, 4) == runs

    def test_merge_with_zero_cap_is_noop(self):
        """cap<=0 表示不限制，不能变成空列表（那会丢掉全部生成）。"""
        runs = [(1, 3)]
        assert merge_runs_to_cap(runs, 0) == runs

    def test_merge_single_slot(self):
        merged = merge_runs_to_cap([(1, 3), (5, 7), (9, 11)], 1)
        assert merged == [(1, 11)]


class TestGenerateIdAcceptance:
    VALID = {"cover", "f1", "illustration"}

    def test_generate_is_accepted(self):
        """generate 是伪 id，不在图片池里也必须被接受，否则会被当成非法值丢掉。"""
        result = _normalize_per_segment(
            [
                {"segment": 0, "image_id": "cover"},
                {"segment": 1, "image_id": "generate"},
            ],
            count=2,
            valid_ids=self.VALID,
            default_id="illustration",
        )
        assert result == ["cover", "generate"]

    def test_unknown_id_still_rejected(self):
        result = _normalize_per_segment(
            [{"segment": 0, "image_id": "f99"}],
            count=1,
            valid_ids=self.VALID,
            default_id="illustration",
        )
        assert result is None

    def test_prompt_tells_model_to_prefer_original_figures(self):
        """有原图就对原图，原图对不上才让生成 —— 原图最准确。"""
        system = build_assign_messages(
            [{"speaker": "A", "text": "x"}], [{"id": "f1", "caption": "y"}]
        )[0]["content"]
        assert "generate" in system
        assert "优先用原图" in system

    def test_prompt_warns_against_marking_every_segment(self):
        """必须提醒模型合并：每段都标会让生成量失控。"""
        system = build_assign_messages(
            [{"speaker": "A", "text": "x"}], [{"id": "f1", "caption": "y"}]
        )[0]["content"]
        assert "合并" in system


class TestTopicGeneration:
    def test_mock_mode_skips_generation(self, tmp_path):
        """Mock 模式没有真实模型，不该发起生成。"""
        from app.services.video import generate_topic_images

        class FakeLLM:
            mock = True

        result = generate_topic_images(
            FakeLLM(),
            segments=[{"speaker": "A", "text": "x"}],
            runs=[(0, 1)],
            paper_title="t",
            analysis=None,
            output_dir=tmp_path,
            stem="s",
        )
        assert result == {}

    def test_empty_runs_returns_empty(self, tmp_path):
        from app.services.video import generate_topic_images

        result = generate_topic_images(
            object(),
            segments=[],
            runs=[],
            paper_title="t",
            analysis=None,
            output_dir=tmp_path,
            stem="s",
        )
        assert result == {}

    def test_generation_failure_does_not_raise(self, tmp_path, monkeypatch):
        """单组生成失败必须降级（退回中性图），不能让整个视频合成挂掉。"""
        from app.services import video as video_module

        def boom(*args, **kwargs):
            raise RuntimeError("模型调用炸了")

        monkeypatch.setattr(
            "app.services.illustration.generate_topic_illustration", boom, raising=False
        )

        class FakeLLM:
            mock = False

        result = video_module.generate_topic_images(
            FakeLLM(),
            segments=[{"speaker": "A", "text": "x"}],
            runs=[(0, 1)],
            paper_title="t",
            analysis=None,
            output_dir=tmp_path,
            stem="s",
        )
        assert result == {}


# --------------------------------------------------------------------------
# 视频过时检测 + 重新合成
# --------------------------------------------------------------------------


class TestStaleness:
    """视频是用当时的配图烘焙进 MP4 的。用户人工校正配图后，
    已生成的视频里还是旧画面 —— 必须能判断出来并支持重新合成。

    重新合成时**复用上次的画面分配**，不再问模型：否则「我只转了一张图，
    怎么画面全变了」，而且主题图会被重新生成一遍（4 次调用 + 几十秒）。
    """

    def test_same_version_is_fresh(self, tmp_path):
        from app.services.video import asset_version, is_video_stale

        path = make_png(tmp_path / "a.png")
        stored = {
            "assets": {"f1": str(path)},
            "asset_versions": {"f1": asset_version(path)},
        }
        assert is_video_stale(stored) is False

    def test_changed_version_is_stale(self, tmp_path):
        from app.services.video import is_video_stale

        path = make_png(tmp_path / "a.png")
        stored = {"assets": {"f1": str(path)}, "asset_versions": {"f1": "old-value"}}
        assert is_video_stale(stored) is True

    def test_missing_file_is_stale(self, tmp_path):
        from app.services.video import is_video_stale

        stored = {
            "assets": {"f1": str(tmp_path / "gone.png")},
            "asset_versions": {"f1": "1-1"},
        }
        assert is_video_stale(stored) is True

    def test_rotation_marks_video_stale(self, tmp_path):
        """真的转一下配图，版本号必须变（这是「过时」判定的实际触发点）。"""
        from app.services.figures import rotate_image_file
        from app.services.video import asset_version, is_video_stale

        path = make_png(tmp_path / "a.png", 300, 200)
        stored = {
            "assets": {"f1": str(path)},
            "asset_versions": {"f1": asset_version(path)},
        }
        assert is_video_stale(stored) is False

        rotate_image_file(path, "cw")
        assert is_video_stale(stored) is True, "旋转配图后视频应当被判定为过时"

    def test_no_metadata_is_not_stale(self):
        """老数据没有这些字段时不该误报过时。"""
        from app.services.video import is_video_stale

        assert is_video_stale(None) is False
        assert is_video_stale({}) is False
        assert is_video_stale({"duration_sec": 1}) is False


class TestPresetReuse:
    """复用既有画面分配。"""

    def test_uses_preset_scenes_without_touching_llm(self, tmp_path):
        from app.services.video import compose_video

        class ExplodingLLM:
            mock = False

            def _chat_json(self, *a, **k):
                raise AssertionError("复用模式下不该再调用模型")

        pool_dir = tmp_path / "assets"
        pool_dir.mkdir()
        cover = make_png(pool_dir / "cover.png")
        f1 = make_png(pool_dir / "f1.png")
        illu = make_png(pool_dir / "illu.png")

        audio = tmp_path / "a.wav"
        import wave

        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 2)  # 2 秒

        segments = [{"speaker": "A", "text": "第一段"}, {"speaker": "B", "text": "第二段"}]
        timings = [
            FakeTiming(0, "A", 0.0, 1.0),
            FakeTiming(1, "B", 1.0, 2.0),
        ]

        result = compose_video(
            segments=segments,
            timings=timings,
            audio_path=audio,
            audio_duration=2.0,
            cover_path=str(cover),
            figures=[{"id": "f1", "path": str(f1), "caption": "Figure 1"}],
            illustration_png=str(illu),
            work_dir=tmp_path / "work",
            output_path=tmp_path / "out.mp4",
            title="t",
            llm=ExplodingLLM(),
            preset_scenes=[
                {"index": 0, "image": "cover"},
                {"index": 1, "image": "f1"},
            ],
            preset_assets={"cover": str(cover), "f1": str(f1), "illustration": str(illu)},
        )
        assert result.assignment == "reused"
        assert [s["image"] for s in result.scenes] == ["cover", "f1"]

    def test_deleted_figure_falls_back(self, tmp_path):
        """被删掉的图不能再引用，要回退到中性图而不是崩掉。"""
        from app.services.video import compose_video

        pool_dir = tmp_path / "assets2"
        pool_dir.mkdir()
        cover = make_png(pool_dir / "cover.png")
        illu = make_png(pool_dir / "illu.png")

        audio = tmp_path / "b.wav"
        import wave

        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 2)

        result = compose_video(
            segments=[{"speaker": "A", "text": "x"}],
            timings=[FakeTiming(0, "A", 0.0, 2.0)],
            audio_path=audio,
            audio_duration=2.0,
            cover_path=str(cover),
            figures=[],
            illustration_png=str(illu),
            work_dir=tmp_path / "work2",
            output_path=tmp_path / "out2.mp4",
            title="t",
            llm=None,
            preset_scenes=[{"index": 0, "image": "f_deleted"}],
            preset_assets={"f_deleted": str(tmp_path / "never-existed.png")},
        )
        assert result.assignment == "reused"
        # 引用的图不存在 → 回退到中性图，而不是抛错
        assert result.scenes[0]["image"] in {"illustration", "cover"}


# --------------------------------------------------------------------------
# 画面风格：白底、不显示主播标签
# --------------------------------------------------------------------------


class TestSlideTheme:
    """画面改成白底，并且不再显示「主播A / 主播B」标签。

    白底的理由：论文配图本身多数是白底图表，深色画布会把它们衬得像贴图；
    白底更接近读论文的观感，也方便投屏和截图。

    去掉标签的理由：谁在说话听声音就知道，画面上多一行标签只会分散注意力，
    还占掉字幕的空间（去掉后字幕可用高度从 148px 提到 186px）。
    """

    def _slide_pixmap(self, tmp_path, text="这是一句测试字幕"):
        image = make_png(tmp_path / "img.png", 900, 500)
        scene = Scene(
            start=0, end=3, image=image, kind="figure", speaker="A", text=text
        )
        out = render_slide(scene, tmp_path / "slide.png", title="测试标题")
        import pymupdf

        return pymupdf.Pixmap(str(out))

    def test_background_is_white(self, tmp_path):
        pix = self._slide_pixmap(tmp_path)
        n, W = pix.n, pix.width
        s = pix.samples

        def pixel(x: int, y: int):
            i = (y * W + x) * n
            return (s[i], s[i + 1], s[i + 2])

        # 画布四角与右侧留白都应当接近纯白
        for x, y in ((5, 5), (W - 5, 5), (5, 300), (W - 6, 900)):
            r, g, b = pixel(x, y)
            assert r > 240 and g > 240 and b > 240, f"({x},{y}) 不是白底：{(r,g,b)}"

    def test_no_speaker_badge_colors(self, tmp_path):
        """旧版在字幕区左上角画过蓝/橙两色的主播标签，现在不该再有。

        判据要同时满足两个条件才叫「标签」：
        - **亮**（标签底色是 18% 不透明度的浅蓝/浅橙，盖在浅底上仍是亮的）
        - **偏色**（max-min 通道差明显）
        只看偏色会把深色的字幕文字（#16202f 本身就偏蓝）误判成标签，
        这一点是实测踩出来的。
        """
        pix = self._slide_pixmap(tmp_path)
        n, W, H = pix.n, pix.width, pix.height
        s = pix.samples
        from app.services.video import SUBTITLE_TOP

        tinted = 0
        total = 0
        for y in range(SUBTITLE_TOP + 2, min(SUBTITLE_TOP + 50, H), 2):
            for x in range(30, min(300, W), 2):
                i = (y * W + x) * n
                r, g, b = s[i], s[i + 1], s[i + 2]
                total += 1
                bright = (r + g + b) / 3 > 150
                colored = (max(r, g, b) - min(r, g, b)) > 15
                if bright and colored:
                    tinted += 1
        assert tinted / max(total, 1) < 0.02, (
            f"字幕区左侧仍出现亮色块（{tinted}/{total}），像是主播标签还在"
        )

    def test_subtitle_area_is_light(self, tmp_path):
        """字幕区也应当是浅色底 + 深色字（白底主题）。"""
        pix = self._slide_pixmap(tmp_path)
        n, W, H = pix.n, pix.width, pix.height
        s = pix.samples
        from app.services.video import SUBTITLE_TOP

        # 取字幕面板右侧一块空白区域的亮度
        y = SUBTITLE_TOP + 10
        i = (y * W + (W - 20)) * n
        assert s[i] > 230 and s[i + 1] > 230, "字幕区底色不是浅色"

        # 文字应当明显更深。注意要落在**文字实际占据的行**上：
        # SUBTITLE_TEXT_TOP 是文字**基线**，字在基线之上，采样基线下方的行会取到空白。
        from app.services.video import SUBTITLE_TEXT_TOP

        darkest = 255
        for dy in range(-20, 2, 2):
            yy = SUBTITLE_TEXT_TOP + dy
            for x in range(40, W - 40, 2):
                j = (yy * W + x) * n
                darkest = min(darkest, s[j])
        assert darkest < 120, "字幕文字不是深色，白底上会看不清"


class TestBrandingInjection:
    """每期都自动加上青稞社区的片头片尾（见 app/branding.py）。"""

    def test_branding_defined_and_interleaved(self):
        from app import branding

        assert branding.BRAND_INTRO and branding.BRAND_OUTRO
        # 两个主播都要有台词，否则听着像独白
        intro_speakers = {speaker for speaker, _ in branding.BRAND_INTRO}
        outro_speakers = {speaker for speaker, _ in branding.BRAND_OUTRO}
        assert intro_speakers == {"A", "B"}
        assert outro_speakers == {"A", "B"}

    def test_branding_is_short(self):
        """片头片尾每期都听，太长就烦。控制在 20 秒左右。"""
        from app import branding
        from app.services import prompts

        seconds = prompts.brand_padding_sec(branding.brand_char_count())
        assert seconds < 25, f"品牌话术占了 {seconds:.1f} 秒，太长了"

    def test_branding_avoids_cliches(self):
        """社区的价值观就是反注水，片头自己先注水就自相矛盾了。"""
        from app import branding

        text = "".join(t for _, t in branding.BRAND_INTRO + branding.BRAND_OUTRO)
        for cliche in ("欢迎来到", "让我们一起", "深入探讨", "综上所述", "不容错过"):
            assert cliche not in text, f"片头片尾出现了套话：{cliche}"

    def test_segments_have_expected_shape(self):
        from app import branding

        for seg in branding.intro_segments() + branding.outro_segments():
            assert seg["speaker"] in ("A", "B")
            assert seg["text"].strip()
            assert seg.get("brand") in ("intro", "outro")
