"""弹簧缓动：**「缓动曲线」而不是线性运动**。

这个模块只有一件事：把「带一点点过冲的阻尼弹簧」写成三种可复用的形式 ——
Python 求值（测试用）、ffmpeg 表达式（`overlay` 的 x/y 用）、JS 片段（页面
`window.seek` 用）。它们必须是**同一条曲线**，所以公式只写一遍。

## 为什么要弹簧

来自参考做法里那条明确的禁令：「用弹簧和缓动曲线，**不要线性运动**」。
我们之前有两处是**匀速**的，肉眼能看出来「机器在滑」而不是「东西被放到位」：

| 位置 | 改之前 | 改之后 |
|---|---|---|
| 强调行滑入（`POINT_SLIDE_SEC`） | `x = -W + W*min(1, t/0.4)` —— **匀速** 936px | 阻尼弹簧，过冲 2%（≈19px）后落定 |
| 聚光灯从上一处移过来（`FOCUS_MORPH_SEC`） | `cubic-bezier(.3,.7,.3,1)` —— 单调、无过冲 | `cubic-bezier(.34,1.28,.5,1)` —— 过冲后落定 |
| 逐句数字胶囊的「弹一下」（`CHIP_SPRING_SEC`） | 只有不透明度淡入 | 以胶囊中心为轴缩放 1 → **1.09** → 1（Tier B 逐帧） |

过冲量是**分开定**的，因为三处的「屏上距离」差了两个数量级：滑入要走 936px，
2% 就是 19px（再大就成了「甩过去」）；胶囊只有 30px 宽，9% 才 2.7px
（小到看不出在动的话，弹簧就白加了）。

## 公式

阻尼弹簧的阶跃响应：

```
p(t) = 1 - e^{-k t} · (cos(ω t) + (k/ω)·sin(ω t))
```

`p(0) = 0`、`p(∞) = 1`，中途**越过 1** 再回来 —— 过冲量 `A` 与阻尼比 `ζ` 的关系是
`A = e^{-πζ/√(1-ζ²)}`，峰值出现在 `t = π/ω`。所以给定「过冲多少、多久到峰值」，
两个参数就唯一确定了（`spring_kernel`）。**这三个数都不是调出来的手感**：
`A` 与峰值时刻（时长的 30%）是输入，`k`/`ω` 是解出来的。

胶囊那个「弹一下」用的是同一条弹簧的**冲激响应**（`t=0` 和 `t→∞` 都是 0，
峰值正好是 `A`）—— 因为胶囊的起点和终点必须是**同一个状态**（静止时的样子），
阶跃响应从 0 开始等于「从看不见长出来」，那不是「弹一下」。
"""

from __future__ import annotations

import math

from .design import SPRING_PEAK_RATIO

__all__ = [
    "spring_kernel",
    "spring_progress",
    "spring_bump",
    "bump_peak_time",
    "ffmpeg_ease",
    "js_spring",
]


def spring_kernel(
    seconds: float,
    *,
    overshoot: float,
    peak_ratio: float = SPRING_PEAK_RATIO,
    decay: float | None = None,
) -> tuple[float, float]:
    """解出阻尼弹簧的两个参数 `(k, ω_d)`：衰减率与**阻尼**角频率。

    响应是 `p(t) = 1 - e^{-kt}(cos ω_d t + (k/ω_d)·sin ω_d t)`，于是：

    - 峰值在 `t = π/ω_d`，所以「峰值出现在时长的 `peak_ratio` 处」直接定出 `ω_d`；
    - 过冲量 `A = e^{-k·π/ω_d}`，反解得 `ζ = |ln A| / √(π² + ln²A)`，再由
      `k = ζ·ω_n = ζ·ω_d/√(1-ζ²)` 得到衰减率。

    **「弹一下」那种用法要传 `decay`**（直接定衰减率，1/秒）而不是 `peak_ratio`：
    冲激响应的峰值时刻上限是 `1/k`（`ω_d→0` 时的极限），所以想让它「0.1 秒内弹到位、
    0.3 秒内落定」，可选的其实是 `k≈12`，而不是「峰值出现在时长的百分之几」。
    按 `peak_ratio` 硬调会得到一个还没落定就把序列播完的曲线（末帧停在过冲上，
    画面会**一直**歪着 —— 实测过：末帧残留 2.4% 就是 0.7px 的错位）。

    ⚠️ 这两个数**不能混用**：早先写成「用 `ω_n` 去 cos/sin」，量出来的过冲是
    8.6% 而目标是 2%（测试里那条「过冲量必须等于设定值」的断言就是为它写的）。
    """
    ratio = min(0.95, max(1e-4, float(overshoot)))
    log_a = abs(math.log(ratio))
    zeta = log_a / math.sqrt(math.pi ** 2 + log_a ** 2)
    if decay is not None:
        omega_d = max(1e-3, float(decay)) * math.sqrt(max(1e-6, 1.0 - zeta ** 2)) / zeta
    else:
        omega_d = math.pi / max(1e-3, peak_ratio * seconds)
    k = zeta * omega_d / math.sqrt(max(1e-6, 1.0 - zeta ** 2))
    return k, omega_d


