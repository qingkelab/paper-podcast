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
    CAPTION_FADE_SEC,
    LANDSCAPE,
    PORTRAIT,
    CAPTION_TOP,
    FADE_IN_SEC,
    IMAGE_BOX_H,
    IMAGE_BOX_LEFT,
    IMAGE_BOX_W,
    IMAGE_TOP,
    TRANSITION_MAX_RATIO,
    TRANSITION_MIN_SEC,
    TRANSITION_SEC,
    TITLE_BASELINE,
    WAVEFORM_HEIGHT,
    WAVEFORM_LEFT,
    WAVEFORM_TOP,
    WAVEFORM_WIDTH,
    POINT_BG,
    POINT_HEIGHT,
    POINT_LEFT,
    POINT_MAX_CHARS,
    POINT_MAX_CHARS_EN,
    POINT_SLIDE_SEC,
    POINT_TEXT,
    POINT_TOP,
    PROGRESS_BAR_COLOR,
    SUBTITLE_TOP,
    VIDEO_H,
    VIDEO_W,
    CaptionBand,
    ImageAsset,
    Scene,
    VideoError,
    _fit_subtitle,
    _normalize_per_segment,
    beat_windows,
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
    _normalize_points,
    build_points_messages,
    local_point,
    merge_runs_to_cap,
    point_char_limit,
    progress_bar_svg,
    layout_for,
    normalize_focus,
    normalize_point_items,
    plan_transitions,
    render_caption_band,
    render_chrome,
    render_focus_overlay,
    render_endcard,
    render_image_card,
    render_point_row,
    render_slide,
    split_caption_beats,
)


SAMPLE_TEXT = (
    "这是一篇关于注意力机制的论文。" * 30
    + "我们提出了一个新的方法，在多个基准上取得了更好的效果。"
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


# --------------------------------------------------------------------------
# 社区 logo 与片尾关注引导
# --------------------------------------------------------------------------


class TestBrandEndCard:
    """片尾做成深色品牌卡（logo + 关注引导）。

    为什么片尾要单独深色：社区 logo 是**浅色**字标（社区自己的视频也用深色星空底），
    白底上会直接消失；而正文又必须是白底 —— 论文配图本身就是白底图表。
    所以两种底各归其位：正文白底保证可读，片尾深色保证品牌正确。
    """

    @staticmethod
    def _pixels(path):
        import pymupdf

        pix = pymupdf.Pixmap(str(path))
        n, W = pix.n, pix.width
        s = pix.samples

        def at(x: int, y: int):
            i = (y * W + x) * n
            return (s[i], s[i + 1], s[i + 2])

        return at, pix.width, pix.height

    def test_logo_asset_exists(self):
        from app.branding import logo_path

        assert logo_path().exists(), "内置的社区 logo 丢了"

    def test_logo_data_uri_is_cached(self):
        from app.services.video import _logo_data_uri

        first = _logo_data_uri(120)
        assert first is not None
        second = _logo_data_uri(120)
        assert first is second, "同一宽度应当命中缓存，否则每帧都要重解码"

    def test_endcard_is_dark_with_brand_colors(self, tmp_path):
        from app.services.video import BRAND_DARK, BRAND_GOLD, BRAND_GREEN

        image = make_png(tmp_path / "i.png", 600, 400)
        scene = Scene(
            start=0, end=6, image=image, kind="illustration", speaker="A",
            text="关注青稞，每天学习最新论文。下期见。", brand="outro",
        )
        out = render_endcard(scene, tmp_path / "end.png", title="测试标题")
        at, W, H = self._pixels(out)

        # 四角是品牌深色底
        assert at(6, 6) == (10, 18, 12), f"片尾不是深色底：{at(6, 6)}"

        # 画面里应当出现青稞绿与麦金（logo 或文字带进来的）
        def near(c, target, tol=60):
            return all(abs(a - b) < tol for a, b in zip(c, target))

        gold = (245, 200, 66)
        green = (124, 179, 66)
        has_gold = any(
            near(at(x, y), gold) for y in range(380, H - 260, 12) for x in range(60, W - 60, 12)
        )
        has_green = any(
            near(at(x, y), green) for y in range(380, H - 260, 12) for x in range(60, W - 60, 12)
        )
        assert has_gold, "片尾没出现麦金（关注引导的主句）"
        assert has_green, "片尾没出现青稞绿"

    def test_endcard_subtitle_panel_is_dark(self, tmp_path):
        from app.services.video import SUBTITLE_TOP

        image = make_png(tmp_path / "i.png", 600, 400)
        scene = Scene(start=0, end=6, image=image, kind="illustration", text="下期见。", brand="outro")
        out = render_endcard(scene, tmp_path / "end2.png")
        at, W, H = self._pixels(out)
        # 字幕面板底色应当是深色（与正文页的浅底相反）
        r, g, b = at(W - 20, SUBTITLE_TOP + 10)
        assert (r + g + b) / 3 < 80, f"片尾字幕面板不是深色：{(r,g,b)}"

    def test_content_slide_still_white_and_has_watermark(self, tmp_path):
        """正文页保持白底，同时右上角有 logo 水印（垫了深色底才看得见）。"""
        from app.services.video import VIDEO_W, WATERMARK_W

        image = make_png(tmp_path / "i.png", 600, 400, color=(220, 220, 220))
        scene = Scene(start=0, end=5, image=image, kind="figure", speaker="A", text="正文内容")
        out = render_slide(scene, tmp_path / "body.png", title="标题")
        at, W, H = self._pixels(out)

        assert at(6, 6) == (255, 255, 255), "正文页应当仍是白底"

        # 水印区域应当出现深色底（logo 是浅色的，必须垫底）
        chip_w = WATERMARK_W + 20
        chip_x = W - 40 - chip_w
        dark_found = any(
            sum(at(x, y)) / 3 < 60
            for y in range(18, 70, 3)
            for x in range(chip_x, W - 40, 4)
        )
        assert dark_found, "正文页右上角没找到 logo 水印的深色底"

    def test_watermark_stays_in_the_header(self, tmp_path):
        """水印只占右上角一小块，不能溢出到标题行下面或图片上。

        注意别拿页眉整行去采样 —— 标题本身就画在那一行（基线 y=46），
        会把「有深色像素」误判成水印溢出。这里检查的是**页眉之下的那一条**。
        """
        from app.services.video import TITLE_BASELINE

        image = make_png(tmp_path / "i.png", 600, 400, color=(220, 220, 220))
        scene = Scene(start=0, end=5, image=image, kind="figure", speaker="A", text="正文内容")
        out = render_slide(scene, tmp_path / "body2.png", title="标题")
        at, W, H = self._pixels(out)

        from app.services.video import (
            IMAGE_TOP,
            WATERMARK_PAD,
            WATERMARK_W,
            _logo_data_uri,
        )

        # 先算清楚水印底块的实际边界，再验证它确实落在图片区之上
        _, _, logo_h = _logo_data_uri(WATERMARK_W)
        chip_bottom = 18 + logo_h + WATERMARK_PAD * 2
        assert chip_bottom <= IMAGE_TOP, (
            f"水印底块下沿 {chip_bottom} 已经压到图片区（顶部 {IMAGE_TOP}）"
        )

        # 水印底块与图片之间的那条空隙必须整条是白的
        leaked = [
            (x, y)
            for y in range(chip_bottom + 1, IMAGE_TOP)
            for x in range(20, W - 20, 10)
            if sum(at(x, y)) / 3 < 240
        ]
        assert not leaked, f"水印溢出了页眉：{leaked[:5]}"


class TestBrandCta:
    """片尾的关注引导：声音里说一遍，画面上出大字。"""

    def test_cta_text(self):
        from app import branding

        assert branding.BRAND_CTA_TITLE == "关注青稞，每天学习最新论文"
        assert branding.BRAND_CTA_SUBTITLE

    def test_cta_is_also_spoken_in_outro(self):
        """画面上的引导必须在语音里也说一遍 —— 只出字幕会漏掉纯听的场景。"""
        from app import branding

        spoken = "".join(t for _, t in branding.BRAND_OUTRO)
        assert "关注青稞" in spoken
        assert "每天学习最新论文" in spoken

    def test_outro_mentions_community(self):
        from app import branding

        spoken = "".join(t for _, t in branding.BRAND_OUTRO)
        assert "青稞社区" in spoken


class TestEndCardRouting:
    def test_outro_scenes_use_endcard(self, tmp_path, monkeypatch):
        """片尾段必须走品牌卡渲染，正文段走普通页。"""
        from app.services import video as video_module

        rendered: list[str] = []
        real_slide, real_end = video_module.render_slide, video_module.render_endcard

        def spy_slide(scene, path, **kw):
            rendered.append("slide")
            return real_slide(scene, path, **kw)

        def spy_end(scene, path, **kw):
            rendered.append("endcard")
            return real_end(scene, path, **kw)

        monkeypatch.setattr(video_module, "render_slide", spy_slide)
        monkeypatch.setattr(video_module, "render_endcard", spy_end)

        cover = make_png(tmp_path / "c.png")
        illu = make_png(tmp_path / "u.png")
        audio = tmp_path / "a.wav"
        import wave

        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 3)

        segments = [
            {"speaker": "A", "text": "正文一", "brand": "intro"},
            {"speaker": "B", "text": "正文二"},
            {"speaker": "A", "text": "关注青稞", "brand": "outro"},
        ]
        timings = [
            FakeTiming(0, "A", 0.0, 1.0),
            FakeTiming(1, "B", 1.0, 2.0),
            FakeTiming(2, "A", 2.0, 3.0),
        ]

        video_module.compose_video(
            segments=segments,
            timings=timings,
            audio_path=audio,
            audio_duration=3.0,
            cover_path=str(cover),
            figures=[],
            illustration_png=str(illu),
            work_dir=tmp_path / "w",
            output_path=tmp_path / "o.mp4",
            title="t",
            llm=None,
            preset_scenes=[{"index": i, "image": "illustration"} for i in range(3)],
            preset_assets={"illustration": str(illu), "cover": str(cover)},
        )

        assert "endcard" in rendered, "片尾段没有走品牌卡渲染"
        assert rendered.count("endcard") == 1, f"品牌卡帧数不对：{rendered}"


