"""豆包播客 WebSocket 二进制协议测试。

这块逻辑没法在本地对接真实服务验证，所以用测试把字节布局钉死：
如果哪天帧格式改错了，这些用例必须第一时间失败。
"""

from __future__ import annotations

import json
import struct

import pytest

from app.services.podcast_tts import (
    COMP_NONE,
    EV_CONNECTION_STARTED,
    EV_FINISH_CONNECTION,
    EV_ROUND_AUDIO,
    EV_ROUND_FINISHED,
    EV_SESSION_FINISHED,
    EV_START_CONNECTION,
    EV_START_SESSION,
    FLAG_WITH_EVENT,
    MSG_AUDIO_ONLY_SERVER,
    MSG_ERROR,
    MSG_FULL_CLIENT,
    SER_JSON,
    build_event_frame,
    build_session_payload,
    parse_server_frame,
)


class TestFrameEncoding:
    def test_header_bytes(self):
        """帧头四字节必须严格符合协议：0x11 | type|flags | ser|comp | 0x00。"""
        frame = build_event_frame(EV_START_CONNECTION, payload=b"{}")

        assert frame[0] == 0x11, "版本 1 + 头长度 1 个单位"
        assert frame[1] == (MSG_FULL_CLIENT << 4) | FLAG_WITH_EVENT
        assert frame[2] == (SER_JSON << 4) | COMP_NONE
        assert frame[3] == 0x00

    def test_start_connection_has_no_session_id(self):
        """连接级事件不带 session_id 字段。"""
        frame = build_event_frame(EV_START_CONNECTION, session_id="should-be-ignored", payload=b"{}")

        # 头 4 字节 + event 4 字节 + payload 长度 4 字节 + payload 2 字节
        assert len(frame) == 4 + 4 + 4 + 2
        assert struct.unpack(">i", frame[4:8])[0] == EV_START_CONNECTION
        assert struct.unpack(">I", frame[8:12])[0] == 2
        assert frame[12:] == b"{}"

    def test_session_event_includes_session_id(self):
        session_id = "session-abc123"
        payload = b'{"a":1}'
        frame = build_event_frame(EV_START_SESSION, session_id=session_id, payload=payload)

        offset = 8  # 头 + event
        length = struct.unpack(">I", frame[offset : offset + 4])[0]
        assert length == len(session_id)
        offset += 4
        assert frame[offset : offset + length].decode() == session_id
        offset += length
        assert struct.unpack(">I", frame[offset : offset + 4])[0] == len(payload)
        assert frame[offset + 4 :] == payload

    def test_finish_connection_has_no_session_id(self):
        frame = build_event_frame(EV_FINISH_CONNECTION, session_id="ignored", payload=b"{}")
        assert struct.unpack(">i", frame[4:8])[0] == EV_FINISH_CONNECTION
        assert struct.unpack(">I", frame[8:12])[0] == 2

    def test_no_sequence_field_for_event_frames(self):
        """带 event 的帧 flags=0x4，不写 sequence 字段。"""
        frame = build_event_frame(EV_START_CONNECTION, payload=b"{}")
        assert struct.unpack(">i", frame[4:8])[0] == EV_START_CONNECTION


