"""豆包语音播客（PodcastTTS）客户端。

真实接口：`wss://openspeech.bytedance.com/api/v3/sami/podcasttts`

⚠️ 方案文档里写的 `/api/v3/tts/submit` + `/api/v3/tts/query` **并不存在**，
本模块按官方实际协议实现。要点：

1. 传输是 **WebSocket 长连接**，不是 HTTP 提交+轮询。
2. 帧格式是自定义二进制协议：4 字节头 + 可选 sequence + event + session_id +
   connect_id + payload。事件码 360=轮次开始、361=音频数据、362=轮次结束、
   152=会话结束。
3. 音频是**按轮次流式返回**的裸字节（361 事件），需要按顺序拼接成完整音频文件。
4. 双主播：`speaker_info.speakers` 必须正好两个音色 ID。
5. 本产品用 `action=3`（from_script）模式：脚本由我们自己的大模型生成后再送进来合成，
   而不是让 TTS 服务自己写稿——这样才能保证解读深度与 Prompt 可控。

鉴权（二选一）：
- 新版：`X-Api-Key`
- 旧版：`X-Api-App-Id` + `X-Api-App-Key` + `X-Api-Access-Key`
两者都需要 `X-Api-Resource-Id`（播客为 `volc.service_type.10050`）和 `X-Api-Connect-Id`。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import struct
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from ..config import Settings

logger = logging.getLogger(__name__)

# ---- 协议常量 ----
VERSION_1 = 0x1
HEADER_UNITS = 1  # 头长度 = 1 * 4 字节

MSG_FULL_CLIENT = 0x1
MSG_AUDIO_ONLY_CLIENT = 0x2
MSG_FULL_SERVER = 0x9
MSG_AUDIO_ONLY_SERVER = 0xB
MSG_ERROR = 0xF

FLAG_NO_SEQUENCE = 0x0
FLAG_WITH_EVENT = 0x4

SER_NONE = 0x0
SER_JSON = 0x1

COMP_NONE = 0x0
COMP_GZIP = 0x1

# ---- 事件码 ----
EV_START_CONNECTION = 1
EV_FINISH_CONNECTION = 2
EV_CONNECTION_STARTED = 50
EV_CONNECTION_FAILED = 51
EV_CONNECTION_ENDED = 52

EV_START_SESSION = 100
EV_FINISH_SESSION = 102
EV_SESSION_STARTED = 150
EV_SESSION_FINISHED = 152
EV_SESSION_FAILED = 153

EV_ROUND_STARTED = 360
EV_ROUND_AUDIO = 361
EV_ROUND_FINISHED = 362
EV_USAGE = 154  # 每轮合成后的 token 用量，仅用于日志

# 这些事件不带 session_id 字段
_NO_SESSION_ID_EVENTS = {
    EV_START_CONNECTION,
    EV_FINISH_CONNECTION,
    EV_CONNECTION_STARTED,
    EV_CONNECTION_FAILED,
    EV_CONNECTION_ENDED,
}
_CONNECT_ID_EVENTS = {EV_CONNECTION_STARTED, EV_CONNECTION_FAILED, EV_CONNECTION_ENDED}

# 播客动作
ACTION_FROM_SOURCE = 0
ACTION_FROM_SCRIPT = 3
ACTION_FROM_PROMPT = 4


class PodcastTTSError(Exception):
    """音频合成失败，message 直接展示给用户。"""


# --------------------------------------------------------------------------
# 帧编解码
# --------------------------------------------------------------------------


def _has_sequence_field(msg_type: int, flags: int) -> bool:
    if msg_type == MSG_AUDIO_ONLY_CLIENT:
        return False
    return flags in (0x1, 0x2, 0x3)


def build_event_frame(
    event: int,
    *,
    session_id: str = "",
    connect_id: str = "",
    payload: bytes = b"",
    msg_type: int = MSG_FULL_CLIENT,
    serialization: int = SER_JSON,
    compression: int = COMP_NONE,
) -> bytes:
    buf = bytearray()
    buf.append((VERSION_1 << 4) | HEADER_UNITS)
    buf.append((msg_type << 4) | FLAG_WITH_EVENT)
    buf.append((serialization << 4) | compression)
    buf.append(0x00)

    buf += struct.pack(">i", event)

    if event not in _NO_SESSION_ID_EVENTS:
        encoded = session_id.encode("utf-8")
        buf += struct.pack(">I", len(encoded))
        buf += encoded

    if event in _CONNECT_ID_EVENTS:
        encoded = connect_id.encode("utf-8")
        buf += struct.pack(">I", len(encoded))
        buf += encoded

    if compression == COMP_GZIP and payload:
        payload = gzip.compress(payload)

    buf += struct.pack(">I", len(payload))
    buf += payload
    return bytes(buf)


@dataclass
class ServerFrame:
    msg_type: int
    flags: int
    serialization: int
    compression: int
    has_event: bool
    event: int
    session_id: str
    connect_id: str
    error_code: int
    payload: bytes


def parse_server_frame(data: bytes) -> ServerFrame:
    if len(data) < 8:
        raise PodcastTTSError(f"收到过短的协议帧（{len(data)} 字节）")

    header_units = data[0] & 0x0F
    if header_units <= 0:
        raise PodcastTTSError("协议帧头长度非法")
    header_size = header_units * 4
    if len(data) < header_size:
        raise PodcastTTSError("协议帧头不完整")

    msg_type = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    serialization = (data[2] >> 4) & 0x0F
    compression = data[2] & 0x0F

    offset = header_size
    if _has_sequence_field(msg_type, flags):
        offset += 4  # sequence，本项目不使用

    has_event = bool(flags & FLAG_WITH_EVENT)
    event = 0
    session_id = ""
    connect_id = ""

    if has_event:
        if len(data) < offset + 4:
            raise PodcastTTSError("协议帧缺少 event 字段")
        event = struct.unpack(">i", data[offset : offset + 4])[0]
        offset += 4

        if event not in _NO_SESSION_ID_EVENTS:
            if len(data) < offset + 4:
                raise PodcastTTSError("协议帧缺少 session_id 长度")
            length = struct.unpack(">I", data[offset : offset + 4])[0]
            offset += 4
            if len(data) < offset + length:
                raise PodcastTTSError("协议帧 session_id 不完整")
            session_id = data[offset : offset + length].decode("utf-8", "replace")
            offset += length

        if event in _CONNECT_ID_EVENTS:
            if len(data) < offset + 4:
                raise PodcastTTSError("协议帧缺少 connect_id 长度")
            length = struct.unpack(">I", data[offset : offset + 4])[0]
            offset += 4
            if len(data) < offset + length:
                raise PodcastTTSError("协议帧 connect_id 不完整")
            connect_id = data[offset : offset + length].decode("utf-8", "replace")
            offset += length

    error_code = 0
    if msg_type == MSG_ERROR:
        if len(data) < offset + 4:
            raise PodcastTTSError("错误帧缺少 error_code")
        error_code = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4

    if len(data) < offset + 4:
        raise PodcastTTSError("协议帧缺少 payload 长度")
    payload_len = struct.unpack(">I", data[offset : offset + 4])[0]
    offset += 4

    payload = data[offset : offset + payload_len]
    if compression == COMP_GZIP and payload:
        try:
            payload = gzip.decompress(payload)
        except OSError as exc:
            raise PodcastTTSError(f"payload 解压失败：{exc}") from exc

    return ServerFrame(
        msg_type=msg_type,
        flags=flags,
        serialization=serialization,
        compression=compression,
        has_event=has_event,
        event=event,
        session_id=session_id,
        connect_id=connect_id,
        error_code=error_code,
        payload=payload,
    )


# --------------------------------------------------------------------------
# 请求构造
# --------------------------------------------------------------------------


def build_session_payload(
    *,
    input_id: str,
    script_segments: list[dict[str, Any]],
    voice_a: str,
    voice_b: str,
    speech_rate: int = 0,
    fmt: str = "mp3",
    sample_rate: int = 24000,
    head_music: bool = True,
    tail_music: bool = True,
    aigc_watermark: bool = True,
) -> dict[str, Any]:
    """构造 from_script 模式的 StartSession 请求体。"""
    return {
        "input_id": input_id,
        "action": ACTION_FROM_SCRIPT,
        "nlp_texts": [
            {"speaker": voice_a if seg["speaker"] == "A" else voice_b, "text": seg["text"]}
            for seg in script_segments
        ],
        "use_head_music": head_music,
        "use_tail_music": tail_music,
        "aigc_watermark": aigc_watermark,
        "input_info": {
            "return_audio_url": False,
            "only_nlp_text": False,
            "strict_audit": False,
        },
        "audio_config": {
            "format": fmt,
            "sample_rate": sample_rate,
            "speech_rate": speech_rate,
        },
        "speaker_info": {
            "random_order": False,
            "speakers": [voice_a, voice_b],
        },
    }


def auth_headers(settings: Settings, connect_id: str) -> dict[str, str]:
    headers = {
        "X-Api-Resource-Id": settings.podcast_resource_id,
        "X-Api-Connect-Id": connect_id,
    }
    if settings.doubao_api_key:
        headers["X-Api-Key"] = settings.doubao_api_key
    elif settings.doubao_access_key and settings.doubao_app_id:
        headers["X-Api-App-Id"] = settings.doubao_app_id
        headers["X-Api-App-Key"] = settings.doubao_app_key
        headers["X-Api-Access-Key"] = settings.doubao_access_key
    else:
        raise PodcastTTSError("未配置豆包语音鉴权信息")
    return headers


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


@dataclass
class SynthesisResult:
    audio_path: Path
    duration_sec: float | None
    bytes_written: int
    task_id: str | None = None
    finished_round: int = -1


@dataclass
class ProgressCallback:
    """把合成进度回传给流水线，用于更新前端进度条。"""

    on_round: Callable[[int, str], None] | None = None


class PodcastTTSClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.mock = settings.tts_mode == "mock"
        if self.mock:
            logger.warning("豆包语音未配置，音频合成将使用 Mock 音频")

    async def synthesize(
        self,
        *,
        segments: list[dict[str, Any]],
        voice_a: str,
        voice_b: str,
        output_path: Path,
        on_round: Callable[[int, str], None] | None = None,
        speech_rate: int = 0,
    ) -> SynthesisResult:
        if self.mock:
            return await self._synthesize_mock(
                segments=segments, output_path=output_path, on_round=on_round
            )
        return await self._synthesize_real(
            segments=segments,
            voice_a=voice_a,
            voice_b=voice_b,
            output_path=output_path,
            on_round=on_round,
            speech_rate=speech_rate,
        )

    # ---------- 真实调用 ----------

    async def _synthesize_real(
        self,
        *,
        segments: list[dict[str, Any]],
        voice_a: str,
        voice_b: str,
        output_path: Path,
        on_round: Callable[[int, str], None] | None,
        speech_rate: int = 0,
    ) -> SynthesisResult:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover
            raise PodcastTTSError("服务端缺少 websockets 依赖") from exc

        settings = self.settings
        connect_id = f"podcast-{uuid.uuid4().hex[:16]}"
        session_id = f"session-{uuid.uuid4().hex[:16]}"
        input_id = output_path.stem

        payload = build_session_payload(
            input_id=input_id,
            script_segments=segments,
            voice_a=voice_a,
            voice_b=voice_b,
            speech_rate=speech_rate,
        )

        headers = auth_headers(settings, connect_id)
        logger.info(
            "提交播客合成任务：%d 段脚本，音色 %s / %s", len(segments), voice_a, voice_b
        )

        connect_kwargs: dict[str, Any] = {
            "max_size": None,
            "ping_interval": 20,
            "ping_timeout": 20,
            "open_timeout": 30,
        }

        try:
            ws = await self._connect(websockets, settings.podcast_ws_url, headers, connect_kwargs)
        except Exception as exc:
            raise PodcastTTSError(f"连接豆包语音服务失败：{exc}") from exc

        audio_buffer = bytearray()
        task_id: str | None = None
        finished_round = -1

        try:
            await ws.send(build_event_frame(EV_START_CONNECTION, payload=b"{}"))
            frame = await self._recv_event(ws, expect=EV_CONNECTION_STARTED, timeout=30)
            if frame.error_code:
                raise PodcastTTSError(f"建立连接失败，错误码 {frame.error_code}")

            await ws.send(
                build_event_frame(
                    EV_START_SESSION,
                    session_id=session_id,
                    payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                )
            )
            started = await self._recv_event(ws, expect=EV_SESSION_STARTED, timeout=30)
            # task_id 用于断点续传（retry_info），服务端通常不在 150 里给，退回用 session_id
            task_id = _safe_json(started.payload).get("task_id") or session_id

            # ⚠️ 关键：收到 150 之后必须**立刻**发送 FinishSession(102)，声明「输入已发完」。
            # 服务端收到它才会在合成完毕后发出 152（会话结束）。
            # 之前把这一步放在 finally 里（循环都结束了才发），结果是永远等不到 152，
            # 每次都误判为超时，并把已经收到的完整音频整个丢掉。
            await ws.send(
                build_event_frame(EV_FINISH_SESSION, session_id=session_id, payload=b"{}")
            )

            # 用 running_loop 而不是 get_event_loop()：后者在 Python 3.12+ 的协程里
            # 已被标记为不推荐，行为也将在未来版本改变。
            loop = asyncio.get_running_loop()
            deadline = loop.time() + settings.podcast_timeout_sec
            current_round = -1
            current_round_type = ""
            script_rounds_done = 0
            expected_script_rounds = len(segments)

            while True:
                if loop.time() > deadline:
                    raise PodcastTTSError(
                        f"播客合成超时（超过 {int(settings.podcast_timeout_sec)} 秒）"
                    )

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=120)
                except asyncio.TimeoutError as exc:
                    raise PodcastTTSError("等待播客音频数据超时") from exc

                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                frame = parse_server_frame(raw)

                if frame.msg_type == MSG_ERROR:
                    detail = frame.payload.decode("utf-8", "replace")[:300]
                    raise PodcastTTSError(
                        f"豆包语音返回错误（错误码 {frame.error_code}）：{detail}"
                    )

                if not frame.has_event:
                    continue

                if frame.event == EV_ROUND_STARTED:
                    meta = _safe_json(frame.payload)
                    current_round_type = str(meta.get("round_type") or "")
                    raw_round_id = meta.get("round_id")
                    try:
                        current_round = int(raw_round_id)
                    except (TypeError, ValueError):
                        current_round = current_round + 1
                    # 只有脚本轮次才回调进度：片头音乐(-1)/片尾音乐(9999)/水印(10000)
                    # 不是用户能感知的「第几段」，混进去会让进度条乱跳。
                    if on_round and not current_round_type:
                        on_round(script_rounds_done, str(meta.get("speaker") or ""))

                elif frame.event == EV_ROUND_AUDIO:
                    if frame.payload:
                        audio_buffer += frame.payload

                elif frame.event == EV_ROUND_FINISHED:
                    # 注意：362 的 payload 里**没有** round_id，只有 audio_duration /
                    # start_time / end_time，所以轮次编号只能从 360 里跟踪。
                    meta = _safe_json(frame.payload)
                    if meta.get("is_error"):
                        raise PodcastTTSError(
                            f"第 {script_rounds_done + 1} 段音频合成失败："
                            f"{meta.get('message') or '服务端未给出原因'}"
                        )
                    if not current_round_type:
                        finished_round = current_round
                        script_rounds_done += 1
                    logger.debug(
                        "轮次完成：round=%s type=%r 时长=%ss（累计脚本轮次 %d/%d）",
                        current_round,
                        current_round_type or "script",
                        meta.get("audio_duration"),
                        script_rounds_done,
                        expected_script_rounds,
                    )

                elif frame.event == EV_USAGE:
                    usage = _safe_json(frame.payload).get("usage") or {}
                    logger.info(
                        "播客合成用量：输出音频 tokens=%s", usage.get("output_audio_tokens")
                    )

                elif frame.event == EV_SESSION_FAILED:
                    meta = _safe_json(frame.payload)
                    raise PodcastTTSError(
                        f"播客合成会话失败：{meta.get('message') or '服务端未给出原因'}"
                    )

                elif frame.event == EV_SESSION_FINISHED:
                    break

        finally:
            try:
                await ws.send(build_event_frame(EV_FINISH_CONNECTION, payload=b"{}"))
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass

        if not audio_buffer:
            raise PodcastTTSError("豆包语音未返回任何音频数据")
        if script_rounds_done < expected_script_rounds:
            logger.warning(
                "脚本共 %d 段，但只收到 %d 段音频，产物可能不完整",
                expected_script_rounds,
                script_rounds_done,
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(bytes(audio_buffer))

        return SynthesisResult(
            audio_path=output_path,
            duration_sec=probe_duration(output_path),
            bytes_written=len(audio_buffer),
            task_id=task_id,
            finished_round=finished_round,
        )

    @staticmethod
    async def _connect(websockets, url: str, headers: dict[str, str], kwargs: dict[str, Any]):
        """兼容 websockets 新旧版本的 header 参数名。"""
        try:
            return await websockets.connect(url, additional_headers=headers, **kwargs)
        except TypeError:
            return await websockets.connect(url, extra_headers=headers, **kwargs)

    @staticmethod
    async def _recv_event(ws, *, expect: int, timeout: float) -> ServerFrame:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        frame = parse_server_frame(raw)
        if frame.msg_type == MSG_ERROR:
            raise PodcastTTSError(
                f"豆包语音返回错误（错误码 {frame.error_code}）："
                f"{frame.payload.decode('utf-8', 'replace')[:300]}"
            )
        if frame.has_event and frame.event in (EV_CONNECTION_FAILED, EV_SESSION_FAILED):
            raise PodcastTTSError(
                f"豆包语音会话建立失败：{frame.payload.decode('utf-8', 'replace')[:300]}"
            )
        if frame.has_event and frame.event != expect:
            logger.debug("期望事件 %d，实际收到 %d，继续等待", expect, frame.event)
        return frame

    # ---------- Mock ----------

    async def _synthesize_mock(
        self,
        *,
        segments: list[dict[str, Any]],
        output_path: Path,
        on_round: Callable[[int, str], None] | None,
    ) -> SynthesisResult:
        """生成一段可播放的双音色占位音频（WAV）。

        用途：零密钥时让「上传→解读→脚本→播放」整条链路可演示。
        用两个不同基频的声音区分主播 A / B，段间留停顿。
        """
        total_chars = sum(len(s["text"]) for s in segments)
        # 不生成完整时长，避免演示时等待过久
        duration = min(max(total_chars / 250 * 60, 8.0), 30.0)

        sample_rate = 24000
        frames = bytearray()
        seg_duration = duration / max(len(segments), 1)

        for index, segment in enumerate(segments):
            if on_round:
                on_round(index, segment.get("speaker", "A"))
            base_freq = 220.0 if segment["speaker"] == "A" else 294.0
            frames += _tone(
                freq=base_freq,
                seconds=max(seg_duration - 0.25, 0.15),
                sample_rate=sample_rate,
                amplitude=0.22,
            )
            frames += b"\x00\x00" * int(0.25 * sample_rate)  # 段间停顿
            await asyncio.sleep(0.05)  # 让前端能看到轮次推进

        output_path = output_path.with_suffix(".wav")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(bytes(frames))

        return SynthesisResult(
            audio_path=output_path,
            duration_sec=probe_duration(output_path),
            bytes_written=output_path.stat().st_size,
            task_id=f"mock-{uuid.uuid4().hex[:12]}",
            finished_round=len(segments) - 1,
        )


def _tone(*, freq: float, seconds: float, sample_rate: int, amplitude: float = 0.2) -> bytes:
    """生成一段带淡入淡出的正弦波 PCM16。"""
    count = int(seconds * sample_rate)
    fade = max(int(0.02 * sample_rate), 1)
    out = bytearray()
    for i in range(count):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > count - fade:
            env = max((count - i) / fade, 0.0)
        value = int(amplitude * env * 32767 * math.sin(2 * math.pi * freq * i / sample_rate))
        out += struct.pack("<h", value)
    return bytes(out)


def _safe_json(payload: bytes) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        data = json.loads(payload.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


# --------------------------------------------------------------------------
# 音频时长探测
# --------------------------------------------------------------------------


def probe_duration(path: Path) -> float | None:
    """估算音频时长。支持 WAV（精确）与 MP3（按 CBR 估算）。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
    except OSError:
        return None

    if head[:4] == b"RIFF":
        try:
            with wave.open(str(path), "rb") as handle:
                rate = handle.getframerate()
                if rate <= 0:
                    return None
                return round(handle.getnframes() / rate, 2)
        except (wave.Error, OSError):
            return None

    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return _estimate_mp3_duration(path)

    return None