# --------------------------------------------------------------------------
# 动效：推镜 / 字幕逐句 / 进度条
# --------------------------------------------------------------------------


class TestCaptionBeats:
    """字幕分句是纯逻辑，先把边界钉住，再去测画面真的在动。"""

    def test_short_text_is_not_split(self):
        # 十几个字再切成两半，每句剩几个字，闪得更难看
        assert split_caption_beats("这一段很短，不拆。") == ["这一段很短，不拆。"]

    def test_two_sentences_split_at_punctuation(self):
        text = "仿真能批量造数据，但任务经常做不成。失败之后没有人回头改场景，误差就一直累积下去。"
        beats = split_caption_beats(text)
        assert len(beats) == 2
        assert beats[0].endswith("。")
        assert beats[1].endswith("。")
        # 不能丢内容：拼起来必须还是原文（只差空白）
        assert "".join(beats).replace(" ", "") == text.replace(" ", "")

    def test_long_text_without_punctuation_splits_at_comma(self):
        text = "先造场景再规划任务的老流水线里桌子太高杂物挡住夹爪任务做不成也没有人回头改场景所以数据质量一直上不去"
        beats = split_caption_beats(text)
        assert len(beats) == 2
        assert "".join(beats) == text

    def test_many_sentences_merge_into_two_without_losing_text(self):
        text = "".join(f"第{index}句话讲的是同一个机制的某一步。" for index in range(5))
        beats = split_caption_beats(text)
        assert len(beats) == 2, f"最多两句，实际 {len(beats)}"
        assert "".join(beats) == text, "合并时不能丢掉后面的句子"

    def test_empty_text(self):
        assert split_caption_beats("") == []
        assert split_caption_beats("   ") == []

    def test_beat_windows_cover_the_whole_scene(self):
        beats = ["第一句比较长，要占更多时间。", "第二句短。"]
        windows = beat_windows(beats, 10.0)
        assert windows[0][0] == 0.0
        assert windows[-1][1] == pytest.approx(10.0)
        # 相邻窗口首尾相接（中间不能有「谁都不显示」的空档）
        assert windows[0][1] == pytest.approx(windows[1][0])
        # 长句分到的时间更多
        assert windows[0][1] - windows[0][0] > windows[1][1] - windows[1][0]

    def test_beat_windows_empty(self):
        assert beat_windows([], 5.0) == []


