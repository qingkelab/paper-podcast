"""接缝审计：**逐切点**量「这个切得对不对」，只读、不改画面、不影响出片。

## 为什么要它

我们原来只有一条**下限**判据：`video_qa` 的「≥4 秒没有任何可见变化」。
它能回答「画面有没有卡住」，回答不了「切得顺不顺」——
而用户报过的那两次（「画面一直在反复切换、和语音对不上、有抖动」），
问题都不在「有没有动」，在**切点两侧的动有没有对上**。

这套判据来自 [HyperFrames](https://github.com/heygen-com/hyperframes) 的
`motion-doctrine` / `seam-craft`（Apache-2.0）—— 他们把「接缝」写成法则：

> How Scene A exits determines how Scene B enters：同轴、同向、速度匹配、
> **两边都还在动的时候切**；「停下来再切」「从静止开始」都是死拍子；
> 而且**叠化（crossfade）没有载体**，在他们的 anti-pattern 里排第一。

他们有一支 `seam-gate.mjs` 逐条数值强制。这里先做**只读的那一半**：
把现状量出来（退出时还在动吗？进入是从静止开始的吗？方向对不对得上？
这一下是硬切还是叠化？），**先不拿它拦出片** —— 判据得先在真实成片上标定过。

## 怎么量（都是确定性的，不看模型）

一次把整条片子解码成 **10fps 的 48×27 灰度小帧**（一趟 ffmpeg，跟 `video_qa` 一个路子），
然后在每个切点前后各取 `WINDOW_SEC` 的窗口逐对算：

- **变化量**：相邻两帧的灰度平均绝对差（0~255）。低于 `MOVE_THRESHOLD` 算「几乎没在动」；
- **位移方向**：差分图的质心在相邻两帧之间的位移，取主分量给方向
  （`x+` / `x-` / `y+` / `y-`）—— 这是「眼睛往哪儿被带着走」的近似；
- **是硬切还是渐变**：`单步最大变化 / 窗口内总变化`。一次跳完（≥`CUT_SHARE`）算硬切，
  摊在好几步上算渐变（我们的 dissolve 就是后者）。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .loudness import ffmpeg_available

__all__ = [
    "SeamReading",
    "SeamAudit",
    "audit_seams",
    "parse_cuts",
]


# 采样：10fps 足够看清 0.1 秒级的「还在不在动」，而整片解码是**一趟**
SAMPLE_FPS = 10
SAMPLE_SIZE = (48, 27)
WINDOW_SEC = 0.7
# 相邻帧灰度平均绝对差的阈值：低于它算「几乎没在动」。
# 标定见 `tests/test_seam_audit.py`（静止画面 <0.2；缓慢平移 2~6；硬切换 >50）
MOVE_THRESHOLD = 1.0
# 「切点附近」的半径：这一带里的变化算「这一下切换造成的」
NEAR_SEC = 0.25
# 位移方向的判定阈值（像素，48×27 的画布上）
DIRECTION_MIN_PX = 0.75
# 找平移方向时搜索的最大位移（像素）
SHIFT_RADIUS = 6
# 「位移确实解释了变化」的最小增益（残差要降这么多才算画面在平移）
DIRECTION_MIN_GAIN = 0.5
# 切点那一步要比场景自己的运动**突出多少倍**才算「看得出这一下」
SEAM_STANDS_OUT = 1.5


@dataclass(frozen=True)
class SeamReading:
    """一个切点的读数。`at` 是切点时刻（秒）。"""

    at: float
    # hard_cut  = 切点附近只有一步大变化（一次跳完）
    # dissolve  = 切点附近有好几步中等变化（摊开了 = 渐变/叠化）
    # seamless  = 两侧本来就在持续动（切点淹没在运动里，看不出「一下」）
    # static    = 整整 1.4 秒里几乎没有变化（两张一样的静态画面）
    kind: str
    change_before: float         # 切点前窗口的相邻帧变化（中位数）
    change_after: float
    moving_before: bool          # 退出时还在动
    moving_after: bool           # 进入不是从静止开始的
    axis_before: str | None      # "x+"/"x-"/"y+"/"y-"，测不出就是 None
    axis_after: str | None
    speed_ratio: float | None    # 进入 / 退出 的变化量之比（速度是否匹配）

    @property
    def direction_match(self) -> bool | None:
        if self.axis_before is None or self.axis_after is None:
            return None
        return self.axis_before == self.axis_after

    def issues(self) -> list[str]:
        """这一处接缝按 HyperFrames 那套法则**哪里不对**（只报告，不拦）。"""
        out: list[str] = []
        if not self.moving_before:
            out.append("退出时已经静止（死拍子：应该还在动的时候切）")
        if not self.moving_after:
            out.append("进入从静止开始（应该切进正在进行的运动里）")
        if self.kind == "dissolve":
            out.append("这一下是渐变/叠化（他们的法则里叠化没有载体）")
        if self.kind == "static":
            out.append("整整 1.4 秒几乎没有变化（两张静态画面之间的一次切换）")
        if self.direction_match is False:
            out.append(f"方向对不上：退出 {self.axis_before} / 进入 {self.axis_after}")
        if self.speed_ratio is not None and (self.speed_ratio > 3.0 or self.speed_ratio < 0.33):
            out.append(f"两侧速度差得多（进入/退出 = {self.speed_ratio:.2f}）")
        return out

    def to_dict(self) -> dict:
        return {
            "at": round(self.at, 2),
            "kind": self.kind,
            "moving_before": self.moving_before,
            "moving_after": self.moving_after,
            "axis_before": self.axis_before,
            "axis_after": self.axis_after,
            "speed_ratio": None if self.speed_ratio is None else round(self.speed_ratio, 2),
            "issues": self.issues(),
        }


@dataclass
class SeamAudit:
    """整条片子的接缝读数 + 一句摘要。"""

    seams: list[SeamReading] = field(default_factory=list)
    notes: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {
            "seams": len(self.seams),
            "settled_before": sum(1 for s in self.seams if not s.moving_before),
            "rest_after": sum(1 for s in self.seams if not s.moving_after),
            "dissolve": sum(1 for s in self.seams if s.kind == "dissolve"),
            "axis_flip": sum(1 for s in self.seams if s.direction_match is False),
        }

    def summary(self) -> str:
        c = self.counts
        if not c["seams"]:
            return "接缝审计：没有可量的切点"
        return (
            f"接缝审计：{c['seams']} 处切点；退出时已静止 {c['settled_before']}、"
            f"进入从静止开始 {c['rest_after']}、渐变/叠化 {c['dissolve']}、"
            f"方向对不上 {c['axis_flip']}"
        )

    def to_dict(self) -> dict:
        return {"counts": self.counts, "seams": [s.to_dict() for s in self.seams],
                "notes": list(self.notes)}


# --------------------------------------------------------------------------
# 解码与度量
# --------------------------------------------------------------------------


def _decode_gray(path: Path | str, *, fps: int = SAMPLE_FPS) -> tuple[list[bytes], int, int]:
    """一趟 ffmpeg 把整片解成灰度小帧（返回 `(frames, w, h)`）。

    为什么先缩到 48×27：接缝判据要的是「有没有在动、往哪儿动」，
    不是细节。缩到这个尺寸之后一帧 1296 字节，10 分钟片子 ≈ 7MB，全在内存里，
    比每个切点各起一次 ffmpeg 便宜得多（HyperFrames 的 PSNR 教训：每个检查点
    spawn 一次 ffmpeg 会**每次从第 0 帧重新解码**，复杂度随检查点数平方增长）。
    """
    width, height = SAMPLE_SIZE
    if not ffmpeg_available():
        return [], width, height
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(path),
            "-vf", f"fps={fps},scale={width}:{height}:flags=area,format=gray",
            "-f", "rawvideo", "-",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return [], width, height
    size = width * height
    frames = [result.stdout[i:i + size] for i in range(0, len(result.stdout) - size + 1, size)]
    return frames, width, height


def _mean_abs_diff(a: bytes, b: bytes) -> float:
    """两帧的灰度平均绝对差（0~255）。"""
    total = 0
    for index in range(len(a)):
        delta = a[index] - b[index]
        total += delta if delta >= 0 else -delta
    return total / max(len(a), 1)


def _best_shift(before: bytes, after: bytes, width: int, height: int,
                *, radius: int = SHIFT_RADIUS) -> tuple[int, int, float]:
    """`after` 相对 `before` 的**最佳平移** `(dx, dy)`，以及对齐后的残差。

    为什么不用「差分图的质心」：一个**平移中的物体**在相邻两帧的差分里是**两条边**
    （前缘和后缘），它们的质心几乎不动 —— 实测拿它判方向，同向平移被判成「x-→x+」、
    反向平移也被判成「x+→x-」，符号是乱的。改成「找一个位移，让两帧最像」就稳了：
    48×27 的画布上搜 ±6 像素、81 个候选位移，一对帧十万次整数运算，可以忽略。
    """
    best = (0, 0, float("inf"))
    # 按 |位移| 从小到大试：**同分时取最小的那个**。两张纯色画面每个位移的残差都是 0，
    # 不这样排的话会返回搜索起点 (-6,-6)，凭空报出一个方向（标定时就踩到了）。
    candidates = sorted(
        ((dx, dy) for dy in range(-radius, radius + 1) for dx in range(-radius, radius + 1)),
        key=lambda pair: (abs(pair[0]) + abs(pair[1]), abs(pair[0]), abs(pair[1])),
    )
    for dx, dy in candidates:
            total = 0
            count = 0
            for y in range(height):
                sy = y + dy
                if sy < 0 or sy >= height:
                    continue
                row, srow = y * width, sy * width
                for x in range(width):
                    sx = x + dx
                    if sx < 0 or sx >= width:
                        continue
                    delta = after[row + x] - before[srow + sx]
                    total += delta if delta >= 0 else -delta
                    count += 1
            if not count:
                continue
            residual = total / count
            if residual < best[2]:
                best = (dx, dy, residual)
    return best


def _axis(dx: float, dy: float) -> str | None:
    """位移 → 方向标签。位移太小（画面不是靠平移在变）就返回 None。"""
    if abs(dx) < DIRECTION_MIN_PX and abs(dy) < DIRECTION_MIN_PX:
        return None
    if abs(dx) >= abs(dy):
        return "x+" if dx > 0 else "x-"
    return "y+" if dy > 0 else "y-"


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _read_seam(frames: list[bytes], *, at: float, width: int, height: int,
               fps: int = SAMPLE_FPS) -> SeamReading | None:
    """量一个切点。帧不够（切点在片子最边上）就返回 None。

    窗口分三段看（这是标定之后定下来的分法）：

    | 段落 | 范围 | 它回答的问题 |
    |---|---|---|
    | 切点附近 | ±`NEAR_SEC`(0.25s) | 这一下切换**跳**了几步？（1 步=硬切，多步=渐变） |
    | 退出侧 | −0.7 ~ −0.25s | 这个场景**自己**在不在动 |
    | 进入侧 | +0.25 ~ +0.7s | 下一个场景**自己**在不在动 |

    关键：`change_before/after` **只取外侧**（不含切点附近）—— 把切换那一下算进去，
    量的就不是「场景自己在不在动」，而是「切换有多大」，那是另一个问题。
    """
    span = int(WINDOW_SEC * fps)
    near = max(1, int(NEAR_SEC * fps))
    center = int(round(at * fps))
    if center - span < 1 or center + span >= len(frames):
        return None

    steps: list[tuple[float, int]] = []       # (变化量, 这一步的中点相对切点的帧数)
    for i in range(center - span, center + span):
        change = _mean_abs_diff(frames[i], frames[i + 1])
        steps.append((change, i + 0.5 - center))
    if not steps:
        return None

    seam_steps = [change for change, offset in steps if abs(offset) <= near]
    before_steps = [change for change, offset in steps if -span <= offset < -near]
    after_steps = [change for change, offset in steps if near < offset <= span]
    if not seam_steps or not before_steps or not after_steps:
        return None

    change_before = _median(before_steps)
    change_after = _median(after_steps)
    # 判「这一下切换看不看得出来」要比的是**场景自己的运动幅度**，不是绝对阈值：
    # 两侧一直在动、而切点那一步并不比平时更大 → 切点淹在运动里（seamless）；
    # 一侧在动、另一侧静止（比如平移切到静止画面）→ 那一下照样看得出来。
    # 实测：只看「有没有一侧在动」会把「平移→静止」错判成 seamless。
    background = max(change_before, change_after)
    seam_peak = max(seam_steps)
    # 「切点附近」里有几步是**真的在切**：门槛取「场景自己的运动幅度」和绝对阈值里更大的那个
    # —— 否则「平移 → 静止」里那几步正常平移（5.8）也会被算成切换的步数，
    # 于是硬切被判成渐变（标定时逮到的）。
    seam_ref = max(MOVE_THRESHOLD, background * SEAM_STANDS_OUT)
    over_seam = sum(1 for change in seam_steps if change >= seam_ref)
    if seam_peak <= max(background * SEAM_STANDS_OUT, MOVE_THRESHOLD):
        kind = "seamless" if background >= MOVE_THRESHOLD else "static"
    elif over_seam <= 1:
        kind = "hard_cut"
    else:
        kind = "dissolve"

    def direction(first: int, last: int) -> str | None:
        """外侧窗口里，靠位移量的方向（取窗口首尾两帧的最佳平移）。

        ⚠️ 符号要**取反**：`_best_shift` 给的是「把后一帧挪回前一帧」需要的位移，
        而我们要报的是**画面自己在往哪走**。不取反的话，向右的平移会被报成 `x-`
        （标定时逮到的）。
        """
        if last <= first:
            return None
        zero = _mean_abs_diff(frames[first], frames[last])
        dx, dy, residual = _best_shift(frames[first], frames[last], width, height)
        if zero - residual < DIRECTION_MIN_GAIN:
            return None      # 位移并没有让两帧更像 → 画面不是靠平移在变
        return _axis(float(-dx), float(-dy))

    speed_ratio = (change_after / change_before) if change_before > 0.05 else None
    return SeamReading(
        at=at,
        kind=kind,
        change_before=round(change_before, 3),
        change_after=round(change_after, 3),
        moving_before=change_before >= MOVE_THRESHOLD,
        moving_after=change_after >= MOVE_THRESHOLD,
        axis_before=direction(center - span, center - near),
        axis_after=direction(center + near, center + span),
        speed_ratio=speed_ratio,
    )


def audit_seams(video: Path | str, cuts: list[float]) -> SeamAudit:
    """量出每个切点的读数。**绝不抛异常** —— 审计失败不该影响任何事。"""
    path = Path(video)
    notes: list[str] = []
    if not path.exists():
        return SeamAudit(notes=("视频文件不存在",))
    frames, width, height = _decode_gray(path)
    if not frames:
        return SeamAudit(notes=("抽不到帧（ffmpeg 不可用或文件损坏）",))
    seams: list[SeamReading] = []
    skipped = 0
    for at in cuts:
        reading = _read_seam(frames, at=float(at), width=width, height=height)
        if reading is None:
            skipped += 1
            continue
        seams.append(reading)
    if skipped:
        notes.append(f"{skipped} 处切点太靠边（片子头尾 0.7 秒内），没量")
    return SeamAudit(seams=seams, notes=tuple(notes))


# --------------------------------------------------------------------------
# 命令行：python -m app.services.seam_audit <mp4> [切点,…]
# --------------------------------------------------------------------------


def parse_cuts(text: str) -> list[float]:
    return [float(part) for part in text.replace("，", ",").split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    video = args[0]
    cuts = parse_cuts(args[1]) if len(args) > 1 else []
    if not cuts:
        print("需要切点列表：python -m app.services.seam_audit <mp4> 3.55,9.03,…")
        return 2
    audit = audit_seams(video, cuts)
    print(audit.summary())
    for note in audit.notes:
        print(f"  · {note}")
    for seam in audit.seams:
        mark = "✅" if not seam.issues() else "⚠️"
        print(
            f"{mark} t={seam.at:7.2f}s  {seam.kind:<9} "
            f"动（前 {seam.change_before:5.2f} / 后 {seam.change_after:5.2f}）"
            f"  方向 {seam.axis_before or '—'} → {seam.axis_after or '—'}"
        )
        for issue in seam.issues():
            print(f"      - {issue}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