def spring_progress(
    t: float,
    seconds: float,
    *,
    overshoot: float,
    peak_ratio: float = SPRING_PEAK_RATIO,
    decay: float | None = None,
) -> float:
    """阶跃响应 `p(t)`：0 → 1（中途略微越过 1）。`seconds` 之后基本就是 1。"""
    k, omega = spring_kernel(
        seconds, overshoot=overshoot, peak_ratio=peak_ratio, decay=decay
    )
    if t <= 0:
        return 0.0
    return 1.0 - math.exp(-k * t) * (
        math.cos(omega * t) + (k / omega) * math.sin(omega * t)
    )


def bump_peak_time(
    seconds: float,
    *,
    overshoot: float,
    peak_ratio: float = SPRING_PEAK_RATIO,
    decay: float | None = None,
) -> float:
    """冲激响应的峰值时刻（`d/dt [e^{-kt} sin ωt] = 0` → `t* = atan(ω/k)/ω`）。"""
    k, omega = spring_kernel(
        seconds, overshoot=overshoot, peak_ratio=peak_ratio, decay=decay
    )
    return math.atan(omega / k) / omega


def spring_bump(
    t: float,
    seconds: float,
    *,
    overshoot: float,
    peak_ratio: float = SPRING_PEAK_RATIO,
    decay: float | None = None,
) -> float:
    """冲激响应形状的「鼓包」：`t=0` 与 `t→∞` 都是 0，**峰值正好等于 `overshoot`**。

    用它做「弹一下」：起点与终点是同一个状态（静止），中途最大偏离 `overshoot`。
    """
    k, omega = spring_kernel(
        seconds, overshoot=overshoot, peak_ratio=peak_ratio, decay=decay
    )
    if t <= 0:
        return 0.0
    peak_t = math.atan(omega / k) / omega
    peak_value = math.exp(-k * peak_t) * math.sin(omega * peak_t)
    if peak_value <= 0:
        return 0.0
    return overshoot * math.exp(-k * t) * math.sin(omega * t) / peak_value


def ffmpeg_ease(
    seconds: float,
    *,
    overshoot: float,
    peak_ratio: float = SPRING_PEAK_RATIO,
    decay: float | None = None,
) -> str:
    """给 ffmpeg 表达式用的 `p(t)`（`t` 是 ffmpeg 的时间变量，单位秒）。

    只展开成 `exp/cos/sin` 与常数 —— `overlay` 的 `x`/`y` 支持这些函数，
    所以「滑入用弹簧」不需要逐帧抓图，零渲染成本。
    """
    k, omega = spring_kernel(
        seconds, overshoot=overshoot, peak_ratio=peak_ratio, decay=decay
    )
    return (
        f"(1-exp(-{k:.6f}*t)*(cos({omega:.6f}*t)"
        f"+{k / omega:.6f}*sin({omega:.6f}*t)))"
    )


def js_spring(
    seconds: float,
    *,
    overshoot: float,
    peak_ratio: float = SPRING_PEAK_RATIO,
    decay: float | None = None,
) -> str:
    """给页面 `window.seek(t)` 用的 JS 表达式（返回「弹一下」的偏移量，含峰值归一化）。

    与 `spring_bump` **同一条曲线**：`bump(t) = overshoot · e^{-kt} sin(ωt) / 峰值`。
    """
    k, omega = spring_kernel(
        seconds, overshoot=overshoot, peak_ratio=peak_ratio, decay=decay
    )
    peak_t = math.atan(omega / k) / omega
    peak_value = math.exp(-k * peak_t) * math.sin(omega * peak_t)
    scale = overshoot / peak_value if peak_value > 0 else 0.0
    return (
        f"(function(t){{ if (t <= 0) return 0;"
        f" return {scale:.6f} * Math.exp(-{k:.6f}*t) * Math.sin({omega:.6f}*t); }})"
    )