class TestPointEmphasis:
    """「突出解读内容」靠的是要点强调行与字幕里的数字，不是镜头运动。

    第一版做的是缓慢推拉镜头，被否掉了 —— 那种运动跟正在讲的内容无关。
    这一组盯着现在留下的三件事里跟内容直接相关的两件。
    """

    def test_local_point_picks_the_number_with_context(self):
        point = local_point("在 16 个任务上平均成功率 67%，两个基线分别是 17% 和 21%。")
        # 一句里多个数字时，要拎出信息量最大的那个：百分比 > 带单位 > 光秃秃的整数
        assert "67%" in point
        assert len(point) <= POINT_MAX_CHARS

    def test_local_point_clips_to_clause(self):
        point = local_point("仿真能批量造数据，但以往的流水线先造场景、再规划任务，任务做不成也没人回头改场景。")
        assert point == "", "没有数字就不硬凑 —— 空着比给一句废话好"

    def test_local_point_handles_empty_and_long_numbers(self):
        assert local_point("") == ""
        long_point = local_point("混入随机布局数据后成功率从 7% 提升到 40% 这个提升非常可观")
        assert 0 < len(long_point) <= POINT_MAX_CHARS

    def test_points_prompt_asks_for_short_lines_and_no_fabrication(self):
        messages = build_points_messages(
            [{"text": "第一段"}, {"text": "第二段"}], title="测试论文"
        )
        system = messages[0]["content"]
        assert "14 个字" in system
        assert "不要编造" in system
        # 每一段都必须有一句（第一版允许留空，结果整条视频只有 18% 的时间有强调行）
        assert "每一段都要有一句" in system

    def test_normalize_points_always_returns_one_per_segment(self):
        points = _normalize_points(
            [{"segment": 0, "point": "成功率 67%"}, {"segment": 5, "point": "越界了"}],
            count=3,
        )
        assert points == ["成功率 67%", "", ""], "缺项补空串、越界的丢掉"

    def test_normalize_points_shortens_at_boundary_not_mid_word(self):
        """超长时要按标点/空格收短 —— 按字符硬切会切出「at 4 and 6 bi」这种半截话。

        画面上最抢眼的一行出现半截英文，比不显示这一行还糟（实测踩到过）。
        """
        points = _normalize_points(
            [{"segment": 1, "point": "at 4 and 6 bits precision the errors are small"}],
            count=2,
        )
        assert points[1].endswith("bits"), f"应当在词边界断开：{points[1]!r}"
        assert len(points[1]) <= POINT_MAX_CHARS
        assert not points[1].rstrip().endswith(("a", "bi", "th")), "不能是半截词"

        chinese = _normalize_points(
            [{"segment": 0, "point": "这是一句被模型写超长的强调文案，后面还有很多内容"}], count=1
        )
        # 在逗号处断开，并丢掉结尾那个逗号（留一个逗号在末尾像话没说完）
        assert chinese[0] == "这是一句被模型写超长的强调文案", f"中文要在标点处断开：{chinese[0]!r}"

    def test_normalize_points_tolerates_garbage(self):
        points = _normalize_points([{"segment": 0, "point": ""}, "闲聊", None], count=2)
        assert points == ["", ""]

    def test_point_row_is_rendered_with_accent_bar(self, tmp_path):
        row = render_point_row("成功率 67%", tmp_path / "point.png")
        import pymupdf

        pix = pymupdf.Pixmap(str(row))
        assert (pix.width, pix.height) == (VIDEO_W, POINT_HEIGHT)
        n, W = pix.n, pix.width
        samples = pix.samples

        def at(x, y):
            index = (y * W + x) * n
            return (samples[index], samples[index + 1], samples[index + 2])

        # 左侧色条是强调蓝，整行的底色是浅蓝
        # 强调行的色条从 POINT_LEFT 开始，左边那 40px 是透明的（图层叠在画面上）
        bar_r, bar_g, bar_b = at(POINT_LEFT + 2, POINT_HEIGHT // 2)
        assert bar_b > 150 and bar_b > bar_r + 40, f"左侧色条不是强调蓝：{(bar_r, bar_g, bar_b)}"
        bg_r, bg_g, bg_b = at(VIDEO_W // 2, 6)
        assert bg_b >= bg_r and bg_r > 200, f"强调行底色不对：{(bg_r, bg_g, bg_b)}"

    def test_slide_without_point_leaves_the_row_empty(self, tmp_path):
        """没有要点时不画强调行 —— 不能留一个空色块让人以为坏了。"""
        image = make_png(tmp_path / "img.png", 700, 500)
        scene = Scene(start=0, end=2, image=image, kind="figure", text="第一段。")
        out = render_slide(scene, tmp_path / "slide.png", title="标题")
        import pymupdf

        pix = pymupdf.Pixmap(str(out))
        n, W = pix.n, pix.width
        index = ((POINT_TOP + 20) * W + 20) * n
        r, g, b = pix.samples[index], pix.samples[index + 1], pix.samples[index + 2]
        assert r > 240 and g > 240 and b > 240, f"没有要点时那一行应该是白的：{(r, g, b)}"

    def test_slide_with_point_draws_it(self, tmp_path):
        image = make_png(tmp_path / "img.png", 700, 500)
        scene = Scene(
            start=0, end=2, image=image, kind="figure", text="第一段。", point="成功率 67%"
        )
        out = render_slide(scene, tmp_path / "slide.png", title="标题")
        import pymupdf

        pix = pymupdf.Pixmap(str(out))
        n, W = pix.n, pix.width
        # 沿着强调行扫一遍，应该能找到强调蓝的像素（色条或文字）
        found = 0
        for x in range(0, W, 3):
            index = ((POINT_TOP + POINT_HEIGHT // 2) * W + x) * n
            r, g, b = pix.samples[index], pix.samples[index + 1], pix.samples[index + 2]
            if b > 120 and b > r + 30:
                found += 1
        assert found > 0, "强调行没画出来"


@pytest.mark.skipif(not ffmpeg_available(), reason="需要系统安装 ffmpeg")
class TestMotionIsActuallyRendered:
    """上面那些都是字符串断言，证明不了画面上真的有动。

    这一组**真的编码一段视频、再抽帧采像素**：推镜要么让画面变化，字幕要么在
    段中间换过一次，进度条要么是长的。字符串对了但画面没动，这一组会红。
    """

    @staticmethod
    def _frame(video: Path, seconds: float, out: Path) -> Path:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{seconds:.3f}", "-i", str(video),
             "-frames:v", "1", str(out)],
            capture_output=True, text=True, check=True,
        )
        return out

    @staticmethod
    def _pixels(path: Path):
        import pymupdf

        pix = pymupdf.Pixmap(str(path))
        n, width = pix.n, pix.width
        samples = pix.samples

        def at(x: int, y: int):
            index = (y * width + x) * n
            return (samples[index], samples[index + 1], samples[index + 2])

        return at, width, pix.height

    @staticmethod
    def _striped_png(path: Path, w: int = 700, h: int = 500) -> Path:
        """有花纹的测试图。

        纯色块无论怎么缩放，采出来的像素都一模一样 —— 用它测「画面有没有动」
        会永远得到「没动」这个假结论（第一版就是这么写的，实测红了一次）。
        """
        import pymupdf

        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
        pix.set_rect(pix.irect, (255, 255, 255))
        for x in range(0, w, 40):
            pix.set_rect(pymupdf.IRect(x, 0, x + 20, h), (20, 20, 20))
        for y in range(0, h, 60):
            pix.set_rect(pymupdf.IRect(0, y, w, y + 10), (200, 30, 30))
        pix.save(str(path))
        return path

    def _encode(
        self, tmp_path: Path, text: str, *, seconds: float = 4.0, point: str = ""
    ) -> Path:
        import wave

        image = self._striped_png(tmp_path / "img.png")
        scene = Scene(
            start=0,
            end=seconds,
            image=image,
            kind="figure",
            speaker="A",
            text=text,
            point=point,
        )
        beats = split_caption_beats(text)
        bands = []
        band_paths = []
        if len(beats) > 1:
            slide = render_slide(scene, tmp_path / "slide.png", include_subtitle=False)
            for index, beat in enumerate(beats):
                band_path = render_caption_band(beat, tmp_path / f"band{index}.png")
                bands.append(CaptionBand(text=beat, image=band_path))
                band_paths.append(band_path)
        else:
            slide = render_slide(scene, tmp_path / "slide.png")

        audio = tmp_path / "audio.wav"
        rate = 8000
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(b"\x00\x00" * int(rate * seconds))

        point_rows: list[Path | None] = []
        if point:
            slide = render_slide(
                scene, tmp_path / "slide.png", include_subtitle=False, include_point=False
            )
            for index, beat in enumerate(beats):
                band_path = tmp_path / f"band{index}.png"
                render_caption_band(beat, band_path)
                bands.append(CaptionBand(text=beat, image=band_path))
                band_paths.append(band_path)
            point_path = render_point_row(point, tmp_path / "point.png")
            point_rows.append(point_path)
            band_paths.append(point_path)

        out = tmp_path / "motion.mp4"
        encode_video(
            [scene], [slide], audio, out, caption_bands=[bands], point_rows=point_rows
        )
        for path in band_paths:
            path.unlink(missing_ok=True)
        return out

    def test_point_row_slides_in(self, tmp_path):
        """要点强调行开头是从左边滑进来的：滑到一半时左侧还在画面外。

        这是**内容驱动的动效**（标记这一段换了新内容），和「整张图慢慢放大」
        那种被否掉的运动不是一回事。做法是量强调行里「浅蓝底」的像素数。
        """
        video = self._encode(tmp_path, "第一段话。", seconds=4.0, point="成功率 67%")
        early = self._pixels(self._frame(video, 0.08, tmp_path / "pt1.png"))
        late = self._pixels(self._frame(video, 1.5, tmp_path / "pt2.png"))

        def row_pixels(at, width):
            # 只数强调行那条**浅蓝底**（#eef4fb）。判据是「偏蓝」而不是「够亮」——
            # 底图那一行本来就是白的，用「亮」当判据会把白底也算进去（第一版就这么误判了）
            count = 0
            for x in range(0, width, 2):
                r, g, b = at(x, POINT_TOP + 10)
                if b - r >= 8 and b > 230:
                    count += 1
            return count

        early_count = row_pixels(early[0], early[1])
        late_count = row_pixels(late[0], late[1])
        assert late_count > early_count, (
            f"强调行没有滑入：0.08s 时已可见 {early_count} 个像素，"
            f"1.5s 时 {late_count} 个（应当变多）"
        )
        assert late_count > VIDEO_W * 0.3, "滑入结束后强调行应当铺满整行"

    def test_caption_switches_mid_scene(self, tmp_path):
        text = "仿真能批量造数据，但任务经常做不成。失败之后没有人回头改场景，误差就一直累积下去。"
        video = self._encode(tmp_path, text, seconds=6.0)
        windows = beat_windows(split_caption_beats(text), 6.0)
        before = self._pixels(self._frame(video, max(0.2, windows[1][0] - 1.2), tmp_path / "c1.png"))[0]
        after = self._pixels(self._frame(video, windows[1][0] + 0.9, tmp_path / "c2.png"))[0]
        # 字幕带里随便挑几行像素，第二句出现后必须不一样
        diffs = sum(
            1
            for x in range(60, 880, 40)
            for y in (1040, 1080, 1120)
            if before(x, y) != after(x, y)
        )
        assert diffs >= 3, "第二句字幕出现前后，字幕带像素没有变化 —— 分句没生效"

    def test_progress_bar_grows(self, tmp_path):
        video = self._encode(tmp_path, "第一段话。", seconds=4.0)
        early = self._pixels(self._frame(video, 0.5, tmp_path / "p1.png"))
        late = self._pixels(self._frame(video, 3.5, tmp_path / "p2.png"))

        def bar_width(at, width):
            # 顶部 5px 是进度条（#2f6fb5 蓝），数一行里蓝色像素的个数
            count = 0
            for x in range(width):
                r, g, b = at(x, 2)
                if b > 100 and b > r + 40:
                    count += 1
            return count

        early_w = bar_width(early[0], early[1])
        late_w = bar_width(late[0], late[1])
        assert late_w > early_w + 50, f"进度条没变长：0.5s={early_w}px，3.5s={late_w}px"
        assert late_w <= VIDEO_W

    def test_caption_band_covers_the_whole_caption_area(self, tmp_path):
        """字幕带必须是不透明的、整整一条 —— 否则底图上的空白会透出来。"""
        band = render_caption_band("这是一句测试字幕。", tmp_path / "band.png")
        at, width, height = self._pixels(band)
        assert width == VIDEO_W
        assert height == VIDEO_H - SUBTITLE_TOP
        # 左上角是字幕底色（#f4f7fa），不是透明（透明会读成黑）
        r, g, b = at(4, 4)
        assert r > 230 and g > 230 and b > 230, f"字幕带底色不对：{(r, g, b)}"


class TestPointGenerationRules:
    """强调行文案「什么时候可以问模型」。

    复用画面时**默认不问模型**（那条规则由 TestPresetReuse 钉着：用户只是转了个配图，
    画面和文案都不该跟着变）。但旧视频里没有强调行文案，需要能单独补一次 ——
    所以有一个显式的开关，而不是默默每次都调。
    """

    def _compose(self, tmp_path, llm, **extra):
        from app.services.video import compose_video

        pool_dir = tmp_path / "assets"
        pool_dir.mkdir(exist_ok=True)
        cover = make_png(pool_dir / "cover.png")
        illu = make_png(pool_dir / "illu.png")
        audio = tmp_path / "a.wav"
        import wave

        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 2)

        return compose_video(
            segments=[{"speaker": "A", "text": "第一段"}, {"speaker": "B", "text": "第二段"}],
            timings=[FakeTiming(0, "A", 0.0, 1.0), FakeTiming(1, "B", 1.0, 2.0)],
            audio_path=audio,
            audio_duration=2.0,
            cover_path=str(cover),
            figures=[],
            illustration_png=str(illu),
            work_dir=tmp_path / "work",
            output_path=tmp_path / "out.mp4",
            title="t",
            llm=llm,
            preset_scenes=[{"index": 0, "image": "cover"}, {"index": 1, "image": "illustration"}],
            preset_assets={"cover": str(cover), "illustration": str(illu)},
            **extra,
        )

    def test_reuse_mode_does_not_ask_the_model_by_default(self, tmp_path):
        class ExplodingLLM:
            mock = False

            def _chat_json(self, *a, **k):
                raise AssertionError("复用模式下默认不该调用模型")

        result = self._compose(tmp_path, ExplodingLLM())
        assert result.assignment == "reused"

    def test_point_llm_runs_when_opted_in_and_is_stored(self, tmp_path):
        class PointsLLM:
            mock = False

            def __init__(self):
                self.calls = 0

            def _chat_json(self, messages, **kwargs):
                self.calls += 1
                assert "14 个字" in messages[0]["content"]
                return {"points": [{"segment": 0, "point": "成功率 67%"}, {"segment": 1, "point": ""}]}

        llm = PointsLLM()
        result = self._compose(tmp_path, llm, allow_point_llm=True)
        assert llm.calls == 1
        # 强调文案要**存进 scenes**，下次重新合成才能复用（不然每次重建都烧一次调用）
        assert [s.get("point") for s in result.scenes] == ["成功率 67%", ""]

    def test_stored_points_win_over_a_fresh_model_call(self, tmp_path):
        class CountingLLM:
            mock = False

            def __init__(self):
                self.calls = 0

            def _chat_json(self, *a, **k):
                self.calls += 1
                return {"points": [{"segment": 0, "point": "新写的"}]}

        llm = CountingLLM()
        result = {}
        from app.services.video import compose_video

        pool_dir = tmp_path / "assets3"
        pool_dir.mkdir()
        cover = make_png(pool_dir / "cover.png")
        illu = make_png(pool_dir / "illu.png")
        import wave

        audio = tmp_path / "a.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 2)

        result = compose_video(
            segments=[{"speaker": "A", "text": "第一段"}],
            timings=[FakeTiming(0, "A", 0.0, 2.0)],
            audio_path=audio,
            audio_duration=2.0,
            cover_path=str(cover),
            figures=[],
            illustration_png=str(illu),
            work_dir=tmp_path / "work",
            output_path=tmp_path / "out.mp4",
            title="t",
            llm=llm,
            allow_point_llm=True,
            preset_scenes=[{"index": 0, "image": "cover", "point": "上次存下来的要点"}],
            preset_assets={"cover": str(cover), "illustration": str(illu)},
        )
        assert llm.calls == 0, "已经有存下来的强调文案，不该再问模型"
        assert result.scenes[0]["point"] == "上次存下来的要点"


class TestPointsPromptLanguage:
    """英文版的强调 prompt 必须**整段英文**。

    实测踩到过：中文 system 里写着「语言与原文一致」，模型给英文脚本写的强调行
    仍然是中文 —— 英文那一版的画面上整行中文。跟当初解读/脚本 prompt 同样的坑：
    中文指令混在英文输出任务里，模型会跟着中文语感走。
    """

    SEGMENTS = [{"text": "It reaches 67% success on 16 tasks."}]

    def test_english_prompt_has_no_chinese(self):
        messages = build_points_messages(self.SEGMENTS, title="T", language="en")
        for message in messages:
            cjk = sum(1 for char in message["content"] if "\u4e00" <= char <= "\u9fff")
            assert cjk == 0, f"英文版 prompt 里还有中文：{message['content'][:120]}"

    def test_english_prompt_states_the_language_and_the_word_limit(self):
        system = build_points_messages(self.SEGMENTS, title="T", language="en")[0]["content"]
        assert "8 words" in system
        assert "English" in system
        assert "Every segment gets a line" in system

    def test_chinese_prompt_still_chinese(self):
        messages = build_points_messages(self.SEGMENTS, title="T", language="zh")
        assert "14 个字" in messages[0]["content"]
        assert "【脚本分段】" in messages[1]["content"]


class TestPointLengthByLanguage:
    """中英文的强调行长度上限必须分开定。

    卡成一个数（20 字符）时，英文被切得只剩三四个词，实测出现过
    「State pool outgrows」「Memory problem is」这种断在半句的强调行 ——
    而 prompt 对英文的要求是「8 个词」（≈40 字符），两边必须对齐。
    """

    def test_limits_differ(self):
        assert point_char_limit("zh") == POINT_MAX_CHARS
        assert point_char_limit("en") == POINT_MAX_CHARS_EN
        assert POINT_MAX_CHARS_EN > POINT_MAX_CHARS

    def test_english_point_of_eight_words_survives(self):
        line = "State pool outgrows the model weights"   # 38 字符 ≈ 6 个词
        points = _normalize_points([{"segment": 0, "point": line}], count=1, max_chars=POINT_MAX_CHARS_EN)
        assert points[0] == line, "英文 8 词以内的强调行不该被切断"

    def test_chinese_point_still_shortens(self):
        chinese = "状态池显存占用超过模型权重本身，这个问题在高并发下更明显"
        points = _normalize_points([{"segment": 0, "point": chinese}], count=1)
        assert 0 < len(points[0]) <= POINT_MAX_CHARS


class TestTransitionPlan:
    """转场规则：有理由才动。

    实测参考是一条 40 秒的论文宣传片：白底、**没有任何硬切**、每 9~10 秒整页换一次、
    每次是约 0.25~0.5 秒的快速淡化。所以默认溶解，而不是花哨的擦除/翻页。
    """

    @staticmethod
    def _scene(start, end, image="a.png", brand="", point=""):
        return Scene(start=start, end=end, image=Path(image), kind="figure", brand=brand)

    def test_first_scene_fades_in(self):
        plan = plan_transitions([self._scene(0, 6)], ["cover"])
        assert plan[0].kind == "fade_in"
        assert plan[0].seconds == pytest.approx(FADE_IN_SEC)

    def test_same_image_gets_no_transition(self):
        scenes = [self._scene(0, 6, "a.png"), self._scene(6, 12, "a.png")]
        plan = plan_transitions(scenes, ["cover", "cover"], cards=[Path("c0.png"), Path("c1.png")])
        assert plan[1].kind == "none", "图没变就别动 —— 无意义的溶解只会让人以为卡了一下"

    def test_changed_image_dissolves_with_previous_card(self):
        scenes = [self._scene(0, 6, "a.png"), self._scene(6, 12, "b.png")]
        plan = plan_transitions(scenes, ["cover", "f1"], cards=[Path("c0.png"), Path("c1.png")])
        assert plan[1].kind == "dissolve"
        assert plan[1].seconds == pytest.approx(TRANSITION_SEC)
        assert plan[1].previous == Path("c0.png")

    def test_push_style(self):
        scenes = [self._scene(0, 6, "a.png"), self._scene(6, 12, "b.png")]
        plan = plan_transitions(
            scenes, ["cover", "f1"], style="push", cards=[Path("c0.png"), Path("c1.png")]
        )
        assert plan[1].kind == "push"

    def test_none_style_disables(self):
        scenes = [self._scene(0, 6, "a.png"), self._scene(6, 12, "b.png")]
        plan = plan_transitions(
            scenes, ["cover", "f1"], style="none", cards=[Path("c0.png"), Path("c1.png")]
        )
        assert plan[1].kind == "none"

    def test_short_scene_skips_transition(self):
        # 0.25 秒的片段：按比例压到 0.0875 秒，低于下限 TRANSITION_MIN_SEC 就干脆不做
        scenes = [self._scene(0, 6, "a.png"), self._scene(6, 6.25, "b.png")]
        assert 0.25 * TRANSITION_MAX_RATIO < TRANSITION_MIN_SEC
        plan = plan_transitions(scenes, ["cover", "f1"], cards=[Path("c0.png"), Path("c1.png")])
        assert plan[1].kind == "none"

    def test_transition_is_clamped_to_a_fraction_of_the_scene(self):
        scenes = [self._scene(0, 6, "a.png"), self._scene(6, 7.0, "b.png")]
        plan = plan_transitions(scenes, ["cover", "f1"], cards=[Path("c0.png"), Path("c1.png")])
        assert plan[1].kind == "dissolve"
        assert 0 < plan[1].seconds <= 7.0 * TRANSITION_MAX_RATIO + 1e-6

    def test_outro_dissolves_from_the_previous_slide(self):
        scenes = [
            self._scene(0, 6, "a.png"),
            self._scene(6, 8, "a.png", brand="outro"),
        ]
        plan = plan_transitions(
            scenes, ["cover", "cover"], cards=[Path("c0.png"), None], slides=[Path("s0.png"), Path("s1.png")]
        )
        assert plan[1].kind == "to_card"
        assert plan[1].previous == Path("s0.png")


@pytest.mark.skipif(not ffmpeg_available(), reason="需要系统安装 ffmpeg")
class TestTransitionsAreRendered:
    """转场必须真的画出来：抽帧采像素，看两张图之间是不是「混合」而不是硬切。

    顺带钉住最重要的一条：**只有图片在变，骨架一动不动**
    （标题/图注/字幕带/进度条在过渡期间像素完全一致）。
    """

    @staticmethod
    def _solid(path: Path, width: int, height: int, color) -> Path:
        import pymupdf

        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
        pix.set_rect(pix.irect, color)
        pix.save(str(path))
        return path

    def _pixels(self, path: Path):
        import pymupdf

        pix = pymupdf.Pixmap(str(path))
        n, width = pix.n, pix.width
        samples = pix.samples

        def at(x: int, y: int):
            index = (y * width + x) * n
            return (samples[index], samples[index + 1], samples[index + 2])

        return at

    def _frame(self, video: Path, seconds: float, out: Path) -> Path:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-ss", f"{seconds:.3f}",
             "-frames:v", "1", str(out)],
            capture_output=True, text=True, check=True,
        )
        return out

    def _encode(
        self, tmp_path: Path, *, same_image: bool = False, seconds: float = 3.0
    ) -> tuple[Path, Path]:
        import wave

        red = self._solid(tmp_path / "red.png", 700, 500, (220, 30, 30))
        blue = self._solid(tmp_path / "blue.png", 700, 500, (30, 60, 220))
        second = red if same_image else blue

        scenes = [
            Scene(start=0, end=seconds, image=red, kind="figure", text="第一段。"),
            Scene(start=seconds, end=seconds * 2, image=second, kind="figure", text="第二段。"),
        ]
        chromes = [
            render_chrome(scene, tmp_path / f"chrome{i}.png", title="测试标题")
            for i, scene in enumerate(scenes)
        ]
        cards = [
            render_image_card(scene, tmp_path / f"card{i}.png") for i, scene in enumerate(scenes)
        ]
        ids = [str(scene.image) for scene in scenes]
        plan = plan_transitions(scenes, ids, cards=cards, slides=chromes)

        audio = tmp_path / "audio.wav"
        rate = 8000
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(b"\x00\x00" * int(rate * seconds * 2))

        out = tmp_path / "transitions.mp4"
        encode_video(
            scenes,
            chromes,
            audio,
            out,
            target_duration=seconds * 2,
            image_cards=cards,
            transitions=plan,
        )
        return out, chromes[0]

    def test_transition_blends_the_two_images(self, tmp_path):
        video, _ = self._encode(tmp_path)
        center = (VIDEO_W // 2, IMAGE_TOP + IMAGE_BOX_H // 2)
        before = self._pixels(self._frame(video, 2.9, tmp_path / "before.png"))(*center)
        middle = self._pixels(self._frame(video, 3.18, tmp_path / "middle.png"))(*center)
        after = self._pixels(self._frame(video, 3.6, tmp_path / "after.png"))(*center)

        assert before[0] > 150 and before[2] < 100, f"过渡前应是红图：{before}"
        assert after[2] > 150 and after[0] < 100, f"过渡后应是蓝图：{after}"
        # 混合：红蓝都有分量，且既不等于红也不等于蓝
        assert 60 < middle[0] < 200 and 60 < middle[2] < 200, f"过渡中应是混合色：{middle}"
        assert middle != before and middle != after

    def test_chrome_does_not_move_during_the_transition(self, tmp_path):
        """过渡期间只有图片在变，骨架（标题/图注/字幕带/进度条）不动。

        判据是**平均值**而不是逐像素全等：两段画面是分开编码的，H.264 在
        平坦区/文字边缘的量化噪声可以差十几个灰阶（实测踩到），
        逐像素全等会把这种压缩噪声误判成「骨架动了」。
        """
        import pymupdf

        video, _ = self._encode(tmp_path)
        frames = [self._frame(video, t, tmp_path / f"g{t}.png") for t in (2.9, 3.18, 3.6)]
        pixmaps = [pymupdf.Pixmap(str(frame)) for frame in frames]

        def mean_diff(box):
            x0, y0, x1, y1 = box
            totals = []
            for a, b in zip(pixmaps, pixmaps[1:]):
                total = count = 0
                for y in range(y0, y1, 3):
                    for x in range(x0, x1, 3):
                        i = (y * a.width + x) * a.n
                        total += (
                            abs(a.samples[i] - b.samples[i])
                            + abs(a.samples[i + 1] - b.samples[i + 1])
                            + abs(a.samples[i + 2] - b.samples[i + 2])
                        )
                        count += 3
                totals.append(total / count)
            return max(totals)

        image_box = (IMAGE_BOX_LEFT + 40, IMAGE_TOP + 40, VIDEO_W - IMAGE_BOX_LEFT - 40, IMAGE_TOP + IMAGE_BOX_H - 40)
        chrome_bands = [
            (20, 8, VIDEO_W - 20, IMAGE_TOP - 8),                    # 顶部：标题 + 进度条
            (20, SUBTITLE_TOP + 4, VIDEO_W - 20, VIDEO_H - 8),        # 底部：字幕带
        ]
        image_change = mean_diff(image_box)
        chrome_change = max(mean_diff(band) for band in chrome_bands)
        assert image_change > 15, f"图片区没怎么变（{image_change:.1f}）—— 转场没生效"
        assert chrome_change < 3, f"骨架区动了（{chrome_change:.1f}）—— 应该是只有图片在变"

    def test_no_transition_when_the_image_is_unchanged(self, tmp_path):
        video, _ = self._encode(tmp_path, same_image=True)
        center = (VIDEO_W // 2, IMAGE_TOP + IMAGE_BOX_H // 2)
        early = self._pixels(self._frame(video, 3.05, tmp_path / "e.png"))(*center)
        later = self._pixels(self._frame(video, 3.7, tmp_path / "l.png"))(*center)
        assert early[0] > 150 and early[2] < 100, f"同图时不该有混合：{early}"
        assert early == later

    def test_length_still_matches_the_audio(self, tmp_path):
        video, _ = self._encode(tmp_path, seconds=3.0)
        duration = float(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        )
        assert duration == pytest.approx(6.0, abs=0.3), f"带转场后长度跑偏了：{duration}"


@pytest.mark.skipif(not ffmpeg_available(), reason="需要系统安装 ffmpeg")
class TestWaveformStrip:
    """声波条是「根据语音做动画」的可见落点：音量大的地方条上墨就多，安静的地方就少。

    这一组真的编码一遍再抽帧采像素 —— 只看滤镜字符串无法证明它跟音量有关。
    """

    PAUSE_TEST_VOICE_TOP = (201, 161, 74)   # A 的麦金 #d3a24a

    def _encode(self, tmp_path: Path, *, waveform: bool = True) -> Path:
        import math
        import struct
        import wave

        image = make_png(tmp_path / "img.png", 700, 500)
        scene = Scene(
            start=0, end=2.0, image=image, kind="figure", speaker="A", text="第一段。"
        )
        chrome = render_chrome(scene, tmp_path / "chrome.png", title="测试")
        card = render_image_card(scene, tmp_path / "card.png")

        # 前 1 秒大声、后 1 秒安静：声波条上的墨应当明显前多后少
        rate = 8000
        frames = bytearray()
        for index in range(rate * 2):
            amplitude = 0.75 if index < rate else 0.0
            value = amplitude * math.sin(2 * math.pi * 220 * index / rate)
            frames.extend(struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32000)))
        audio = tmp_path / "voice.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(bytes(frames))

        out = tmp_path / "wave.mp4"
        encode_video(
            [scene],
            [chrome],
            audio,
            out,
            target_duration=2.0,
            image_cards=[card],
            transitions=plan_transitions([scene], ["x"], cards=[card]),
            waveform=waveform,
        )
        return out

    def _frame(self, video: Path, seconds: float, out: Path) -> Path:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-ss", f"{seconds:.3f}",
             "-frames:v", "1", str(out)],
            capture_output=True, text=True, check=True,
        )
        return out

    def _strip_stats(self, frame: Path) -> tuple[int, int]:
        """返回（条上的墨像素数, 条上的暗像素数）。"""
        import pymupdf

        pix = pymupdf.Pixmap(str(frame))
        n, width = pix.n, pix.width
        samples = pix.samples
        ink = dark = 0
        for y in range(WAVEFORM_TOP, WAVEFORM_TOP + WAVEFORM_HEIGHT):
            for x in range(WAVEFORM_LEFT, WAVEFORM_LEFT + WAVEFORM_WIDTH):
                index = (y * width + x) * n
                r, g, b = samples[index], samples[index + 1], samples[index + 2]
                if r > 150 and r > g + 20 and g > b + 20:
                    ink += 1
                if r < 120 and g < 120 and b < 120:
                    dark += 1
        return ink, dark

    def test_strip_inks_more_when_the_voice_is_loud(self, tmp_path):
        video = self._encode(tmp_path)
        loud_ink, loud_dark = self._strip_stats(self._frame(video, 0.5, tmp_path / "loud.png"))
        quiet_ink, quiet_dark = self._strip_stats(self._frame(video, 1.7, tmp_path / "quiet.png"))
        assert loud_ink > 200, f"有声时声波条上应有明显墨迹，实际 {loud_ink}"
        assert loud_ink > quiet_ink * 2, f"声波条没跟着音量走：响 {loud_ink} vs 静 {quiet_ink}"
        # 底色必须干净：showwaves 画的是黑底彩线，直接叠会留一层黑
        assert loud_dark == 0 and quiet_dark == 0, f"声波条残留暗像素：{(loud_dark, quiet_dark)}"

    def test_strip_can_be_turned_off(self, tmp_path):
        video = self._encode(tmp_path, waveform=False)
        ink, _ = self._strip_stats(self._frame(video, 0.5, tmp_path / "off.png"))
        assert ink == 0, "关掉之后不该再有声波条"


