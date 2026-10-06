"""业务逻辑测试：文本清洗、JSON 容错、Prompt 约束、时长探测。"""

from __future__ import annotations

import math
import struct
import wave

import pytest

from app.services import prompts
from app.services.ingest import clean_text, guess_arxiv_id, guess_title, truncate_smart
from app.services.llm import (
    build_script_payload,
    mock_analysis,
    mock_script,
    parse_json_response,
    _normalize_segments,
)
from app.services.podcast_tts import probe_duration


class TestCleanText:
    def test_repairs_hyphenated_line_breaks(self):
        assert "transformer" in clean_text("trans-\nformer")

    def test_drops_references_section(self):
        text = "正文内容。" * 60 + "\nReferences\n[1] Someone. A paper.\n[2] Another."
        cleaned = clean_text(text)
        assert "Someone. A paper." not in cleaned

    def test_drops_chinese_thanks_section(self):
        text = "正文内容。" * 60 + "\n致谢\n感谢我的导师。\n"
        cleaned = clean_text(text)
        assert "感谢我的导师" not in cleaned

    def test_drops_noise_lines(self):
        text = "正文第一段内容。\n42\nFigure 3\n正文第二段内容。"
        cleaned = clean_text(text)
        assert "Figure 3" not in cleaned
        assert "正文第一段内容" in cleaned

    def test_collapses_extra_whitespace(self):
        assert "\n\n\n" not in clean_text("a\n\n\n\n\nb")

    def test_empty_input(self):
        assert clean_text("") == ""


class TestTruncateSmart:
    def test_short_text_untouched(self):
        assert truncate_smart("abc", 100) == "abc"

    def test_keeps_both_ends(self):
        """超长时开头和结尾都要保留——论文的结论在结尾，不能丢。"""
        text = "开头" + "x" * 5000 + "结尾"
        result = truncate_smart(text, 1000)
        assert result.startswith("开头")
        assert result.endswith("结尾")
        assert "省略中间部分" in result
        assert len(result) < 1100


class TestGuessing:
    def test_extracts_arxiv_id(self):
        assert guess_arxiv_id("https://arxiv.org/abs/1706.03762") == "1706.03762"
        assert guess_arxiv_id("https://arxiv.org/pdf/1706.03762v5") == "1706.03762"
        assert guess_arxiv_id("https://example.com/paper") is None

    def test_guess_title_skips_abstract(self):
        text = "Abstract\nWe propose something.\n\nAttention Is All You Need\nAshish Vaswani"
        assert guess_title(text) == "Attention Is All You Need"

    def test_guess_title_falls_back(self):
        assert guess_title("Abstract\n短", fallback="兜底") == "兜底"


class TestParseJsonResponse:
    def test_plain_json(self):
        assert parse_json_response('{"a": 1}') == {"a": 1}

    def test_strips_markdown_fence(self):
        raw = '```json\n{"a": 1}\n```'
        assert parse_json_response(raw) == {"a": 1}

    def test_recovers_from_surrounding_prose(self):
        raw = '好的，以下是结果：\n{"a": 1}\n希望有帮助。'
        assert parse_json_response(raw) == {"a": 1}

    def test_handles_braces_inside_strings(self):
        raw = '{"text": "这里有个 } 花括号", "b": 2}'
        parsed = parse_json_response(raw)
        assert parsed["b"] == 2
        assert "花括号" in parsed["text"]

    def test_rejects_non_json(self):
        with pytest.raises(Exception):
            parse_json_response("完全没有 JSON")


class TestNormalizeSegments:
    def test_accepts_plain_form(self):
        result = _normalize_segments(
            [{"speaker": "A", "text": "一"}, {"speaker": "B", "text": "二"}]
        )
        assert [s["speaker"] for s in result] == ["A", "B"]

    def test_strips_speaker_prefix_in_text(self):
        """模型有时会把「主播A：」也写进 text，要清掉，否则会被念出来。"""
        result = _normalize_segments([{"speaker": "A", "text": "主播A：大家好"}])
        assert result[0]["text"] == "大家好"

    def test_tolerates_chinese_speaker_label(self):
        result = _normalize_segments([{"speaker": "主播B", "text": "问题"}])
        assert result[0]["speaker"] == "B"

    def test_lowercase_speaker(self):
        result = _normalize_segments([{"speaker": "b", "text": "x"}])
        assert result[0]["speaker"] == "B"

    def test_drops_empty_segments(self):
        result = _normalize_segments([{"speaker": "A", "text": "  "}, {"speaker": "B", "text": "y"}])
        assert len(result) == 1

    def test_non_list_returns_empty(self):
        assert _normalize_segments("不是列表") == []


