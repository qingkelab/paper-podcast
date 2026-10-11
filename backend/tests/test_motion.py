"""弹簧缓动（`services/motion.py`）的测试。

这一层的意义全在**数值对不对**上：曲线本身没法「看着差不多就行」——
早先 `spring_kernel` 把自然频率当阻尼频率用（`cos(ω_n t)` 而不是 `cos(ω_d t)`），
设 2% 过冲实际量出 8.6%。所以这里第一条断言就是「过冲量必须等于设定值」，
而不是「看起来像弹簧」。

另外还盯着：**同一条曲线在三种形态下必须一致** —— Python 求值、ffmpeg 表达式、
页面里的 JS。三处各写一遍公式的话，改一处另外两处就成了两种动效
（滑入是弹簧、胶囊是另一条弹簧，肉眼不一定看得出，但那是 bug）。
"""

from __future__ import annotations

import math

import pytest

from app.services import motion
from app.services.design import (
    CHIP_SPRING_SEC,
    SPRING_PEAK_RATIO,
    SPRING_POP_DECAY,
    SPRING_POP_OVERSHOOT,
    SPRING_SLIDE_OVERSHOOT,
    SPRING_SLIDE_PEAK_RATIO,
)

SLIDE_SEC = 0.4  # = POINT_SLIDE_SEC，直接写字面量免得为一个常量把测试耦合进 design


def _peak(func, seconds: float, samples: int = 4000) -> float:
    return max(func(seconds * index / samples) for index in range(samples + 1))


class TestSpringCurve:
    def test_overshoot_is_exactly_what_we_asked_for(self):
        """滑入 2%、胶囊 9% —— 量出来的峰值必须就是这两个数（±0.5%）。"""
        for overshoot, seconds, peak_ratio in (
            (SPRING_SLIDE_OVERSHOOT, SLIDE_SEC, SPRING_SLIDE_PEAK_RATIO),
            (SPRING_POP_OVERSHOOT, CHIP_SPRING_SEC, SPRING_PEAK_RATIO),
        ):
            peak = _peak(
                lambda t: motion.spring_progress(
                    t, seconds, overshoot=overshoot, peak_ratio=peak_ratio
                ),
                seconds,
            )
            assert peak == pytest.approx(1.0 + overshoot, abs=0.005), (
                f"过冲设的是 {overshoot:.3f}，量出来是 {peak - 1:.3f}"
            )

    def test_progress_starts_at_zero_and_settles_at_one(self):
        for seconds, overshoot, peak_ratio in (
            (SLIDE_SEC, SPRING_SLIDE_OVERSHOOT, SPRING_SLIDE_PEAK_RATIO),
            (CHIP_SPRING_SEC, SPRING_POP_OVERSHOOT, SPRING_PEAK_RATIO),
        ):
            assert motion.spring_progress(0, seconds, overshoot=overshoot) == 0.0
            settled = motion.spring_progress(
                seconds, seconds, overshoot=overshoot, peak_ratio=peak_ratio
            )
            assert settled == pytest.approx(1.0, abs=0.01), "时长内必须已经落定"

    def test_progress_actually_overshoots(self):
        """不是「单调缓动」：中途必须真的越过 1（否则就只是换了个 ease）。"""
        values = [
            motion.spring_progress(
                SLIDE_SEC * i / 200,
                SLIDE_SEC,
                overshoot=SPRING_SLIDE_OVERSHOOT,
                peak_ratio=SPRING_SLIDE_PEAK_RATIO,
            )
            for i in range(201)
        ]
        assert max(values) > 1.0 + SPRING_SLIDE_OVERSHOOT * 0.8

    def test_peak_happens_where_it_was_asked(self):
        for seconds, overshoot, peak_ratio in (
            (SLIDE_SEC, SPRING_SLIDE_OVERSHOOT, SPRING_SLIDE_PEAK_RATIO),
            (CHIP_SPRING_SEC, SPRING_POP_OVERSHOOT, SPRING_PEAK_RATIO),
        ):
            best_t, best = 0.0, 0.0
            for index in range(4001):
                t = seconds * index / 4000
                value = motion.spring_progress(
                    t, seconds, overshoot=overshoot, peak_ratio=peak_ratio
                )
                if value > best:
                    best_t, best = t, value
            assert best_t == pytest.approx(seconds * peak_ratio, abs=seconds * 0.05)


