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
    heuristic_assignment,
    heuristic_per_segment,
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
        assert len(lines) <= 5
        assert len(lines) * size * 1.36 <= 148.1, f"溢出：{len(lines)} 行 × {size}pt"

    def test_shrinks_font_for_long_text(self):
        short_size, _ = _fit_subtitle("好的。")
        long_size, _ = _fit_subtitle("字" * 200)
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
