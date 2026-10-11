"""响度：成片的**音量归一化**与**测量**（LUFS + true peak）。

这两件事必须在同一个模块里：编码时要**按测量值决定加多少增益**，
而「最后到底是多少 LUFS」又是成片自查要报的数 —— 两处不是同一把尺子的话，
归一化完再量一次又对不上（实测过一次：单遍 loudnorm 目标 -14、量出来 -15.7）。

**为什么不是「静音也照加」**：这一层只依赖标准库 + ffmpeg，不 import 任何
`video_*` 模块（`video_encode` 与 `video_qa` 都要用它，反向依赖会炸半初始化模块）。

## 实测事实（真实成片，不是推算）

| 集 | Integrated | True peak | 峰均差 | LRA |
|---|---|---|---|---|
| 100dce22 | **-18.1 LUFS** | -1.4 dBFS | 16.6 dB | 3.8 LU |
| 666adf5b | -18.4 LUFS | -1.4 dBFS | 16.9 dB | — |
| 4e594ee2.en | **-22.9 LUFS**（RMS） | -4.1 dBFS | 18.9 dB | — |

结论有两条，都是量出来的：

1. **同一套流水线出来的集，响度能差 4~5 LU**（中文 -18.1 vs 英文更低）——
   用户连续听两集要来回拧音量，这就是归一化真正要解决的问题。
2. **-14 LUFS 在一部分素材上「到不了」**：100dce22 要到 -14 得加 +4.14 dB，
   而它的 true peak 已经在 -1.4 dBFS —— 加完峰值 +2.7 dBFS，必然削波。
   ffmpeg 自己也这么说：两遍法的 JSON 里 `target_offset: 1.70`，
   最终 `output_i: -15.70`。**这不是估算，是 loudnorm 的实测回报。**
   要真的踩到 -14，只能上压缩（把最响的 1% 压下去，实测那 1% 只比峰值低 1.2 dB，
   也就是会被压得很勤），那是**会听得出来的音色改动**，不在「归一化」的范围里。

所以这里的策略是：**能到 -14 就到 -14，到不了就把 peak 守在 -1.5 dBFS 上停住**，
并把「差多少」记下来（`Loudness.target_offset_lu`）—— 差值就是这种素材的物理上限。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "TARGET_LUFS",
    "TARGET_TRUE_PEAK_DB",
    "TARGET_LRA",
    "OUTPUT_SAMPLE_RATE",
    "SILENCE_LUFS",
    "Loudness",
    "ffmpeg_available",
    "measure_loudness",
    "normalization_chain",
    "describe",
]

# 目标响度。取 -14 LUFS 是平台的通行做法（视频平台不会把**偏轻**的片子调响，
# 只会把偏响的压下来 —— 所以偏轻的集在同一个信息流里就是显得小声）。
TARGET_LUFS = -14.0
# true peak 上限。留 1.5 dB 给有损编码器（AAC）的过冲，避免解码后削波。
TARGET_TRUE_PEAK_DB = -1.5
# 允许的响度范围。太大 = 动得太狠（说话会一抽一抽的），11 LU 是广播常用值。
TARGET_LRA = 11.0
# loudnorm 内部按 192kHz 处理，输出统一到 48kHz（AAC 的常规取样率）。
OUTPUT_SAMPLE_RATE = 48000
# **数字静音不是「很轻的声音」**：ebur128 对纯静音报的是 `I: -70.0 LUFS`
# （它的下限），而不是 -inf。拿它当测量值去算增益会得到「+56dB」这种荒唐结果 ——
# 底噪会被一起放大。所以低于这条线一律当「测不到」，调用方退回原行为。
SILENCE_LUFS = -60.0

_JSON_BLOCK = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)
_SUMMARY_I = re.compile(r"I:\s*(-?\d+(?:\.\d+)?|-inf|nan)\s*LUFS")
_SUMMARY_PEAK = re.compile(r"Peak:\s*(-?\d+(?:\.\d+)?|-inf|nan)\s*dBFS")
_SUMMARY_LRA = re.compile(r"LRA:\s*(-?\d+(?:\.\d+)?|-inf|nan)\s*LU")


@dataclass(frozen=True)
class Loudness:
    """一次实测的结果。数值全是**测出来的**，没有任何一项是推算的。"""

    integrated_lufs: float
    true_peak_db: float | None = None
    lra_lu: float | None = None
    # 两遍法里 loudnorm 自己报的「离目标还差多少 LU」：> 0 表示**受 true peak 限制，
    # 到不了目标**。它是有信息量的一位数字（差值 ≈ 需要压缩多少才能到位）。
    target_offset_lu: float = 0.0
    mode: str = ""

    @property
    def below_target_lu(self) -> float:
        return max(0.0, TARGET_LUFS - self.integrated_lufs)

    def to_dict(self) -> dict:
        payload: dict[str, float | str] = {
            "integrated_lufs": round(self.integrated_lufs, 2),
            "target_lufs": TARGET_LUFS,
        }
        if self.true_peak_db is not None:
            payload["true_peak_db"] = round(self.true_peak_db, 2)
        if self.lra_lu is not None:
            payload["lra_lu"] = round(self.lra_lu, 2)
        if self.target_offset_lu:
            payload["target_offset_lu"] = round(self.target_offset_lu, 2)
        if self.mode:
            payload["mode"] = self.mode
        return payload


def ffmpeg_available() -> bool:
    """本模块自带的探测（不 import `video_encode`，避免分层反向依赖）。"""
    return shutil.which("ffmpeg") is not None


def _run(command: list[str], *, timeout: float = 600.0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, capture_output=True, timeout=timeout)


def _number(raw: str) -> float | None:
    """`-inf` / `nan`（数字静音）一律当「测不到」——**不要**当成 0。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("-inf"), float("inf")):
        return None
    return value