class TestBuildScriptPayload:
    def test_numbers_rounds_sequentially(self):
        payload = build_script_payload(
            [{"speaker": "A", "text": "一二三"}, {"speaker": "B", "text": "四五六"}]
        )
        assert [s["round"] for s in payload["segments"]] == [0, 1]

    def test_counts_characters_and_estimates_duration(self):
        payload = build_script_payload([{"speaker": "A", "text": "字" * 250}])
        assert payload["word_count"] == 250
        # 无 padding（默认）时就是纯语速换算：250 / 350 * 60 ≈ 43 秒
        assert payload["est_duration_sec"] == pytest.approx(43, abs=1)

    def test_padding_is_added_to_estimate(self):
        payload = build_script_payload(
            [{"speaker": "A", "text": "字" * 250}], padding_sec=17
        )
        assert payload["est_duration_sec"] == pytest.approx(60, abs=1)


class TestPromptConstraints:
    def test_target_chars_roundtrips_to_requested_duration(self):
        """最重要的一致性：按目标字数写出来的脚本，预测时长应当回到用户选的分钟数。"""
        for minutes in (3, 5, 10):
            chars = prompts.target_chars(minutes)
            predicted = prompts.estimate_duration_sec(chars)
            assert abs(predicted - minutes * 60) <= 20, (
                f"选 {minutes} 分钟，按 {chars} 字预测 {predicted} 秒，偏差过大"
            )

    def test_target_chars_amortizes_padding(self):
        """正片之外的固定开销（品牌话术 / 音乐）时长越长摊得越薄。

        所以每分钟字数随时长**递增**：target_chars(10)/10 > target_chars(5)/5。
        """
        padding = 20.0
        assert prompts.target_chars(10, 0, padding) > prompts.target_chars(5, 0, padding)
        per_minute_5 = prompts.target_chars(5, 0, padding) / 5
        per_minute_10 = prompts.target_chars(10, 0, padding) / 10
        assert per_minute_10 > per_minute_5

    def test_no_padding_means_strictly_linear(self):
        """没有固定开销时，字数严格随时长线性增长。"""
        assert prompts.target_chars(10) == prompts.target_chars(5) * 2

    def test_brand_padding_counts_toward_budget(self):
        """品牌片头片尾是固定开销，要从时长预算里扣掉。"""
        from app.branding import brand_char_count

        chars = brand_char_count()
        assert chars > 0
        pad = prompts.compute_padding_sec(
            head_music=False, tail_music=False, brand_chars=chars
        )
        assert pad > 5, "品牌话术应当占用可观的时长"
        assert prompts.target_chars(5, 0, pad) < prompts.target_chars(5)
        assert prompts.estimate_duration_sec(
            prompts.target_chars(5, 0, pad), 0, pad
        ) == pytest.approx(300, abs=8)

    def test_music_toggle_changes_padding(self):
        """默认关掉服务端片头片尾音乐后，padding 里就不该再有它。"""
        off = prompts.compute_padding_sec(head_music=False, tail_music=False)
        on = prompts.compute_padding_sec(head_music=True, tail_music=True)
        assert off == 0
        assert on == pytest.approx(prompts.MUSIC_PADDING_SEC)
        single = prompts.compute_padding_sec(head_music=True, tail_music=False)
        assert 0 < single < on, "只开一个时应当只算对应那半"

    def test_duration_model_matches_real_measurements(self):
        """用真实合成结果校准过的模型，不能随意改动常量而不复核。

        实测（当时服务端片头片尾音乐是开着的，共约 17 秒）：
        - 238 字  → 58.49 秒
        - 1787 字 → 319.01 秒

        现在默认关掉了服务端音乐，所以这里显式传 17 秒 padding 来复核**语速常量本身**；
        再验证「无音乐」时正好等于总时长减掉音乐那一段。
        """
        music = prompts.MUSIC_PADDING_SEC
        assert prompts.estimate_duration_sec(238, 0, music) == pytest.approx(58.5, abs=3)
        assert prompts.estimate_duration_sec(1787, 0, music) == pytest.approx(319.0, abs=8)
        assert prompts.estimate_duration_sec(238, 0, 0) == pytest.approx(
            58.5 - music, abs=3
        )

    def test_slower_speech_rate_needs_fewer_chars(self):
        """调慢语速后，同样时长所需的字数必须变少，否则实际时长会超标。"""
        assert prompts.target_chars(5, -20) < prompts.target_chars(5, 0)
        assert prompts.target_chars(5, 20) > prompts.target_chars(5, 0)

    def test_speech_rate_roundtrips(self):
        for rate in (-30, -20, 0, 50):
            chars = prompts.target_chars(5, rate)
            predicted = prompts.estimate_duration_sec(chars, rate)
            assert abs(predicted - 300) <= 20, f"speech_rate={rate} 往返偏差过大"

    def test_script_prompt_states_word_budget(self):
        messages = prompts.build_script_messages(
            {"background": "bg"}, {"title": "T"}, duration_min=5, level="intro"
        )
        user = messages[-1]["content"]
        target = prompts.target_chars(5)
        # prompt 里给的是区间而不是单点，两端都要出现，模型才有明确的目标
        assert str(int(target * 0.92)) in user
        assert str(int(target * 1.05)) in user
        assert "硬约束" in user
        assert "5 分钟" in user

    def test_level_guide_differs_by_level(self):
        intro = prompts.build_script_messages({}, {}, duration_min=5, level="intro")[-1]["content"]
        expert = prompts.build_script_messages({}, {}, duration_min=5, level="expert")[-1]["content"]
        assert "非本专业" in intro
        assert "科研人员" in expert

    def test_prompt_forbids_ai_cliches(self):
        """反 AI 腔的负面清单是产品质量的关键，不能被人删掉。"""
        system = prompts.SCRIPT_SYSTEM
        for banned in ("综上所述", "值得注意的是", "首先"):
            assert banned in system

    def test_analysis_prompt_demands_json(self):
        assert '"innovations"' in prompts.ANALYSIS_SYSTEM
        assert '"limitations"' in prompts.ANALYSIS_SYSTEM


