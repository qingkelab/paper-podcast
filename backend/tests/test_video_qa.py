"""成片自查（`services/video_qa.py`）与响度（`services/loudness.py`）的测试。

这些断言全都是**像素/采样级的事实**，不是「字符串里有这个字段」：
- 静止空档：造一段真的 6 秒不动的画面，看它能不能被挑出来；再看有变化的片子不会被误报；
- 响度：造一段真的偏轻的音频，出片后**再量一遍**，看它有没有被抬到目标附近。

判据为什么这么定（真实成片标定的过程、噪声底线在哪）写在 `video_qa.py` 的模块开头。
"""

from __future__ import annotations

import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from app.services import loudness, video_qa
from app.services.video import encode_video
from app.services.video_core import Scene

ffmpeg_missing = pytest.mark.skipif(
    not loudness.ffmpeg_available(), reason="需要系统安装 ffmpeg"
)


def make_tone(path: Path, seconds: float, *, amplitude: float = 0.05, rate: int = 24000) -> Path:
    """一段**偏轻**的 220Hz 正弦（默认振幅 0.05 ≈ -26dBFS）—— 用来验证响度会被抬起来。"""
    frames = bytearray()
    for index in range(int(rate * seconds)):
        value = int(amplitude * 32767 * math.sin(2 * math.pi * 220 * index / rate))
        frames += struct.pack("<h", value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


def make_silence(path: Path, seconds: float, *, rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def make_png(path: Path, color: tuple[int, int, int], w: int = 320, h: int = 200) -> Path:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, color)
    pix.save(str(path))
    return path


def make_clip(path: Path, colors: list[tuple[int, int, int]], seconds: float = 3.0) -> Path:
    """把若干张纯色图拼成一段视频：`colors` 有几种，画面就有几次变化。"""
    import pymupdf

    tmp = path.parent / "clip-src"
    tmp.mkdir(exist_ok=True)
    images: list[Path] = []
    for index, color in enumerate(colors):
        target = tmp / f"c{index}.png"
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 320, 200), False)
        pix.set_rect(pix.irect, color)
        pix.save(str(target))
        images.append(target)
    listfile = tmp / "list.txt"
    step = seconds / max(1, len(images))
    listfile.write_text(
        "".join(f"file '{image}'\nduration {step:.3f}\n" for image in images),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
            "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


# --------------------------------------------------------------------------
# 静止空档
# --------------------------------------------------------------------------


class TestStaticGaps:
    def test_pure_math_single_gap(self):
        """10 秒里一次变化都没有 → 一整段空档（2fps 抽样 → 19 对）。"""
        ratios = [0.0] * 19
        gaps = video_qa.find_static_gaps(ratios, fps=2.0, min_gap=4.0)
        assert len(gaps) == 1
        assert gaps[0].seconds == pytest.approx(9.5, abs=0.6)

    def test_pure_math_no_gap_when_something_happens(self):
        """每秒都有一次可见变化 → 不该报空档（判据别把「有变化」也算进去）。"""
        ratios = [0.0, 0.5] * 10
        assert video_qa.find_static_gaps(ratios, fps=2.0, min_gap=4.0) == []

    def test_the_progress_bar_alone_is_not_a_change(self):
        """进度条/声波条那种千分之几的细碎运动**不算**变化（这是标定出来的门槛）。"""
        ratios = [0.0009] * 19
        gaps = video_qa.find_static_gaps(ratios, fps=2.0, min_gap=4.0)
        assert len(gaps) == 1, "进度条在爬不等于「画面上有新东西」"

    def test_a_short_pause_is_not_reported(self):
        """3 秒的静止不报（门槛是 4 秒）—— 短句本来就该安静地读完。"""
        ratios = [0.5] + [0.0] * 3 + [0.5] + [0.0] * 10
        gaps = video_qa.find_static_gaps(ratios, fps=2.0, min_gap=4.0)
        assert all(gap.seconds >= 4.0 for gap in gaps)
        assert len(gaps) == 1


@ffmpeg_missing
class TestStaticGapsOnRealVideo:
    def test_a_frozen_picture_is_detected(self, tmp_path):
        """真的编一段 6 秒不动的画面 → 空档必须被抓到。"""
        clip = make_clip(tmp_path / "frozen.mp4", [(40, 90, 160), (40, 90, 160)], seconds=6.0)
        report = video_qa.qa_video(clip)
        assert report.duration_sec == pytest.approx(6.0, abs=0.5)
        assert report.longest_gap_sec >= 4.0, (
            f"6 秒不动的画面没被判定为空档（最长 {report.longest_gap_sec:.1f}s）"
        )
        assert report.issues(), "空档要能在 issues 里点名，并带时间点"

    def test_changing_content_is_not_flagged(self, tmp_path):
        """每 1.5 秒换一次颜色 → 不该报空档（守卫：别把正常片子判成静止）。"""
        colors = [
            (40, 90, 160), (200, 60, 60), (60, 160, 90),
            (160, 160, 60), (90, 60, 160),
        ]
        clip = make_clip(tmp_path / "moving.mp4", colors * 2, seconds=8.0)
        report = video_qa.qa_video(clip)
        assert report.change_events >= 5
        assert report.longest_gap_sec < 4.0, (
            f"每 1.5 秒换一次画面却被判成静止（最长 {report.longest_gap_sec:.1f}s）"
        )

    def test_missing_file_does_not_raise(self, tmp_path):
        """自查失败不该炸：文件不在也要给一份空报告。"""
        report = video_qa.qa_video(tmp_path / "nope.mp4")
        assert report.duration_sec == 0.0
        assert report.notes


# --------------------------------------------------------------------------
# 响度
# --------------------------------------------------------------------------


@ffmpeg_missing
class TestLoudness:
    def test_silence_measures_as_nothing(self, tmp_path):
        """数字静音测不出响度 → None（**不能**当成 0 LUFS：那会推着我们把静音放大 14dB）。"""
        assert loudness.measure_loudness(make_silence(tmp_path / "s.wav", 1.0)) is None

    def test_a_tone_measures_close_to_its_amplitude(self, tmp_path):
        """220Hz 正弦振幅 0.05（-26dBFS）→ 实测约 -29 LUFS（正弦的 LUFS 比峰值低 3dB）。"""
        tone = make_tone(tmp_path / "t.wav", 2.0, amplitude=0.05)
        measured = loudness.measure_loudness(tone)
        assert measured is not None
        assert measured.integrated_lufs == pytest.approx(-29.0, abs=2.5)
        assert measured.true_peak_db is not None

    def test_normalization_chain_targets_our_numbers(self, tmp_path):
        chain = loudness.normalization_chain(make_tone(tmp_path / "t.wav", 2.0))
        assert chain is not None
        assert f"I={loudness.TARGET_LUFS}" in chain
        assert f"TP={loudness.TARGET_TRUE_PEAK_DB}" in chain
        assert "measured_I=" in chain and "linear=true" in chain

    def test_chain_is_none_for_silence(self, tmp_path):
        """测不出输入就别加滤镜 —— 宁可不归一化，也不要把静音放大到 -14 LUFS。"""
        assert loudness.normalization_chain(make_silence(tmp_path / "s.wav", 1.0)) is None

    def test_encoding_actually_raises_the_loudness(self, tmp_path):
        """**端到端**：偏轻的音轨出片后要更响，而且封装的滤镜链真的生效了。

        这是这一整套里唯一能证明「归一化进了 MP4」的断言 —— 只看代码里有没有
        `loudnorm` 是证明不了的（滤镜串写错一个参数就是静默无效）。
        """
        frame = make_png(tmp_path / "f.png", (250, 250, 250))
        audio = make_tone(tmp_path / "t.wav", 4.0, amplitude=0.03)
        before = loudness.measure_loudness(audio)
        assert before is not None
        scene = Scene(
            start=0.0, end=4.0, image=frame, kind="figure", speaker="A", text="一句话。"
        )
        encode_video([scene], [frame], audio, tmp_path / "out.mp4")
        after = loudness.measure_loudness(tmp_path / "out.mp4")
        assert after is not None
        assert after.integrated_lufs > before.integrated_lufs + 1.0, (
            f"响度没被抬起来：出片前 {before.integrated_lufs:.1f} LUFS，"
            f"出片后 {after.integrated_lufs:.1f} LUFS"
        )
        # 但**不能越过 true peak 上限**（宁可达不到目标，也不许削波）
        assert after.true_peak_db is not None
        assert after.true_peak_db <= loudness.TARGET_TRUE_PEAK_DB + 1.0

    def test_describe_reports_the_shortfall(self, tmp_path):
        measured = loudness.measure_loudness(make_tone(tmp_path / "t.wav", 2.0, amplitude=0.02))
        assert measured is not None
        text = loudness.describe(measured)
        assert "LUFS" in text and "目标" in text

    def test_qa_reports_the_shipped_file_not_the_source(self, tmp_path):
        """自查报的响度必须是**成片**的，不是源音频的。

        实测这个坑：`100dce22` 源音频 −18.1 LUFS、成片 −15.7 LUFS（差 2.4 LU）。
        报源音频的数字，等于「归一化到底进没进 MP4」这件事根本没人看着 ——
        滤镜链哪天写错了、静默失效，自查数字照样漂亮。
        """
        frame = make_png(tmp_path / "f.png", (250, 250, 250))
        # 源音频故意做得很轻：出片会被 loudnorm 抬上去，两个数必须不一样
        audio = make_tone(tmp_path / "t.wav", 4.0, amplitude=0.02)
        source = loudness.measure_loudness(audio)
        assert source is not None
        scene = Scene(
            start=0.0, end=4.0, image=frame, kind="figure", speaker="A", text="一句话。"
        )
        video = tmp_path / "out.mp4"
        encode_video([scene], [frame], audio, video)

        report = video_qa.qa_video(video, audio_path=audio)
        assert report.loudness is not None
        shipped = loudness.measure_loudness(video)
        assert shipped is not None
        assert report.loudness.integrated_lufs == pytest.approx(
            shipped.integrated_lufs, abs=0.2
        )
        assert report.loudness.integrated_lufs > source.integrated_lufs + 1.0, (
            f"自查报的还是源音频的响度（{report.loudness.integrated_lufs:.1f} LUFS）"
        )
        # 长度仍然对着**音频**量（那是另一条不变量：视频长度 == 音频长度）
        assert report.audio_duration_sec == pytest.approx(4.0, abs=0.3)