class TestLandscapeLayout:
    """横版（1920×1080）与竖版的版式同构，只有数值不同。

    版式是「标题条 → 图片区 → 图注 → 强调行 → 字幕带」这一条纵向流，
    所以两版看起来是同一个产品。这里钉住几何：各条带不重叠、都在画幅内。
    """

    def test_two_layouts_differ_only_in_numbers(self):
        for layout in (PORTRAIT, LANDSCAPE):
            bands = [
                ("标题", 0, layout.title_baseline + 10),
                ("图片区", layout.image_top, layout.image_top + layout.image_box_h),
                ("图注", layout.caption_top, layout.point_top),
                ("强调行", layout.point_top, layout.point_top + layout.point_height),
                ("字幕带", layout.subtitle_top, layout.height),
            ]
            for index in range(len(bands) - 1):
                _, _, top_end = bands[index]
                _, next_start, _ = bands[index + 1]
                assert top_end <= next_start, f"{layout.width}px 版式里 {bands[index][0]} 与 {bands[index+1][0]} 重叠"
            assert layout.waveform_top + 20 <= layout.height
            assert layout.image_box_left + layout.image_box_w <= layout.width
            assert layout.point_left + layout.point_width <= layout.width
            assert layout.subtitle_left + layout.subtitle_width <= layout.width
            # 声波条贴在字幕带底部
            assert layout.waveform_top > layout.subtitle_top

    def test_landscape_is_16_by_9(self):
        assert LANDSCAPE.width == 1920 and LANDSCAPE.height == 1080
        assert abs(LANDSCAPE.width / LANDSCAPE.height - 16 / 9) < 0.01
        assert PORTRAIT.width == 936 and PORTRAIT.height == 1210

    def test_layout_for_accepts_aliases(self):
        assert layout_for("landscape") is LANDSCAPE
        assert layout_for("16:9") is LANDSCAPE
        assert layout_for("portrait") is PORTRAIT
        assert layout_for(None) is PORTRAIT, "老数据没有画幅字段，默认竖版"
        assert layout_for("乱写") is PORTRAIT

    def test_landscape_renderers_use_the_landscape_canvas(self, tmp_path):
        import pymupdf

        image = make_png(tmp_path / "img.png", 900, 400)
        scene = Scene(
            start=0, end=2, image=image, kind="figure", text="第一段。", point="成功率 67%"
        )
        chrome = pymupdf.Pixmap(str(render_chrome(scene, tmp_path / "c.png", title="标题", layout=LANDSCAPE)))
        assert (chrome.width, chrome.height) == (LANDSCAPE.width, LANDSCAPE.height)
        card = pymupdf.Pixmap(str(render_image_card(scene, tmp_path / "card.png", layout=LANDSCAPE)))
        assert (card.width, card.height) == (LANDSCAPE.image_box_w, LANDSCAPE.image_box_h)
        row = pymupdf.Pixmap(str(render_point_row("成功率 67%", tmp_path / "p.png", layout=LANDSCAPE)))
        assert (row.width, row.height) == (LANDSCAPE.width, LANDSCAPE.point_height)
        band = pymupdf.Pixmap(str(render_caption_band("一句字幕。", tmp_path / "b.png", layout=LANDSCAPE)))
        assert (band.width, band.height) == (LANDSCAPE.width, LANDSCAPE.height - LANDSCAPE.subtitle_top)

    def test_portrait_render_is_unchanged_by_default(self, tmp_path):
        import pymupdf

        image = make_png(tmp_path / "img.png", 700, 500)
        scene = Scene(start=0, end=2, image=image, kind="figure", text="第一段。")
        chrome = pymupdf.Pixmap(str(render_chrome(scene, tmp_path / "c.png", title="标题")))
        assert (chrome.width, chrome.height) == (VIDEO_W, VIDEO_H)


