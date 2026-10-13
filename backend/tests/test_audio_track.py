"""音频轨分析：停顿检测与字幕换句对齐。

这两件事都容易「看起来对、其实差半秒」——所以测的是时间戳本身，
以及「检测失败时必须能退回按字数切句」这条降级路径。
"""

from __future__ import annotations

import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from app.services.audio_track import (
    Pause,
    align_boundaries,
    detect_pauses,
    parse_silences,
)
from app.services.video import beat_windows, beat_windows_aligned

FFMPEG = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True).returncode == 0


class TestParseSilences:
    def test_parses_pairs(self):
        stderr = (
            "[silencedetect @ 0x1] silence_start: 1.069167\n"
            "[silencedetect @ 0x1] silence_end: 1.516667 | silence_duration: 0.4475\n"
            "[silencedetect @ 0x1] silence_start: 8.5\n"
        )
        pauses = parse_silences(stderr)
        # 只有 start 没有 end 的那段不算（音频在静音里结束的情况由 duration 兜底）
        assert len(pauses) == 1
        assert pauses[0].start == pytest.approx(1.069167)
        assert pauses[0].center == pytest.approx((1.069167 + 1.516667) / 2)

    def test_ignores_noise(self):
        assert parse_silences("") == []
        assert parse_silences("no silence here") == []


class TestAlignBoundaries:
    def test_snaps_to_nearest_pause_center(self):
        pauses = [Pause(3.0, 3.6)]      # 中心 3.3
        aligned = align_boundaries([3.1], pauses, start=0.0, end=8.0)
        assert aligned == [pytest.approx(3.3)]

    def test_a_far_pause_still_pulls_the_boundary(self):
        """**反过来了**：现在「总是吸到最近的停顿」，不再用「够近才吸」的门槛。

        实测依据（最新一集 2f359258）：旧门槛下 25 个换句边界只有 3 个落在停顿上，
        其余离最近停顿 1.0~3.2 秒 —— 字幕就在人还没停顿的地方换了，观众看到的是
        「字幕和语音对不上」。改成总是吸附之后：**24/25（96%）落在停顿上，中位数 0.00 秒**。

        代价是位置可能偏离「按字数比例」的估算值几秒 —— 但落在一个真实停顿上，
        比精确地落在句子中间要好得多。
        """
        from app.services.audio_track import Pause, align_boundaries

        pauses = [Pause(start=3.0, end=3.4)]  # 中心 3.2，离 10.5 很远
        aligned = align_boundaries([10.5], pauses, start=0.0, end=20.0)
        assert aligned == [3.2], f"应当吸到最近的停顿上：{aligned}"

    def test_no_pause_means_the_estimate_stands(self):
        """没有可用停顿（检测失败 / 这一段真的没停顿）→ 保留按字数比例的估算值。

        这条路径必须永远可用：字幕对齐是锦上添花，不能因为它让出片失败。
        """
        from app.services.audio_track import align_boundaries

        assert align_boundaries([10.5], [], start=0.0, end=20.0) == [10.5]


class TestTrimLongSilences:
    """长静音压缩：一次 TTS 异常留下几十秒空白，把 3 分钟的片子撑成 6 分钟。

    实测（b95d6832）：静音占 60%、最长 78.8 秒，而说话只有 154 秒（≈2.6 分钟，
    和设定 3 分钟基本吻合）—— 所以脚本没问题，多出来的全是静音。
    """

    def _make(self, tmp_path, pattern):
        """pattern: [(时长, 有没有声)]，用 440Hz 正弦当「说话」。"""
        import math, struct, subprocess, wave

        rate = 24000
        frames = bytearray()
        for seconds, loud in pattern:
            for index in range(int(rate * seconds)):
                value = int(0.3 * 32767 * math.sin(2 * math.pi * 440 * index / rate)) if loud else 0
                frames += struct.pack("<h", value)
        raw = tmp_path / "raw.wav"
        with wave.open(str(raw), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(bytes(frames))
        out = tmp_path / "in.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), str(out)], check=True)
        return out

    def test_a_long_silence_is_squeezed(self, tmp_path):
        from app.services.audio_track import trim_long_silences
        from app.services.podcast_tts import probe_duration

        # 1 秒说话 + **5 秒空白** + 1 秒说话：中间的空白就是「3 分钟变 6 分钟」那种异常
        source = self._make(tmp_path, [(1.0, True), (5.0, False), (1.0, True)])
        before = probe_duration(source) or 0
        result = trim_long_silences(source, tmp_path / "trimmed.mp3")
        assert result is not None, "5 秒的空白应当被判定为「异常长静音」"
        after = probe_duration(result.path) or 0
        assert after < before - 3.0, f"没压掉多少：{before:.1f} → {after:.1f}"
        assert result.removed_sec > 3.0

    def test_short_pauses_are_left_alone(self, tmp_path):
        """0.5 秒的停顿是正常呼吸，不要动它 —— 压掉会让人说话变成连珠炮。"""
        from app.services.audio_track import trim_long_silences

        source = self._make(tmp_path, [(1.0, True), (0.5, False), (1.0, True)])
        assert trim_long_silences(source, tmp_path / "trimmed.mp3") is None

    def test_times_are_remapped(self, tmp_path):
        """压缩后**必须能重映射时间轴**：不重映射的话字幕与画面会整体错位。"""
        from app.services.audio_track import trim_long_silences

        source = self._make(tmp_path, [(1.0, True), (5.0, False), (1.0, True)])
        result = trim_long_silences(source, tmp_path / "trimmed.mp3")
        assert result is not None
        # 第一段说话（0~1 秒）几乎不动；第二段说话（6~7 秒）整体前移约 4.7 秒
        assert result.remap(0.5) == pytest.approx(0.5, abs=0.35)
        assert result.remap(6.5) < 2.0, f"第二句没有跟上来：{result.remap(6.5):.2f}"
        # 单调：越靠后的时刻映射后不会反而变早
        mapped = [result.remap(t) for t in (0.2, 0.8, 1.2, 6.2, 6.9)]
        assert mapped == sorted(mapped)
