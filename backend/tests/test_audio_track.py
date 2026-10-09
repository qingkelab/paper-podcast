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

    def test_does_not_snap_beyond_tolerance(self):
        pauses = [Pause(5.0, 5.4)]      # 中心 5.2，离 3.1 太远
        aligned = align_boundaries([3.1], pauses, start=0.0, end=8.0, tolerance=0.9)
        assert aligned == [pytest.approx(3.1)]

    def test_keeps_monotonic_and_min_beat(self):
        pauses = [Pause(2.0, 2.4), Pause(2.5, 2.9)]
        aligned = align_boundaries([2.2, 2.7], pauses, start=0.0, end=8.0, min_beat=0.6)
        assert aligned[0] < aligned[1]
        assert aligned[1] - aligned[0] >= 0.6

    def test_ignores_pauses_outside_the_scene(self):
        """段落之外的停顿不能拿来吸附。

        注意 `start=3.0` 时 `min_beat=0.6` 会把边界顶到 3.6 —— 这是另一条规则
        （第一句至少 0.6 秒），不是吸附，所以这里专门把两条分开断言。
        """
        pauses = [Pause(0.0, 0.5), Pause(20.0, 20.5)]
        aligned = align_boundaries([3.2], pauses, start=3.0, end=9.0, min_beat=0.6)
        assert aligned == [pytest.approx(3.6)], "既没被段外停顿拉走，也被最短句下限顶住"

        # 下限不生效时，原值应当原样保留
        aligned = align_boundaries([3.2], pauses, start=0.0, end=9.0, min_beat=0.6)
        assert aligned == [pytest.approx(3.2)]

    def test_empty_inputs(self):
        assert align_boundaries([], [Pause(1, 2)], start=0, end=5) == []
        assert align_boundaries([2.0], [], start=0, end=5) == [pytest.approx(2.0)]


def make_speech_like_wav(path: Path) -> Path:
    """造一段「说话 1 秒 → 停 0.6 秒 → 说话 1 秒」的音频。

    用 220Hz 正弦加一点噪声包络，比纯静音更接近真实语音的检测条件。
    """
    rate = 8000
    frames = bytearray()

    def tone(seconds: float, amplitude: float = 0.35):
        for index in range(int(rate * seconds)):
            value = amplitude * math.sin(2 * math.pi * 220 * index / rate)
            frames.extend(struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32000)))

    def silence(seconds: float):
        for _ in range(int(rate * seconds)):
            frames.extend(struct.pack("<h", 0))

    tone(1.0)
    silence(0.6)
    tone(1.0)

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


@pytest.mark.skipif(not FFMPEG, reason="需要系统安装 ffmpeg")
class TestDetectPauses:
    def test_finds_the_real_silence(self, tmp_path):
        audio = make_speech_like_wav(tmp_path / "speech.wav")
        pauses = detect_pauses(audio)
        assert pauses, "应当检测到中间那段静音"
        target = [p for p in pauses if 0.8 < p.start < 1.4]
        assert target, f"静音应当在 1.0 秒附近开始，实际 {pauses}"
        assert target[0].center == pytest.approx(1.3, abs=0.2)

    def test_missing_file_returns_empty(self, tmp_path):
        # 检测失败不能让整条视频出不来
        assert detect_pauses(tmp_path / "nope.mp3") == []

    def test_all_speech_has_no_pauses(self, tmp_path):
        rate = 8000
        frames = bytearray()
        for index in range(int(rate * 1.5)):
            value = 0.35 * math.sin(2 * math.pi * 220 * index / rate)
            frames.extend(struct.pack("<h", int(value * 32000)))
        audio = tmp_path / "talk.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(bytes(frames))
        assert detect_pauses(audio) == []


class TestBeatWindowsAligned:
    BEATS = ["第一句比较长，占的时间更多一些。", "第二句短。"]

    def test_without_pauses_falls_back_to_proportions(self):
        assert beat_windows_aligned(self.BEATS, 10.0) == beat_windows(self.BEATS, 10.0)

    def test_snaps_boundary_to_pause(self):
        plain = beat_windows(self.BEATS, 10.0)
        boundary = plain[0][1]
        pause = Pause(boundary + 0.3, boundary + 0.7)   # 中心离边界 0.5 秒，在容差内
        aligned = beat_windows_aligned(self.BEATS, 10.0, pauses=[pause])
        assert aligned[0][1] == pytest.approx(pause.center)
        # 窗口仍然首尾相接、覆盖整段
        assert aligned[0][0] == 0.0
        assert aligned[-1][1] == pytest.approx(10.0)
        assert aligned[0][1] == pytest.approx(aligned[1][0])

    def test_single_beat_is_untouched(self):
        beats = ["只有一句。"]
        assert beat_windows_aligned(beats, 4.0, pauses=[Pause(1.0, 1.5)]) == beat_windows(beats, 4.0)