# MP3 比特率表（kbps），按 [版本][层] 索引；索引 0 与 15 是保留值。
# ⚠️ 必须区分 MPEG1 与 MPEG2/2.5：豆包播客接口给的是 24000Hz，属于 **MPEG2**，
# 其 Layer III 比特率表和 MPEG1 完全不同。早先只按 MPEG1 表估算，
# 实测把一段真实的 58.52 秒音频算成了 35.11 秒（偏差 40%），并且直接显示给用户。
_MP3_BITRATE_TABLES: dict[tuple[int, int], list[int]] = {
    (1, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0],
    (1, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0],
    (1, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
}
_MP3_BITRATE_TABLES[(2, 3)] = _MP3_BITRATE_TABLES[(2, 2)]  # MPEG2/2.5 的 Layer II/III 共用一张表

_MP3_SAMPLE_RATES: dict[int, list[int]] = {
    1: [44100, 48000, 32000, 0],  # MPEG1
    2: [22050, 24000, 16000, 0],  # MPEG2
    0: [11025, 12000, 8000, 0],   # MPEG2.5
}

# 帧头里 2 bit 的 version 字段 → 上表的键。
# 注意 MPEG1 在帧头里是 3，不是 1。
_MP3_VERSION_BITS_TO_KEY = {3: 1, 2: 2, 0: 0}

# 每帧采样数：[版本][层]
_MP3_SAMPLES_PER_FRAME: dict[tuple[int, int], int] = {
    (1, 1): 384, (1, 2): 1152, (1, 3): 1152,
    (2, 1): 384, (2, 2): 1152, (2, 3): 576,
}


def _parse_mp3_frame_header(data: bytes, offset: int) -> dict[str, int] | None:
    """解析 offset 处的 MP3 帧头，不是合法帧头则返回 None。"""
    if offset + 4 > len(data):
        return None
    b0, b1, b2 = data[offset], data[offset + 1], data[offset + 2]

    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None

    version_bits = (b1 >> 3) & 0x03  # 3=MPEG1, 2=MPEG2, 0=MPEG2.5, 1=保留
    layer_bits = (b1 >> 1) & 0x03    # 1=Layer III, 2=Layer II, 3=Layer I
    if version_bits == 1 or layer_bits == 0:
        return None

    layer = 4 - layer_bits
    bitrate_index = (b2 >> 4) & 0x0F
    sample_rate_index = (b2 >> 2) & 0x03
    padding = (b2 >> 1) & 0x01

    # 帧头里的 version 字段是 3/2/0，而各张表用 1/2/0 作键（1=MPEG1）。
    # 必须显式转换：直接拿 version_bits 查表会在 MPEG1（3）上 KeyError。
    version_key = _MP3_VERSION_BITS_TO_KEY.get(version_bits)
    if version_key is None:
        return None
    table = _MP3_BITRATE_TABLES.get((version_key, layer))
    if table is None:
        return None
    bitrate_kbps = table[bitrate_index]
    sample_rate = _MP3_SAMPLE_RATES[version_key][sample_rate_index]
    if bitrate_kbps <= 0 or sample_rate <= 0:
        return None

    samples_per_frame = _MP3_SAMPLES_PER_FRAME[(version_key, layer)]
    if layer == 1:
        frame_length = int((12 * bitrate_kbps * 1000 / sample_rate) + padding) * 4
    else:
        coefficient = 144 if version_key == 1 else 72
        frame_length = int(coefficient * bitrate_kbps * 1000 / sample_rate) + padding

    if frame_length <= 4:
        return None

    return {
        "bitrate": bitrate_kbps * 1000,
        "sample_rate": sample_rate,
        "samples_per_frame": samples_per_frame,
        "frame_length": frame_length,
    }


def _estimate_mp3_duration(path: Path) -> float | None:
    """逐帧累计采样数得到时长。

    逐帧走而不是「首帧比特率 × 文件大小」的原因：后者对 VBR 文件是错的，
    而且必须先正确区分 MPEG 版本。逐帧累计对 CBR/VBR 都精确。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None

    offset = 0
    if data[:3] == b"ID3" and len(data) > 10:
        # ID3v2 标签长度是 synchsafe 整数（每字节只用低 7 位）
        size = (
            (data[6] & 0x7F) << 21
            | (data[7] & 0x7F) << 14
            | (data[8] & 0x7F) << 7
            | (data[9] & 0x7F)
        )
        offset = 10 + size

    total_samples = 0
    sample_rate = 0
    frames = 0

    while offset < len(data) - 4:
        header = _parse_mp3_frame_header(data, offset)
        if header is None:
            offset += 1  # 没对齐，逐字节找下一个同步字
            continue
        sample_rate = header["sample_rate"]
        total_samples += header["samples_per_frame"]
        offset += header["frame_length"]
        frames += 1

    if frames == 0 or sample_rate <= 0:
        return None
    return round(total_samples / sample_rate, 2)