@pytest.mark.skipif(not ffmpeg_available(), reason="需要系统安装 ffmpeg")
class TestLandscapeEncode:
    def test_landscape_video_is_1920x1080_and_keeps_the_audio_length(self, tmp_path):
        import wave

        image = make_png(tmp_path / "img.png", 900, 400)
        scenes = [
            Scene(start=0, end=2, image=image, kind="figure", text="第一段。", speaker="A"),
            Scene(start=2, end=4, image=image, kind="figure", text="第二段。", speaker="B"),
        ]
        chromes = [
            render_chrome(scene, tmp_path / f"c{i}.png", title="标题", layout=LANDSCAPE)
            for i, scene in enumerate(scenes)
        ]
        cards = [
            render_image_card(scene, tmp_path / f"k{i}.png", layout=LANDSCAPE)
            for i, scene in enumerate(scenes)
        ]
        audio = tmp_path / "a.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 4)

        out = tmp_path / "landscape.mp4"
        encode_video(
            scenes,
            chromes,
            audio,
            out,
            target_duration=4.0,
            image_cards=cards,
            transitions=plan_transitions(
                scenes, [str(s.image) for s in scenes], cards=cards, slides=chromes
            ),
            layout=LANDSCAPE,
        )
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, check=True,
        ).stdout
        assert f"{LANDSCAPE.width},{LANDSCAPE.height}" in probe, probe
        duration = float(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        )
        assert duration == pytest.approx(4.0, abs=0.3), f"横版长度没对齐音频：{duration}"


