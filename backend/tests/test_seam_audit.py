"""接缝审计（`services/seam_audit.py`）的判据标定。

这套判据来自 HyperFrames 的 `motion-doctrine` / `seam-craft`（Apache-2.0）：
**接缝的质量在于「切点两侧的动对不对得上」**，所以先要有能把这件事量出来的仪器。

标定的方式是造**已知性质**的小片子 —— 每一条只差一个变量，读数必须跟着变：

| 造出来的片子 | 期望读数 |
|---|---|
| 两张静态画面之间硬切 | `hard_cut`；两侧都不在动 |
| 两张静态画面之间 0.35s 叠化 | `dissolve` |
| 横移 → **同向**横移 | `seamless`；方向一致（`direction_match` 为真） |
| 横移 → **反向**横移 | `seamless`；方向对不上（这条就是「来回摆」） |
| 横移 → 静止 | `hard_cut`；退出在动、进入从静止开始、速度比 0 |

## 标定时踩到的三个坑（都在这里钉住）

1. **方向不能用「差分图的质心」**：平移物体在相邻两帧的差分里是两条边（前缘 + 后缘），
   质心几乎不动 —— 同向平移被报成 `x-→x+`、反向平移被报成 `x+→x-`，符号是乱的。
   改成「找最佳平移」，并且**符号要取反**（`_best_shift` 给的是「挪回前一帧」需要的位移）。
2. **同分时要取 |位移| 最小的那个**：两张纯色画面每个位移的残差都是 0，不排顺序的话
   会返回搜索起点 `(-6,-6)`，凭空报出一个方向。
3. **`hard_cut` 与 `dissolve` 的步数门槛要跟着「场景自己的运动幅度」走**：
   门槛写成绝对的 `MOVE_THRESHOLD` 时，「平移 → 静止」里那几步正常平移（5.8）
   也会被算成「切换的步数」，于是硬切被判成渐变。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services import loudness, seam_audit
from app.services.podcast_tts import RoundTiming

pytestmark = pytest.mark.skipif(
    not loudness.ffmpeg_available(), reason="需要系统安装 ffmpeg"
)

CUT = 2.0


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def _still(path: Path, seconds: float, color: str) -> Path:
    _run(["-f", "lavfi", "-i", f"color=c={color}:s=320x180:d={seconds}:r=30",
          "-pix_fmt", "yuv420p", str(path)])
    return path


def _pan(path: Path, seconds: float, *, x0: float = -120.0, speed: float = 220.0) -> Path:
    """深底上一条白条**匀速横移**（`x = x0 + t·speed`）。"""
    _run(["-f", "lavfi", "-i", f"color=c=black:s=320x180:d={seconds}:r=30",
          "-f", "lavfi", "-i", f"color=c=white:s=120x60:d={seconds}:r=30",
          "-filter_complex", f"[0:v][1:v]overlay=x='{x0}+t*{speed}':y=60",
          "-pix_fmt", "yuv420p", str(path)])
    return path


def _concat(a: Path, b: Path, out: Path) -> Path:
    _run(["-i", str(a), "-i", str(b), "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
          "-pix_fmt", "yuv420p", str(out)])
    return out


def _dissolve(a: Path, b: Path, out: Path, *, duration: float = 0.35) -> Path:
    offset = 1.0
    _run(["-i", str(a), "-i", str(b),
          "-filter_complex",
          f"[0:v][1:v]xfade=transition=fade:duration={duration}:offset={offset}",
          "-pix_fmt", "yuv420p", str(out)])
    return out


def _read(video: Path, at: float):
    audit = seam_audit.audit_seams(video, [at])
    assert audit.seams, f"{video} 的切点没量出来：{audit.notes}"
    return audit.seams[0]


class TestTheReadingMatchesTheKnownClip:
    def test_a_hard_cut_between_stills(self, tmp_path):
        red = _still(tmp_path / "red.mp4", 2.0, "red")
        blue = _still(tmp_path / "blue.mp4", 2.0, "blue")
        seam = _read(_concat(red, blue, tmp_path / "hard.mp4"), CUT)
        assert seam.kind == "hard_cut"
        assert not seam.moving_before and not seam.moving_after
        assert seam.axis_before is None and seam.axis_after is None, "静态画面不该报出方向"

    def test_a_dissolve_is_not_a_hard_cut(self, tmp_path):
        red = _still(tmp_path / "red.mp4", 2.0, "red")
        blue = _still(tmp_path / "blue.mp4", 2.0, "blue")
        seam = _read(_dissolve(red, blue, tmp_path / "xf.mp4"), 1.175)
        assert seam.kind == "dissolve", seam
        assert any("叠化" in issue for issue in seam.issues())

    def test_matching_motion_reads_as_seamless(self, tmp_path):
        pan = _pan(tmp_path / "pan.mp4", 2.0)
        seam = _read(_concat(pan, pan, tmp_path / "match.mp4"), CUT)
        assert seam.kind == "seamless"
        assert seam.moving_before and seam.moving_after
        assert seam.axis_before == seam.axis_after, seam
        assert seam.direction_match is True

    def test_reversed_motion_is_flagged(self, tmp_path):
        right = _pan(tmp_path / "right.mp4", 2.0)
        left = _pan(tmp_path / "left.mp4", 2.0, x0=380.0, speed=-220.0)
        seam = _read(_concat(right, left, tmp_path / "flip.mp4"), CUT)
        assert seam.moving_before and seam.moving_after
        assert seam.direction_match is False, seam
        assert any("方向对不上" in issue for issue in seam.issues())

    def test_motion_into_stillness_reports_the_dead_beat(self, tmp_path):
        pan = _pan(tmp_path / "pan.mp4", 2.0)
        white = _still(tmp_path / "white.mp4", 2.0, "white")
        seam = _read(_concat(pan, white, tmp_path / "settle.mp4"), CUT)
        assert seam.kind == "hard_cut", "平移切到静止是一次看得出来的硬切"
        assert seam.moving_before and not seam.moving_after
        assert seam.speed_ratio == 0
        assert any("从静止开始" in issue for issue in seam.issues())


class TestTheAuditNeverBreaksARender:
    def test_missing_file_is_a_note_not_an_exception(self, tmp_path):
        audit = seam_audit.audit_seams(tmp_path / "nope.mp4", [1.0])
        assert audit.seams == []
        assert audit.notes and "不存在" in audit.notes[0]
        assert "没有可量的切点" in audit.summary()

    def test_a_cut_at_the_very_edge_is_skipped_not_guessed(self, tmp_path):
        red = _still(tmp_path / "red.mp4", 2.0, "red")
        blue = _still(tmp_path / "blue.mp4", 2.0, "blue")
        video = _concat(red, blue, tmp_path / "hard.mp4")
        audit = seam_audit.audit_seams(video, [0.05, 3.99])
        assert audit.seams == [], "边上没有前后各 0.7 秒的窗口，就不该硬量"
        assert any("太靠边" in note for note in audit.notes)


class TestParseCuts:
    def test_accepts_commas_and_full_width_commas(self):
        assert seam_audit.parse_cuts("1.5, 2，3.25") == [1.5, 2.0, 3.25]


class TestItReachesThePayload:
    """审计结果要**真的进库**（`video.qa.seams`），不是只打一行日志。

    这里顺带钉住一个真实踩过的顺序问题：`compose_video` 里那一步是
    `result.qa = report.to_dict()`（**整份替换**），所以
    「画面计划概览（`visual_plan`）」和「接缝审计」都必须写在它**之后** ——
    `visual_plan` 原来写在它之前，算出来了、日志也打了，但**从来没进过库**。
    """

    @staticmethod
    def _compose(tmp_path):
        import wave

        from app.services.video import compose_video

        cover = tmp_path / "cover.png"
        illu = tmp_path / "illu.png"
        for path, color in ((cover, (250, 250, 250)), (illu, (240, 244, 250))):
            import pymupdf

            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 150), False)
            pix.set_rect(pix.irect, color)
            pix.save(str(path))
        audio = tmp_path / "a.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 4)

        return compose_video(
            segments=[{"speaker": "A", "text": "第一段。"}, {"speaker": "B", "text": "第二段。"}],
            timings=[RoundTiming(index=0, speaker="A", start=0.0, end=2.0),
                     RoundTiming(index=1, speaker="B", start=2.0, end=4.0)],
            audio_path=audio,
            audio_duration=4.0,
            cover_path=str(cover),
            figures=[],
            illustration_png=str(illu),
            work_dir=tmp_path / "work",
            output_path=tmp_path / "out.mp4",
            title="t",
            llm=None,
            preset_scenes=[{"index": 0, "image": "cover"}, {"index": 1, "image": "illustration"}],
            preset_assets={"cover": str(cover), "illustration": str(illu)},
        )

    def test_both_the_visual_plan_and_the_seam_report_survive(self, tmp_path):
        result = self._compose(tmp_path)
        assert "visual_plan" in result.qa, "画面计划概览又被整份替换吃掉了"
        assert "seams" in result.qa, "接缝审计没进 qa"
        seams = result.qa["seams"]
        assert seams["counts"]["seams"] == 1, seams
        assert seams["seams"][0]["at"] == pytest.approx(2.0, abs=0.2)