class TestMockGeneration:
    def test_mock_picks_relevant_paper(self):
        meta, _ = mock_analysis("this paper is about the transformer and self-attention")
        assert "Attention" in meta["title"]

        meta, _ = mock_analysis("we study residual learning for image recognition")
        assert "Residual" in meta["title"]

    def test_mock_analysis_has_all_fields(self):
        _, analysis = mock_analysis("some paper text")
        for field in (
            "background",
            "innovations",
            "method",
            "experiments",
            "conclusion",
            "limitations",
            "value",
            "future",
        ):
            assert analysis[field], f"{field} 不应为空"

    def test_mock_script_respects_short_duration(self):
        """Mock 脚本也要按目标时长裁剪，否则 3 分钟档会明显超长。"""
        _, analysis = mock_analysis("x")
        short = mock_script(analysis, {"title": "T"}, duration_min=3, level="intro")
        assert short["word_count"] < prompts.target_chars(3) * 1.5

    def test_mock_script_alternates_speakers(self):
        _, analysis = mock_analysis("x")
        script = mock_script(analysis, {"title": "T"}, duration_min=5, level="intro")
        speakers = {s["speaker"] for s in script["segments"]}
        assert speakers == {"A", "B"}


class TestProbeDuration:
    def test_reads_wav_duration(self, tmp_path):
        path = tmp_path / "t.wav"
        rate = 8000
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(b"\x00\x00" * rate * 2)  # 2 秒
        assert probe_duration(path) == pytest.approx(2.0, abs=0.05)

    def test_estimates_mpeg2_mp3_duration(self, tmp_path):
        """豆包播客接口返回的是 24000Hz 的 **MPEG2** MP3。

        这里用真实抓到的帧头 `FF F3 A4 C4`（MPEG2 / Layer III / 96kbps / 24000Hz）。
        早先的估算器只按 MPEG1 的比特率表算，把一段真实的 58.52 秒音频算成了
        35.11 秒（偏差 40%），而且这个值会直接显示给用户。
        """
        path = tmp_path / "mpeg2.mp3"
        frames = 120
        frame_length = 288  # int(72 * 96000 / 24000)
        header = bytes([0xFF, 0xF3, 0xA4, 0xC4])
        path.write_bytes((header + b"\x00" * (frame_length - 4)) * frames)

        duration = probe_duration(path)
        assert duration is not None
        # MPEG2 Layer III 每帧 576 采样
        assert duration == pytest.approx(frames * 576 / 24000, abs=0.05)

    def test_estimates_mpeg1_mp3_duration(self, tmp_path):
        path = tmp_path / "mpeg1.mp3"
        frames = 200
        frame_length = int(144 * 128000 / 44100)  # 128kbps / 44100Hz
        header = bytes([0xFF, 0xFB, 0x90, 0x00])
        path.write_bytes((header + b"\x00" * (frame_length - 4)) * frames)

        duration = probe_duration(path)
        assert duration is not None
        # MPEG1 Layer III 每帧 1152 采样
        assert duration == pytest.approx(frames * 1152 / 44100, abs=0.05)

    def test_skips_id3v2_tag(self, tmp_path):
        """ID3v2 标签内的字节不能被当成音频帧。"""
        path = tmp_path / "with_id3.mp3"
        tag_size = 200
        id3 = b"ID3\x04\x00\x00" + bytes([
            (tag_size >> 21) & 0x7F, (tag_size >> 14) & 0x7F,
            (tag_size >> 7) & 0x7F, tag_size & 0x7F,
        ]) + b"\x00" * tag_size
        frames = 60
        frame_length = 288
        header = bytes([0xFF, 0xF3, 0xA4, 0xC4])
        path.write_bytes(id3 + (header + b"\x00" * (frame_length - 4)) * frames)

        duration = probe_duration(path)
        assert duration is not None
        assert duration == pytest.approx(frames * 576 / 24000, abs=0.05)

    def test_rejects_garbage_after_sync_word(self, tmp_path):
        """只有同步字但帧头非法时不能瞎猜时长。"""
        path = tmp_path / "bad.mp3"
        path.write_bytes(bytes([0xFF, 0xFF, 0xFF, 0xFF]) * 100)
        assert probe_duration(path) is None

    def test_returns_none_for_unknown_format(self, tmp_path):
        path = tmp_path / "t.bin"
        path.write_bytes(b"not audio at all")
        assert probe_duration(path) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        assert probe_duration(tmp_path / "nope.wav") is None