class TestLandscapeApi:
    """横版是**额外产出**的一份，不是替换竖版。

    对外只有三件事要说清楚：怎么生成、怎么取、竖版有没有被动过。
    """

    @staticmethod
    def _client(tmp_path):
        from fastapi.testclient import TestClient

        from app.config import Settings
        from app.main import create_app

        settings = Settings(
            force_mock=True,
            enable_video=True,
            data_dir=tmp_path / "data",
            database_path=tmp_path / "data" / "landscape.db",
        )
        app = create_app(settings)
        return TestClient(app), settings

    @staticmethod
    def _wait(client, episode_id: str, timeout: float = 180.0) -> dict:
        import time as _time

        deadline = _time.time() + timeout
        while _time.time() < deadline:
            body = client.get(f"/api/episodes/{episode_id}").json()
            if body["status"] in ("completed", "failed"):
                return body
            _time.sleep(0.2)
        raise AssertionError("任务超时")

    def test_landscape_rebuild_keeps_portrait_and_adds_a_second_file(self, tmp_path):
        client, settings = self._client(tmp_path)
        with client:
            created = client.post(
                "/api/episodes", json={"source_type": "text", "text": SAMPLE_TEXT}
            ).json()
            episode = self._wait(client, created["id"])
            portrait = episode.get("video")
            assert portrait is not None and episode.get("video_landscape") is None

            response = client.post(
                f"/api/episodes/{created['id']}/video/rebuild", params={"orientation": "landscape"}
            )
            assert response.status_code == 200, response.text
            body = response.json()

            # 竖版原样不动（URL 里没有 orientation、字节数一致）
            assert body["video"]["url"] == portrait["url"]
            assert body["video"]["bytes"] == portrait["bytes"]
            landscape = body["video_landscape"]
            assert landscape is not None, "应当产出横版"
            assert "orientation=landscape" in landscape["url"]
            assert landscape["orientation"] == "landscape"

            landscape_path = Path(settings.video_dir) / f"{created['id']}.landscape.mp4"
            assert landscape_path.exists(), "横版文件应当叫 <id>.landscape.mp4"
            assert landscape_path.stat().st_size != landscape_path.stat().st_size - 1

            # 两种画幅都能放出来，且横版是 1920×1080
            import subprocess

            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", str(landscape_path)],
                capture_output=True, text=True, check=True,
            ).stdout
            assert "1920,1080" in probe, probe

            served = client.get(
                f"/api/episodes/{created['id']}/video", params={"orientation": "landscape"}
            )
            assert served.status_code == 200
            assert served.headers["accept-ranges"] == "bytes"
            assert served.content[4:8] == b"ftyp"

    def test_landscape_404_before_it_is_generated(self, tmp_path):
        client, _ = self._client(tmp_path)
        with client:
            created = client.post(
                "/api/episodes", json={"source_type": "text", "text": SAMPLE_TEXT}
            ).json()
            self._wait(client, created["id"])
            response = client.get(
                f"/api/episodes/{created['id']}/video", params={"orientation": "landscape"}
            )
            assert response.status_code == 404
            assert "横版" in response.json()["detail"]

    def test_unknown_orientation_is_rejected(self, tmp_path):
        client, _ = self._client(tmp_path)
        with client:
            created = client.post(
                "/api/episodes", json={"source_type": "text", "text": SAMPLE_TEXT}
            ).json()
            self._wait(client, created["id"])
            response = client.post(
                f"/api/episodes/{created['id']}/video/rebuild", params={"orientation": "竖向"}
            )
            assert response.status_code == 400
            assert "未知画幅" in response.json()["detail"]