class TestFrameParsing:
    def test_roundtrip_connection_started(self):
        raw = build_event_frame(
            EV_CONNECTION_STARTED, connect_id="conn-1", payload=b'{"ok":true}'
        )
        frame = parse_server_frame(raw)

        assert frame.msg_type == MSG_FULL_CLIENT
        assert frame.has_event
        assert frame.event == EV_CONNECTION_STARTED
        assert frame.connect_id == "conn-1"
        assert frame.payload == b'{"ok":true}'

    def test_roundtrip_session_event_carries_session_id(self):
        raw = build_event_frame(EV_ROUND_FINISHED, session_id="s-9", payload=b"{}")
        frame = parse_server_frame(raw)

        assert frame.event == EV_ROUND_FINISHED
        assert frame.session_id == "s-9"

    def test_audio_frame_payload_is_raw_bytes(self):
        """361 事件的 payload 是裸音频字节，不能当 JSON 解析。"""
        audio = bytes(range(256)) * 4
        raw = build_event_frame(
            EV_ROUND_AUDIO, session_id="s-1", payload=audio, msg_type=MSG_AUDIO_ONLY_SERVER
        )
        frame = parse_server_frame(raw)

        assert frame.event == EV_ROUND_AUDIO
        assert frame.payload == audio

    def test_error_frame_reads_error_code(self):
        buf = bytearray()
        buf.append(0x11)
        buf.append((MSG_ERROR << 4) | FLAG_WITH_EVENT)
        buf.append((SER_JSON << 4) | COMP_NONE)
        buf.append(0x00)
        buf += struct.pack(">i", EV_SESSION_FINISHED)
        buf += struct.pack(">I", 0)  # session id 长度 0
        buf += struct.pack(">I", 4013)  # error code
        payload = b"invalid speaker"
        buf += struct.pack(">I", len(payload))
        buf += payload

        frame = parse_server_frame(bytes(buf))
        assert frame.msg_type == MSG_ERROR
        assert frame.error_code == 4013
        assert frame.payload == b"invalid speaker"

    def test_rejects_short_frame(self):
        with pytest.raises(Exception):
            parse_server_frame(b"\x11\x14")


class TestSessionPayload:
    SEGMENTS = [
        {"speaker": "A", "text": "第一段", "round": 0},
        {"speaker": "B", "text": "第二段", "round": 1},
        {"speaker": "A", "text": "第三段", "round": 2},
    ]
    VOICE_A = "zh_male_dayixiansheng_v2_saturn_bigtts"
    VOICE_B = "zh_female_mizaitongxue_v2_saturn_bigtts"

    def test_is_from_script_action(self):
        payload = build_session_payload(
            input_id="ep1",
            script_segments=self.SEGMENTS,
            voice_a=self.VOICE_A,
            voice_b=self.VOICE_B,
        )
        # action=3 表示「用我给的脚本合成」，而不是让服务自己写稿
        assert payload["action"] == 3

    def test_speakers_map_to_voice_ids(self):
        payload = build_session_payload(
            input_id="ep1",
            script_segments=self.SEGMENTS,
            voice_a=self.VOICE_A,
            voice_b=self.VOICE_B,
        )
        assert payload["nlp_texts"] == [
            {"speaker": self.VOICE_A, "text": "第一段"},
            {"speaker": self.VOICE_B, "text": "第二段"},
            {"speaker": self.VOICE_A, "text": "第三段"},
        ]

    def test_speaker_info_has_exactly_two_voices(self):
        payload = build_session_payload(
            input_id="ep1",
            script_segments=self.SEGMENTS,
            voice_a=self.VOICE_A,
            voice_b=self.VOICE_B,
        )
        assert payload["speaker_info"]["speakers"] == [self.VOICE_A, self.VOICE_B]
        assert len(payload["speaker_info"]["speakers"]) == 2
        assert payload["speaker_info"]["random_order"] is False

    def test_payload_is_json_serializable(self):
        payload = build_session_payload(
            input_id="ep1",
            script_segments=self.SEGMENTS,
            voice_a=self.VOICE_A,
            voice_b=self.VOICE_B,
        )
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        assert json.loads(encoded)["input_id"] == "ep1"

    def test_audio_config_defaults(self):
        payload = build_session_payload(
            input_id="ep1",
            script_segments=self.SEGMENTS,
            voice_a=self.VOICE_A,
            voice_b=self.VOICE_B,
        )
        assert payload["audio_config"]["format"] == "mp3"
        assert payload["audio_config"]["sample_rate"] == 24000
        assert payload["audio_config"]["speech_rate"] == 0