class TestArxivUrlRewrite:
    """arXiv 摘要页没有正文，必须改抓 PDF，否则解读只能靠摘要（会写空、会编）。"""

    def test_abs_url_maps_to_pdf(self):
        from app.services.ingest import arxiv_pdf_url

        assert arxiv_pdf_url("https://arxiv.org/abs/1706.03762") == "https://arxiv.org/pdf/1706.03762"

    def test_versioned_abs_url_strips_version(self):
        from app.services.ingest import arxiv_pdf_url

        assert arxiv_pdf_url("https://arxiv.org/abs/1706.03762v5") == "https://arxiv.org/pdf/1706.03762"

    def test_pdf_url_maps_to_itself(self):
        from app.services.ingest import arxiv_pdf_url

        assert arxiv_pdf_url("https://arxiv.org/pdf/1706.03762") == "https://arxiv.org/pdf/1706.03762"

    def test_non_arxiv_url_returns_none(self):
        from app.services.ingest import arxiv_pdf_url

        assert arxiv_pdf_url("https://example.com/paper.pdf") is None

    def test_drops_inline_references_heading(self):
        """PDF 抽取时标题常和正文挤在同一行，这种形态也要认出来。"""
        text = "正文内容。" * 60 + "\nReferences [1] Someone. A paper. [2] Another."
        cleaned = clean_text(text)
        assert "Someone. A paper." not in cleaned
        assert "正文内容" in cleaned

    def test_drops_inline_acknowledgements(self):
        text = "正文内容。" * 60 + "\nAcknowledgements We are grateful to our reviewers."
        cleaned = clean_text(text)
        assert "grateful" not in cleaned

    def test_keeps_early_mention_of_references(self):
        """正文前半段合法地提到 references 时不能被误砍。"""
        text = "References to prior work are common.\n" + "正文内容。" * 60
        assert "References to prior work" in clean_text(text)