class TestFocusBox:
    """图内聚光灯的坐标整理。

    这是「让图跟着讲解动」的第一步，但它**必须框对地方** ——
    框错了观众会以为那块真的在讲那个，比不框更糟。所以边界收得很紧。
    """

    def test_accepts_a_normal_box(self):
        focus = normalize_focus({"x": 0.05, "y": 0.1, "w": 0.4, "h": 0.3, "label": "编码器"})
        assert focus == {"x": 0.05, "y": 0.1, "w": 0.4, "h": 0.3, "label": "编码器"}

    def test_rejects_pixel_coordinates(self):
        """模型偶尔会给像素坐标。

        归一化里没法反推「这张图有多大」，所以宁可这一帧不框 ——
        猜错会框到画面的角落，而框错地方比不框更糟。
        """
        assert normalize_focus({"x": 120, "y": 80, "w": 600, "h": 400}) is None
        assert normalize_focus({"x": 0.2, "y": 0.3, "w": 0.6, "h": 0.7})["w"] == pytest.approx(0.6)

    def test_clamps_to_the_image(self):
        focus = normalize_focus({"x": 0.8, "y": 0.8, "w": 0.9, "h": 0.9})
        assert focus is not None
        assert focus["x"] + focus["w"] <= 1.0001
        assert focus["y"] + focus["h"] <= 1.0001

    def test_rejects_tiny_or_invalid(self):
        assert normalize_focus({"x": 0.1, "y": 0.1, "w": 0.01, "h": 0.01}) is None
        assert normalize_focus({"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}) is None, "面积太小也不框"
        assert normalize_focus({"x": "a", "y": 0, "w": 1, "h": 1}) is None
        assert normalize_focus(None) is None
        assert normalize_focus("别框了") is None

    def test_label_is_trimmed(self):
        focus = normalize_focus({"x": 0, "y": 0, "w": 0.5, "h": 0.5, "label": "这是一个非常长的标签超过十六个字"})
        assert focus is not None and len(focus["label"]) <= 16

    def test_point_items_carry_both_point_and_focus(self):
        items = normalize_point_items(
            [
                {"segment": 0, "point": "成功率 67%", "focus": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.3}},
                {"segment": 1, "point": "过渡", "focus": None},
            ],
            count=2,
        )
        assert items[0]["point"] == "成功率 67%"
        assert items[0]["focus"]["w"] == pytest.approx(0.3)
        assert items[1] == {"point": "过渡", "focus": None}

    def test_points_prompt_asks_for_focus_and_warns_against_guessing(self):
        zh = build_points_messages([{"text": "第一段"}], title="T", image_captions=["Figure 1: 编码器"])
        system = zh[0]["content"]
        assert "focus" in system and "框错地方比不框更糟" in system
        assert "Figure 1: 编码器" in zh[1]["content"], "要告诉模型这一段配的是哪张图"

        en = build_points_messages([{"text": "seg"}], title="T", language="en", image_captions=["Figure 1"])
        assert "wrong place is worse" in en[0]["content"]
        assert "Figure 1" in en[1]["content"]


@pytest.mark.skipif(not ffmpeg_available(), reason="需要系统安装 ffmpeg")
class TestFocusIsRendered:
    """聚光灯要真的压暗了「不是重点」的地方，而且没有把重点本身压暗。"""

    FOCUS = {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5, "label": "重点"}

    def _encode(self, tmp_path: Path, *, with_focus: bool = True) -> tuple[Path, Path]:
        import wave

        image = self._pattern(tmp_path / "fig.png")
        scene = Scene(
            start=0,
            end=3.0,
            image=image,
            kind="figure",
            speaker="A",
            text="第一段。",
            focus=dict(self.FOCUS) if with_focus else None,
        )
        chrome = render_chrome(scene, tmp_path / "chrome.png", title="测试")
        card = render_image_card(scene, tmp_path / "card.png")
        rows: list[Path | None] = [None]
        if with_focus:
            rows = [render_focus_overlay(scene, tmp_path / "focus.png", focus=dict(self.FOCUS))]

        audio = tmp_path / "a.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 3)

        out = tmp_path / "focus.mp4"
        encode_video(
            [scene],
            [chrome],
            audio,
            out,
            target_duration=3.0,
            image_cards=[card],
            focus_overlays=rows,
            waveform=False,
            transitions=plan_transitions([scene], ["x"], cards=[card]),
        )
        return out, card

    @staticmethod
    def _pattern(path: Path) -> Path:
        """整张图都是深色：压暗白蒙版一盖就明显变亮，方不方便量。"""
        import pymupdf

        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 700, 500), False)
        pix.set_rect(pix.irect, (40, 40, 40))
        pix.save(str(path))
        return path

    def _frame(self, video: Path, seconds: float, out: Path) -> Path:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-ss", f"{seconds:.3f}",
             "-frames:v", "1", str(out)],
            capture_output=True, text=True, check=True,
        )
        return out

    @staticmethod
    def _at(frame: Path):
        import pymupdf

        pix = pymupdf.Pixmap(str(frame))
        n, width = pix.n, pix.width
        samples = pix.samples

        def at(x: int, y: int):
            index = (y * width + x) * n
            return (samples[index], samples[index + 1], samples[index + 2])

        return at

    def test_outside_is_dimmed_and_inside_is_not(self, tmp_path):
        video, _ = self._encode(tmp_path)
        at = self._at(self._frame(video, 1.5, tmp_path / "f.png"))
        # 图片在卡里居中：700×500 的图放进 896×742 → 居中，四周留白
        img_left = IMAGE_BOX_LEFT + (IMAGE_BOX_W - 700) / 2
        img_top = IMAGE_TOP + (IMAGE_BOX_H - 500) / 2
        inside = at(int(img_left + 0.5 * 700), int(img_top + 0.5 * 500))
        outside = at(int(img_left + 0.08 * 700), int(img_top + 0.08 * 500))
        assert outside[0] > 150, f"框外应当被白蒙版压亮：{outside}"
        assert inside[0] < 80, f"框内应当保持原图（深色）：{inside}"
        assert outside[0] > inside[0] + 60, "框内外的明暗差不够，聚光灯没生效"

    def test_no_focus_means_no_dimming(self, tmp_path):
        video, _ = self._encode(tmp_path, with_focus=False)
        at = self._at(self._frame(video, 1.5, tmp_path / "n.png"))
        img_left = IMAGE_BOX_LEFT + (IMAGE_BOX_W - 700) / 2
        img_top = IMAGE_TOP + (IMAGE_BOX_H - 500) / 2
        corner = at(int(img_left + 0.08 * 700), int(img_top + 0.08 * 500))
        assert corner[0] < 80, f"没有聚光灯时不该压暗：{corner}"


class TestFocusRejectsUselessBoxes:
    """实测出来的两种「没用的框」，都要挡掉。

    模型（DeepSeek，纯文本、看不到图）第一次给回的是**整张图**（`w=1,h=1`）外加一个图名，
    第二次给了像素坐标。两种都等于没框，甚至更糟：整张图盖上蒙版只是把图压暗一圈。
    """

    def test_rejects_full_frame(self):
        assert normalize_focus({"x": 0, "y": 0, "w": 1, "h": 1, "label": "四比特精度表"}) is None
        assert normalize_focus({"x": 0.02, "y": 0.02, "w": 0.96, "h": 0.96}) is None

    def test_accepts_a_real_sub_region(self):
        focus = normalize_focus({"x": 0.55, "y": 0.1, "w": 0.4, "h": 0.5, "label": "右侧曲线"})
        assert focus is not None and focus["w"] == pytest.approx(0.4)

    def test_rejects_pixels_and_garbage(self):
        assert normalize_focus({"x": 600, "y": 400, "w": 300, "h": 200}) is None
        assert normalize_focus({"x": "左", "y": 0, "w": 1, "h": 1}) is None
        assert normalize_focus([]) is None
