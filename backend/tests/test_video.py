"""视频解读播客的测试：时间轴、配图分配、字幕排版、编码产物。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.services.video import (
    VIDEO_H,
    VIDEO_W,
    VideoError,
    _fit_subtitle,
    _normalize_assignment,
    build_assign_messages,
    build_scenes,
    encode_video,
    ffmpeg_available,
    heuristic_assignment,
    render_slide,
    Scene,
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


# --------------------------------------------------------------------------
# 画幅
# --------------------------------------------------------------------------


class TestCanvasSize:
    def test_matches_pdf_first_page_aspect(self):
        """画幅要跟论文首页一致（935x1210）。"""
        assert (VIDEO_W, VIDEO_H) == (936, 1210)

    def test_width_is_even_for_h264(self):
        """H.264 的 yuv420p 要求宽高能被 2 整除，935 会被 libx264 直接拒掉。

        实测报错：width not divisible by 2 (935x1210)
        """
        assert VIDEO_W % 2 == 0
        assert VIDEO_H % 2 == 0


# --------------------------------------------------------------------------
# 配图分配
# --------------------------------------------------------------------------


class TestAssignment:
    def test_normalizes_model_output(self):
        result = _normalize_assignment(
            [
                {"image_id": "f1", "segment": 3},
                {"image_id": "f2", "segment": 6},
                {"image_id": "f3", "segment": 10},
            ],
            count=20,
            ordered_ids=["f1", "f2", "f3"],
        )
        assert result == [3, 6, 10]

    def test_duplicates_are_pushed_apart(self):
        """起点相同的图必须被推开。

        取图逻辑是「取最后一个 start <= 当前段」，起点相同会让靠前的图
        永远不显示。实测模型给过 [3, 6, 6, 10, 12]，f2 就这样丢了。
        """
        result = _normalize_assignment(
            [
                {"image_id": "f1", "segment": 3},
                {"image_id": "f2", "segment": 6},
                {"image_id": "f3", "segment": 6},
                {"image_id": "f4", "segment": 10},
                {"image_id": "f5", "segment": 12},
            ],
            count=21,
            ordered_ids=["f1", "f2", "f3", "f4", "f5"],
        )
        assert result == sorted(set(result)), f"仍有重复：{result}"
        assert result == [3, 6, 7, 10, 12]

    def test_out_of_order_is_made_monotonic(self):
        result = _normalize_assignment(
            [
                {"image_id": "f1", "segment": 9},
                {"image_id": "f2", "segment": 2},
            ],
            count=20,
            ordered_ids=["f1", "f2"],
        )
        assert result == sorted(result)

    def test_missing_ids_are_filled(self):
        result = _normalize_assignment(
            [{"image_id": "f1", "segment": 4}],
            count=20,
            ordered_ids=["f1", "f2", "f3"],
        )
        assert len(result) == 3
        assert result == sorted(result)

    def test_out_of_range_segments_are_clamped(self):
        result = _normalize_assignment(
            [{"image_id": "f1", "segment": 999}],
            count=5,
            ordered_ids=["f1"],
        )
        assert result == [4]

    def test_garbage_returns_none(self):
        assert _normalize_assignment("不是列表", count=5, ordered_ids=["f1"]) is None
        assert _normalize_assignment([], count=5, ordered_ids=["f1"]) is None

    def test_heuristic_is_strictly_increasing(self):
        """段数很少时均匀分布容易撞在一起，也必须保证严格递增。"""
        for count in (5, 6, 8, 21, 40):
            for figures in (1, 3, 5):
                result = heuristic_assignment(count, figures)
                assert len(result) == figures
                assert result == sorted(set(result)), f"count={count} fig={figures}: {result}"
                assert all(0 <= v < count for v in result)

    def test_heuristic_spreads_across_body(self):
        result = heuristic_assignment(40, 5)
        assert result[0] > 0, "第一张图不该压在第 0 段（留出封面）"
        assert result[-1] < 40, "最后一张图不该压到最后一段（留出结尾卡）"


class TestAssignPrompt:
    def test_prompt_lists_segments_and_assets(self):
        messages = build_assign_messages(
            [{"speaker": "A", "text": "第一段内容"}, {"speaker": "B", "text": "第二段内容"}],
            [{"id": "f1", "caption": "Figure 1: 架构图"}],
        )
        user = messages[-1]["content"]
        assert "[0]" in user and "[1]" in user
        assert "f1" in user
        assert "Figure 1" in user


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

    def _assets(self, tmp_path):
        cover = make_png(tmp_path / "cover.png")
        figs = [
            {"id": "f1", "path": str(make_png(tmp_path / "f1.png")), "caption": "Figure 1"},
            {"id": "f2", "path": str(make_png(tmp_path / "f2.png")), "caption": "Figure 2"},
        ]
        illu = make_png(tmp_path / "illu.png", 600, 400)
        return cover, figs, illu

    def test_covers_head_music_with_cover(self, tmp_path):
        """片头音乐期间必须也有画面，不能让视频开头是黑的。"""
        cover, figs, illu = self._assets(tmp_path)
        scenes = build_scenes(
            segments=self.SEGMENTS,
            timings=self.TIMINGS,
            audio_duration=27.0,
            cover=cover,
            figures=figs,
            illustration=illu,
            assignment=[0, 2],
        )
        assert scenes[0].kind == "cover"
        assert scenes[0].start == 0.0
        assert scenes[0].end == pytest.approx(7.0), "开头要覆盖到第一段开始"

    def test_covers_tail_music_with_illustration(self, tmp_path):
        cover, figs, illu = self._assets(tmp_path)
        scenes = build_scenes(
            segments=self.SEGMENTS,
            timings=self.TIMINGS,
            audio_duration=27.0,
            cover=cover,
            figures=figs,
            illustration=illu,
            assignment=[0, 2],
        )
        assert scenes[-1].kind == "illustration"
        assert scenes[-1].end == pytest.approx(27.0)

    def test_timeline_has_no_gaps_or_overlaps(self, tmp_path):
        """画面时间轴必须连续，否则 ffmpeg concat 会丢帧或音画错位。"""
        cover, figs, illu = self._assets(tmp_path)
        scenes = build_scenes(
            segments=self.SEGMENTS,
            timings=self.TIMINGS,
            audio_duration=27.0,
            cover=cover,
            figures=figs,
            illustration=illu,
            assignment=[0, 2],
        )
        for index in range(len(scenes) - 1):
            assert scenes[index].end == pytest.approx(scenes[index + 1].start), (
                f"第 {index} 段与下一段之间有缝"
            )

    def test_every_figure_gets_shown(self, tmp_path):
        """每张图都必须有独占的出现机会，不能被后一张顶掉。"""
        cover, figs, illu = self._assets(tmp_path)
        scenes = build_scenes(
            segments=self.SEGMENTS,
            timings=self.TIMINGS,
            audio_duration=27.0,
            cover=cover,
            figures=figs,
            illustration=illu,
            assignment=[0, 2],
        )
        used = {scene.image.name for scene in scenes}
        assert "f1.png" in used and "f2.png" in used

    def test_subtitles_follow_script(self, tmp_path):
        cover, figs, illu = self._assets(tmp_path)
        scenes = build_scenes(
            segments=self.SEGMENTS,
            timings=self.TIMINGS,
            audio_duration=27.0,
            cover=cover,
            figures=figs,
            illustration=illu,
            assignment=[0, 2],
        )
        body = [s for s in scenes if s.kind == "figure"]
        assert [s.text for s in body] == ["开场", "追问", "回答", "收尾"]
        assert [s.speaker for s in body] == ["A", "B", "A", "B"]

    def test_falls_back_to_cover_when_no_figures(self, tmp_path):
        """没有正文配图时也要有画面，不能抛错。"""
        cover = make_png(tmp_path / "cover.png")
        scenes = build_scenes(
            segments=self.SEGMENTS,
            timings=self.TIMINGS,
            audio_duration=27.0,
            cover=cover,
            figures=[],
            illustration=None,
            assignment=None,
        )
        assert len(scenes) >= len(self.SEGMENTS)
        assert all(scene.image.exists() for scene in scenes)

    def test_raises_without_any_image(self, tmp_path):
        with pytest.raises(VideoError):
            build_scenes(
                segments=self.SEGMENTS,
                timings=self.TIMINGS,
                audio_duration=27.0,
                cover=None,
                figures=[],
                illustration=None,
                assignment=None,
            )

    def test_raises_without_timings(self, tmp_path):
        cover, figs, illu = self._assets(tmp_path)
        with pytest.raises(VideoError):
            build_scenes(
                segments=self.SEGMENTS,
                timings=[],
                audio_duration=27.0,
                cover=cover,
                figures=figs,
                illustration=illu,
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
        """字幕必须放得进字幕区。

        竖版只有 856px 宽、148px 高，比横版窄得多；
        光看行数不看高度，字号偏大时文字会溢出到画面外。
        """
        size, lines = _fit_subtitle(text)
        if not text:
            assert lines == []
            return
        assert len(lines) <= 5
        assert len(lines) * size * 1.36 <= 148.1, f"溢出：{len(lines)} 行 × {size}pt"

    def test_shrinks_font_for_long_text(self):
        short_size, short_lines = _fit_subtitle("好的。")
        long_size, long_lines = _fit_subtitle("字" * 200)
        assert long_size < short_size
        assert len(long_lines) > len(short_lines)

    def test_keeps_readable_size_for_typical_segment(self):
        """典型的脚本段（30-120 字）不该被压到最小字号。"""
        text = "它把循环结构换成注意力。打个比方，以前的模型像接力赛，一棒传一棒，必须等前一棒跑完；现在改成所有人同时看全场。" * 1
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

        concat 的每段时长会按帧取整，累加后视频会比音频长一两秒
        （实测 209.17s vs 206.9s），表现为结尾只剩画面没有声音，
        所以用 -t 把总长钳住。
        """
        image = make_png(tmp_path / "img.png", 900, 600)
        scenes = [
            Scene(start=0, end=2, image=image, kind="figure", speaker="A", text="第一段"),
            Scene(start=2, end=4, image=image, kind="figure", speaker="B", text="第二段"),
        ]
        slides = [
            render_slide(scene, tmp_path / f"slide{i}.png") for i, scene in enumerate(scenes)
        ]

        audio = tmp_path / "audio.wav"
        import wave

        rate = 8000
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(b"\x00\x00" * rate * 4)  # 4 秒

        out = tmp_path / "out.mp4"
        result = encode_video(scenes, slides, audio, out, target_duration=4.0)
        assert out.exists() and out.stat().st_size > 1000

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "stream=width,height,codec_name", "-of", "csv=p=0", str(out),
            ],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "h264" in probe
        assert f"{VIDEO_W},{VIDEO_H}" in probe

        duration = float(
            subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "csv=p=0", str(out),
                ],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        )
        assert duration == pytest.approx(4.0, abs=0.2), f"时长 {duration} 未对齐音频"

    def test_encode_rejects_mismatched_lengths(self, tmp_path):
        image = make_png(tmp_path / "img.png")
        scenes = [Scene(start=0, end=1, image=image, kind="figure")]
        with pytest.raises(VideoError):
            encode_video(scenes, [], tmp_path / "a.mp3", tmp_path / "o.mp4")