class TestBump:
    """「弹一下」那条曲线：起点终点都是静止，中间偏离正好是设定值。"""

    def test_bump_starts_and_ends_at_rest(self):
        assert motion.spring_bump(0, CHIP_SPRING_SEC, overshoot=SPRING_POP_OVERSHOOT) == 0.0
        tail = motion.spring_bump(
            CHIP_SPRING_SEC,
            CHIP_SPRING_SEC,
            overshoot=SPRING_POP_OVERSHOOT,
            decay=SPRING_POP_DECAY,
        )
        assert abs(tail) < 0.01, f"序列播完时还歪着 {tail:.3f}（末帧会一直停在过冲上）"

    def test_bump_peak_equals_the_overshoot(self):
        peak = _peak(
            lambda t: motion.spring_bump(
                t,
                CHIP_SPRING_SEC,
                overshoot=SPRING_POP_OVERSHOOT,
                decay=SPRING_POP_DECAY,
            ),
            CHIP_SPRING_SEC,
        )
        assert peak == pytest.approx(SPRING_POP_OVERSHOOT, abs=0.002)

    def test_the_overshoot_is_still_there_at_30fps(self):
        """抓帧是 30fps 的：峰值必须落在**某一帧**上，否则抓出来的序列里没有过冲。

        实测（k=12）：第 2 帧 +7.7%、第 3 帧 +8.9% —— 峰值 9% 在第 3 帧上。
        """
        frames = [
            motion.spring_bump(
                step / 30,
                CHIP_SPRING_SEC,
                overshoot=SPRING_POP_OVERSHOOT,
                decay=SPRING_POP_DECAY,
            )
            for step in range(int(CHIP_SPRING_SEC * 30) + 1)
        ]
        assert max(frames) > SPRING_POP_OVERSHOOT * 0.85, (
            f"30fps 抓不到过冲：逐帧最大只有 {max(frames):.3f}"
        )
        assert abs(frames[-1]) < 0.01


class TestThreeFormsAgree:
    """Python / ffmpeg 表达式 / 页面 JS 必须是**同一条曲线**。"""

    def _ffmpeg_value(self, expression: str, t: float) -> float:
        namespace = {"exp": math.exp, "cos": math.cos, "sin": math.sin, "t": t}
        return float(eval(expression, {"__builtins__": {}}, namespace))  # noqa: S307

    def _js_bump(self, script: str, t: float):
        """把 `js_spring` 生成的那个 IIFE 里的常数抠出来，在 Python 里复算一遍。"""
        import re

        numbers = re.findall(r"-?\d+\.\d+", script)
        assert len(numbers) >= 3, f"JS 里的常数没抠出来：{script}"
        scale, decay, omega = (float(value) for value in numbers[:3])
        if t <= 0:
            return 0.0
        # JS 里是 `Math.exp(-12.0*t)`，所以抠出来的第三个之前那个数自带负号
        return scale * math.exp(decay * t) * math.sin(omega * t)

    def test_ffmpeg_expression_matches_python(self):
        for seconds, overshoot, peak_ratio in (
            (SLIDE_SEC, SPRING_SLIDE_OVERSHOOT, SPRING_SLIDE_PEAK_RATIO),
            (CHIP_SPRING_SEC, SPRING_POP_OVERSHOOT, SPRING_PEAK_RATIO),
        ):
            expression = motion.ffmpeg_ease(
                seconds, overshoot=overshoot, peak_ratio=peak_ratio
            )
            for step in range(0, 21):
                t = seconds * step / 20
                expected = motion.spring_progress(
                    t, seconds, overshoot=overshoot, peak_ratio=peak_ratio
                )
                assert self._ffmpeg_value(expression, t) == pytest.approx(expected, abs=1e-6)

    def test_js_spring_matches_python_bump(self):
        script = motion.js_spring(
            CHIP_SPRING_SEC, overshoot=SPRING_POP_OVERSHOOT, decay=SPRING_POP_DECAY
        )
        for step in range(0, 21):
            t = CHIP_SPRING_SEC * step / 20
            expected = motion.spring_bump(
                t,
                CHIP_SPRING_SEC,
                overshoot=SPRING_POP_OVERSHOOT,
                decay=SPRING_POP_DECAY,
            )
            assert self._js_bump(script, t) == pytest.approx(expected, abs=1e-6)

    def test_ffmpeg_expression_is_a_single_expression(self):
        """它会被塞进 `overlay=x='…'`，所以不能有换行、也不能有引号。"""
        expression = motion.ffmpeg_ease(SLIDE_SEC, overshoot=SPRING_SLIDE_OVERSHOOT)
        assert "\n" not in expression and "'" not in expression and '"' not in expression
        assert "exp(" in expression and "cos(" in expression and "sin(" in expression