def measure_loudness(path: Path | str) -> Loudness | None:
    """用 `ebur128` 实测整条音轨的响度。测不出来（无音轨 / 数字静音 / 没装 ffmpeg）→ None。

    解析必须**只看 Summary 块**：ebur128 每 0.1 秒打一行进度，那行里同样有
    `I: … LUFS`，直接全文正则会取到「中途某一刻」的值（实测差 0.3 LU）。
    """
    if not ffmpeg_available() or not Path(path).exists():
        return None
    try:
        result = _run([
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
            "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-",
        ])
    except (OSError, subprocess.SubprocessError):
        return None
    text = result.stderr.decode("utf-8", "replace")
    if "Summary:" not in text:
        return None
    summary = text.rsplit("Summary:", 1)[1]
    integrated = _SUMMARY_I.search(summary)
    if integrated is None:
        return None
    value = _number(integrated.group(1))
    if value is None or value <= SILENCE_LUFS:
        return None
    peak = _SUMMARY_PEAK.search(summary)
    lra = _SUMMARY_LRA.search(summary)
    return Loudness(
        integrated_lufs=value,
        true_peak_db=_number(peak.group(1)) if peak else None,
        lra_lu=_number(lra.group(1)) if lra else None,
    )


def _probe(path: Path) -> dict | None:
    """两遍法的第一遍：让 loudnorm 自己报出输入的各项测量值 + 离目标差多少。"""
    try:
        result = _run([
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
            "-vn", "-af",
            f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK_DB}:LRA={TARGET_LRA}"
            ":print_format=json",
            "-f", "null", "-",
        ])
    except (OSError, subprocess.SubprocessError):
        return None
    text = result.stderr.decode("utf-8", "replace")
    match = _JSON_BLOCK.search(text)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def normalization_chain(path: Path | str) -> str | None:
    """给出**第二遍**要用的 loudnorm 滤镜链；测不出来时返回 None（调用方退回原行为）。

    用两遍法而不是单遍，理由是单遍的测量是流式估的：实测同一条素材单遍给 -15.7，
    两遍法带 `measured_*` 给同一个数，但两遍法**多回报一个 `target_offset`** ——
    「差多少、为什么差」是这种素材唯一的可查线索。参数里 `linear=true`：
    有足够余量时它就是一个**静态增益**（不做动态处理，说话不会一抽一抽的），
    余量不够时 loudnorm 自己退回动态模式（ffmpeg 的行为，不是我们选的）。
    """
    probe = _probe(Path(path))
    if probe is None:
        return None
    needed = ("input_i", "input_tp", "input_lra", "input_thresh")
    if any(probe.get(key) in (None, "", "nan") for key in needed):
        return None
    measured_i = _number(str(probe.get("input_i")))
    if measured_i is None:
        return None
    offset = _number(str(probe.get("offset", "0"))) or 0.0
    return (
        f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK_DB}:LRA={TARGET_LRA}:linear=true"
        f":measured_I={probe['input_i']}:measured_TP={probe['input_tp']}"
        f":measured_LRA={probe['input_lra']}:measured_thresh={probe['input_thresh']}"
        f":offset={offset}:print_format=summary"
    )


def describe(loudness: Loudness | None) -> str:
    """给日志/自查报告用的一句话。"""
    if loudness is None:
        return "响度：测不出来（无音轨或数字静音）"
    parts = [f"{loudness.integrated_lufs:.1f} LUFS（目标 {TARGET_LUFS:.0f}）"]
    if loudness.true_peak_db is not None:
        parts.append(f"true peak {loudness.true_peak_db:.1f} dBFS")
    if loudness.lra_lu is not None:
        parts.append(f"LRA {loudness.lra_lu:.1f} LU")
    if loudness.below_target_lu >= 1.0:
        parts.append(f"离目标还差 {loudness.below_target_lu:.1f} LU（受 true peak 限制）")
    return "响度：" + "，".join(parts)
