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


class TestBilingualUnits:
    """双语的「单位换算」与语言规划。这些是最容易搞错、又最难从现象上看出来的地方。"""

    def test_english_word_count_is_words_not_chars(self):
        """`word_count` 是时长预算的单位数：中文数字符，英文数词。

        曾经按字符数统一统计，结果英文时长被高估 4~5 倍
        （2081 字符当成 2081 词 → 13.9 分钟，实际约 2.4 分钟）。
        """
        text = "one two three four five"
        zh = build_script_payload([{"speaker": "A", "text": text}], language="zh")
        en = build_script_payload([{"speaker": "A", "text": text}], language="en")
        assert zh["word_count"] == len(text)
        assert en["word_count"] == 5

    def test_english_estimate_uses_word_rate(self):
        """150 词应该正好估出 150/WORDS_PER_MINUTE_EN 分钟。

        断言的是「跟着常量走」，不是某个魔数 —— 这样以后重新实测语速时
        只需要改常量，不用再改一遍测试。
        """
        words = 150
        text = " ".join(["word"] * words)
        payload = build_script_payload([{"speaker": "A", "text": text}], language="en")
        expected = words / prompts.WORDS_PER_MINUTE_EN * 60
        assert payload["est_duration_sec"] == pytest.approx(expected, abs=1)
        assert payload["word_count"] == words

    def test_chinese_estimate_unchanged(self):
        payload = build_script_payload([{"speaker": "A", "text": "字" * 350}])
        assert payload["est_duration_sec"] == pytest.approx(60, abs=2)

    def test_target_chars_differs_by_language(self):
        zh = prompts.target_chars(5, 0, 0.0, "zh")
        en = prompts.target_chars(5, 0, 0.0, "en")
        assert zh == 1750
        # 英文按词算：5 分钟 × WORDS_PER_MINUTE_EN
        assert en == int(5 * prompts.WORDS_PER_MINUTE_EN)
        # 同一篇论文的英文稿「单位数」必然远少于中文 —— 1 个英文词要念好几倍时长
        assert en < zh / 2

    def test_brand_padding_is_per_language(self):
        from app import branding

        zh = branding.brand_char_count("zh")
        en = branding.brand_char_count("en")
        assert zh > 0 and en > 0
        # 英文话术按词算，量级必然比中文按字算小得多
        assert en < zh

    def test_brand_copy_is_really_english(self):
        from app import branding

        intro = " ".join(seg["text"] for seg in branding.intro_segments("en"))
        outro = " ".join(seg["text"] for seg in branding.outro_segments("en"))
        title, _ = branding.cta_text("en")
        for text in (intro, outro, title):
            cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
            assert cjk == 0, f"英文品牌话术里混进了中文：{text}"


class TestLanguagePlan:
    """语言规划：哪些版本要产出、谁是主语言。"""

    def _settings(self, **kwargs):
        from app.config import Settings

        return Settings(force_mock=True, **kwargs)

    def test_primary_comes_first(self):
        from app.services.pipeline import language_plan

        primary, languages = language_plan(
            {"language": "en", "languages": ["zh", "en"]}, self._settings()
        )
        assert primary == "en"
        assert languages == ["en", "zh"]

    def test_defaults_come_from_settings(self):
        from app.services.pipeline import language_plan

        primary, languages = language_plan({}, self._settings(languages="zh,en"))
        assert (primary, languages) == ("zh", ["zh", "en"])

    def test_unknown_language_values_are_dropped(self):
        from app.services.pipeline import language_plan

        primary, languages = language_plan(
            {"language": "jp", "languages": ["en", "jp"]}, self._settings()
        )
        assert primary == "zh"
        assert languages == ["zh", "en"]

    def test_single_language_default(self):
        from app.services.pipeline import language_plan

        assert language_plan({}, self._settings()) == ("zh", ["zh"])

    def test_audio_filename_keeps_primary_unsuffixed(self):
        """主语言的音频文件名不能变 —— 改了会让已经生成好的集全部失效。"""
        from app.services.pipeline import audio_filename, video_filename

        assert audio_filename("abc123", "zh", "zh") == "abc123.mp3"
        assert audio_filename("abc123", "en", "zh") == "abc123.en.mp3"
        assert video_filename("abc123", "zh", "zh") == "abc123.mp4"
        assert video_filename("abc123", "en", "zh") == "abc123.en.mp4"


class TestMockScriptLanguage:
    def test_english_mock_is_not_a_copy(self):
        meta, analysis = mock_analysis("attention transformer", language="en")
        script = mock_script(
            analysis, meta, duration_min=5, level="intro", language="en"
        )
        text = " ".join(s["text"] for s in script["segments"])
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        assert cjk / len(text) < 0.05
        assert script["word_count"] < prompts.target_chars(5, 0, 0.0, "en") * 1.5

    def test_chinese_mock_still_chinese(self):
        meta, analysis = mock_analysis("attention transformer", language="zh")
        script = mock_script(analysis, meta, duration_min=5, level="intro", language="zh")
        text = " ".join(s["text"] for s in script["segments"])
        # 语言纯度用比例判（钉一句具体文案太脆），再确认它已经是作者视角的新版
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        assert cjk / len(text) > 0.7, text[:80]
        # 第三人称讲解那一版的开场（用户要求：不要「我们」「你们」）
        assert "这篇论文做的事" in text


class TestSplitLongSegments:
    """TTS 单轮字符上限的兜底切分。

    双语上线前这里没被发现：豆包的播客接口对**单个 round 的文本长度**有硬上限
    （实测 >300 字符直接拒绝，错误码 40000010）。中文一段 120 字 ≈ 120 字符撞不到，
    英文一段 50 词就有 300 字符 —— 所以这个坑是英文版上线时才炸出来的，
    而且炸在 TTS 阶段，触发 worker 整条流水线重跑（重新解读 + 重新生成配图）。
    """

    def test_short_segments_untouched(self):
        from app.services.llm import split_long_segments

        segments = [{"speaker": "A", "text": "短句。"}, {"speaker": "B", "text": "也短。"}]
        out = split_long_segments(segments, max_chars=280)
        assert [s["text"] for s in out] == ["短句。", "也短。"]

    def test_reproduces_the_real_english_failure(self):
        """真实失败样本：一段 309 字符的英文发言。"""
        from app.services.llm import split_long_segments

        text = (
            "Right, and that is the part people usually miss: the adapter is not "
            "learning a new task, it is learning a correction on top of weights that "
            "already encode most of what you need, which is why it converges fast "
            "even though the trainable parameter count is three orders of magnitude "
            "smaller than what full fine-tuning would touch."
        )
        assert len(text) > 300, len(text)

        out = split_long_segments([{"speaker": "A", "text": text}], max_chars=280)
        assert len(out) >= 2
        assert all(len(s["text"]) <= 280 for s in out)
        assert all(s["speaker"] == "A" for s in out)
        # 切分不能丢字（只允许因为 strip 少掉空白）
        assert "".join(s["text"] for s in out).replace(" ", "") == text.replace(" ", "")

    def test_prefers_sentence_boundary(self):
        from app.services.llm import split_long_segments

        text = ("A" * 100) + "。" + ("B" * 100) + "。" + ("C" * 100) + "。"
        out = split_long_segments([{"speaker": "B", "text": text}], max_chars=150)
        assert all(len(s["text"]) <= 150 for s in out)
        assert out[0]["text"].endswith("。"), out[0]["text"]
        assert out[1]["text"].endswith("。"), out[1]["text"]

    def test_no_break_token_falls_back_to_hard_cut(self):
        from app.services.llm import split_long_segments

        out = split_long_segments(
            [{"speaker": "A", "text": "字" * 700}], max_chars=280
        )
        assert len(out) == 3
        assert all(len(s["text"]) <= 280 for s in out)

    def test_brand_marker_survives_split(self):
        from app.services.llm import split_long_segments

        out = split_long_segments(
            [{"speaker": "A", "text": "啊" * 600, "brand": "outro"}], max_chars=280
        )
        assert all(s.get("brand") == "outro" for s in out), "片尾标记丢了，视频会不认品牌卡"

    def test_limit_is_what_the_api_actually_enforces(self):
        """280 是给接口的 300 留的余量，不是随手定的数。"""
        assert prompts.MAX_ROUND_CHARS < 300

    def test_zh_and_en_prompts_both_state_the_limit(self):
        zh = prompts.script_system("zh")
        en = prompts.script_system("en")
        assert "260" in zh
        assert "280" in en
        # 英文那段必须是英文写的，否则模型会按中文的「字」理解词数
        assert "characters per" in en


class TestEnglishPromptIsWrittenInEnglish:
    """英文版的 user brief 必须是英文、且长度单位说 "words"。

    中文那句「正文总字数控制在 X-Y 字之间」在英文输出里会被模型当成词数，
    实测英文脚本因此比目标长 30%（目标 382 词、实际 496 词，成片 238 秒 vs 180 秒）。
    """

    _ANALYSIS = {
        "background": "bg",
        "innovations": ["a", "b"],
        "method": "m",
        "experiments": "e",
        "conclusion": "c",
        "limitations": ["l"],
        "value": "v",
        "future": ["f"],
    }

    def test_brief_is_english_and_counts_words(self):
        messages = prompts.build_script_messages(
            self._ANALYSIS, {"title": "T"}, duration_min=5, level="intro", language="en"
        )
        body = messages[-1]["content"]
        assert "words" in body
        assert "Target length" in body
        cjk = sum(1 for ch in body if "\u4e00" <= ch <= "\u9fff")
        # 允许出现被引用的论文标题里的中文，但整体不能是中文 brief
        assert cjk < 20, f"英文 brief 里混了大量中文：{body[:200]}"

    def test_english_system_prompt_has_no_chinese_instructions(self):
        system = prompts.build_script_messages(
            self._ANALYSIS, {"title": "T"}, duration_min=5, level="intro", language="en"
        )[0]["content"]
        assert "HARD LIMIT" in system
        cjk = sum(1 for ch in system if "\u4e00" <= ch <= "\u9fff")
        assert cjk == 0, "英文版的 system prompt 里还有中文要求"

    def test_chinese_brief_unchanged(self):
        body = prompts.build_script_messages(
            self._ANALYSIS, {"title": "T"}, duration_min=5, level="intro", language="zh"
        )[-1]["content"]
        assert "正文总字数控制在" in body
        assert "【解读稿】" in body

    def test_word_budget_scales_with_measured_rate(self):
        body = prompts.build_script_messages(
            self._ANALYSIS, {"title": "T"}, duration_min=10, level="intro", language="en"
        )[-1]["content"]
        words = prompts.target_chars(10, 0, 0.0, "en")
        assert f"{int(words * 0.92)}-{int(words * 1.05)} words" in body


class TestAnalysisPromptLanguage:
    """英文版的解读 prompt 必须整段是英文。

    踩过的坑：只给中文 `ANALYSIS_SYSTEM` 追加一句「请用英文输出」不管用 ——
    模型跟着中文 system prompt 走了，英文版的 background 实测 83% 是中文。
    脚本那边因为一开始就写了完整的 `SCRIPT_SYSTEM_EN`，反而没出问题。
    输出语言这种事，**指令语言本身就得和目标语言一致**。
    """

    def test_english_system_prompt_has_no_chinese(self):
        system = prompts.analysis_system("en")
        cjk = sum(1 for ch in system if "\u4e00" <= ch <= "\u9fff")
        assert cjk == 0, f"英文解读 prompt 里还有 {cjk} 个中文字"
        assert '"analysis"' in system or '"paper_meta"' in system, "JSON 结构说明丢了"

    def test_english_user_brief_has_no_chinese(self):
        messages = prompts.build_analysis_messages(
            "PAPER BODY TEXT", title_hint="A Title", language="en"
        )
        user = messages[1]["content"]
        cjk = sum(1 for ch in user if "\u4e00" <= ch <= "\u9fff")
        assert cjk == 0, f"英文解读的 user brief 里还有中文：{user[:80]}"
        assert "A Title" in user and "PAPER BODY TEXT" in user

    def test_chinese_path_unchanged(self):
        messages = prompts.build_analysis_messages("正文", title_hint="标题", language="zh")
        assert "论文正文" in messages[1]["content"]
        assert "研究背景" in messages[0]["content"]

    def test_both_languages_ask_for_the_same_json_shape(self):
        import json
        import re

        def fields(system: str) -> set[str]:
            match = re.search(r"\{.*\}", system, re.S)
            assert match, "prompt 里找不到 JSON 结构说明"
            return set(re.findall(r'"(\w+)":', match.group(0)))

        zh = fields(prompts.analysis_system("zh"))
        en = fields(prompts.analysis_system("en"))
        assert zh == en, f"两版的 JSON 字段不一致：{zh ^ en}"

    def test_not_covered_fallback_is_localised(self):
        # 论文没写某个字段时的兜底文案也要跟着语言走
        assert "论文未涉及" in prompts.analysis_system("zh")
        assert "Not covered in the paper." in prompts.analysis_system("en")


class TestAnalysisCoversTheWholeReadingOrder:
    """解读要按「谁的工作 → 背景与问题 → 方法 → 关键数据 → 结论 → 概念解释 → 创新 → 优缺点 → 方向」走。

    用户给的结构里有两件事原来的解读**没有**：研究问题（单独成句）和
    「新颖概念的通俗解释」；还有一件事做了一半：只写了「不足」，
    没有正面写过「优点」。所以新增了 `question` / `concepts` / `strengths`。
    """

    def _prompt_shape(self, language: str = "zh") -> tuple[set[str], set[str]]:
        import json
        import re

        system = prompts.analysis_system(language)
        match = re.search(r"\{.*\}", system, re.S)
        assert match, "prompt 里找不到 JSON 结构说明"
        block = json.loads(match.group(0))
        return set(block["paper_meta"]), set(block["analysis"])

    def test_prompt_model_and_normalizer_agree_on_the_fields(self):
        """prompt 要的字段 == 落库的模型 == 归一化函数 == Mock 给的字段。

        这三处最容易漂移：prompt 里加了一个字段、模型里没有 → 模型白写一段；
        模型里有、prompt 里没要 → 界面上永远空着。两边都不报错，
        所以只能拿一条测试钉住。
        """
        from app.models import Analysis, PaperMeta

        model_fields = set(Analysis.model_fields)
        meta_fields = set(PaperMeta.model_fields)
        for language in ("zh", "en"):
            prompt_meta, prompt_analysis = self._prompt_shape(language)
            assert prompt_analysis == model_fields, f"{language} 的 analysis 字段对不上"
            assert prompt_meta == meta_fields, f"{language} 的 paper_meta 字段对不上"

        meta, mock = mock_analysis("Attention Is All You Need", title_hint="Attention")
        assert set(mock) == model_fields, f"Mock 落下的解读字段：{model_fields ^ set(mock)}"
        assert set(meta) == meta_fields, f"Mock 落下的元信息字段：{meta_fields ^ set(meta)}"

    def test_new_fields_survive_normalisation(self):
        from app.services.llm import _normalize_analysis, _normalize_meta

        analysis = _normalize_analysis(
            {
                "background": "背景",
                "question": "它到底能不能成立？",
                "concepts": ["自注意力：每个位置都看一遍其他位置"],
                "strengths": ["对照组做得很干净"],
            }
        )
        assert analysis["question"] == "它到底能不能成立？"
        assert analysis["concepts"] == ["自注意力：每个位置都看一遍其他位置"]
        assert analysis["strengths"] == ["对照组做得很干净"]

        # 老数据（模型没给）→ 空值，不是 None、也不是报错
        empty = _normalize_analysis({})
        assert empty["question"] == "" and empty["concepts"] == [] and empty["strengths"] == []

        meta = _normalize_meta({"title": "T", "affiliations": ["Google Brain"]})
        assert meta["affiliations"] == ["Google Brain"]
        assert _normalize_meta({})["affiliations"] == []

    def test_affiliations_must_come_from_the_paper(self):
        """作者机构只能来自原文 —— 猜错的机构比不写更糟（解读会照它讲「这是谁的工作」）。"""
        zh = prompts.analysis_system("zh")
        en = prompts.analysis_system("en")
        assert "作者机构只能来自论文原文" in zh
        assert "论文未标注机构" in zh
        assert "Affiliations come from the paper only" in en

    def test_affiliations_reach_the_podcast_brief(self):
        """机构要**传到脚本那一步**，否则播客里永远不会点出这是谁的工作。"""
        messages = prompts.build_script_messages(
            {"background": "背景", "innovations": ["a"]},
            {"title": "T", "affiliations": ["Google Brain", "University of Toronto"]},
            duration_min=3,
            level="intro",
            language="zh",
        )
        body = messages[-1]["content"]
        assert "【作者机构】Google Brain、University of Toronto" in body
        # 没有机构时不许出现这一行（模型手里没有的事实，宁可不说）
        bare = prompts.build_script_messages(
            {"background": "背景", "innovations": ["a"]},
            {"title": "T"},
            duration_min=3,
            level="intro",
            language="zh",
        )[-1]["content"]
        assert "【作者机构】" not in bare

    def test_markdown_export_keeps_the_same_sections(self):
        """下载的解读 .md 要用同一套标题与顺序，否则「解读」在页面上和文件里是两个东西。"""
        from app.services.pipeline import render_analysis_markdown

        episode = {
            "title": "T",
            "paper_meta": {"authors": ["A"], "affiliations": ["Google Brain"]},
            "analysis": {
                "background": "背景",
                "question": "问题？",
                "concepts": ["概念：大白话"],
                "strengths": ["优点一"],
                "limitations": ["不足一"],
            },
            "script": {},
        }
        text = render_analysis_markdown(episode, "zh")
        for heading in ("**作者机构**：Google Brain", "## 研究问题", "## 关键概念（通俗解释）", "## 优点"):
            assert heading in text, text
        assert text.index("## 研究背景") < text.index("## 研究问题") < text.index("## 关键概念（通俗解释）")

        english = render_analysis_markdown(episode, "en")
        assert "**Affiliations**：Google Brain" in english
        assert "## Research question" in english and "## Strengths" in english


class TestScriptSpeaksAsTheAuthorAndTeaches:
    """文案生成的两条硬要求（用户原话：「用费曼技巧解释，把自己当成作者」）。

    实测改之前的稿子（三集，库里已有）：「我们」只出现 2 次、「他们」8~9 次、「你们」0 次 ——
    也就是说主播在**转述**一篇论文，而不是在讲自己做过的事。这两条就是冲这个来的。
    """

    def test_both_languages_teach_the_feynman_way(self):
        zh, en = prompts.SCRIPT_SYSTEM, prompts.SCRIPT_SYSTEM_EN
        assert "费曼技巧" in zh
        assert "先讲直觉，再上术语" in zh
        assert "类比不能改变事实" in zh
        assert "讲不顺 = 这里其实没讲透" in zh

        assert "Feynman" in en
        assert "Intuition first, jargon second" in en
        assert "an analogy may not change" in en
        assert "you have not finished understanding it" in en

    def test_both_languages_speak_as_the_author(self):
        zh, en = prompts.SCRIPT_SYSTEM, prompts.SCRIPT_SYSTEM_EN
        # 用户后来把口吻反过来：**不要冒充作者**，用第三人称讲解
        assert "第三人称讲解" in zh and "不是作者本人" in zh
        assert "冒充作者" in zh
        assert "explainer, not the author" in en.lower()
        assert "third person" in en.lower()
        assert "your team" in en

    def test_third_person_is_not_a_licence_to_invent(self):
        """换成第三人称讲解之后，**「替作者编动机」成了最容易犯的那条**，所以禁令照旧写死。

        （以前这条叫「第一人称不是编造许可证」—— 用户把口吻反过来之后，
        风险点从「替自己编心路历程」变成「替别人编心路历程」，禁令本身没变。）
        """
        zh, en = prompts.SCRIPT_SYSTEM, prompts.SCRIPT_SYSTEM_EN
        assert "第三人称不是免责声明" in zh
        assert "一律不许编" in zh
        assert "not a licence to invent" in en
        assert "Never invent failed" in en

    def test_mock_script_uses_the_same_voice(self):
        """离线演示（浏览器 Mock / 无密钥）看到的文案要和线上同一个风格。

        否则改完 prompt 打开演示页还是旧腔调，会以为改动没生效 ——
        这个坑在这个项目里踩过好几次（配色、白底都是这么发现的）。
        """
        meta, analysis = mock_analysis("Attention Is All You Need", title_hint="Attention")
        # 声音基准 = **第三人称讲解**（用户明确要求不要「我们」「你们」），
        # 所以标记词也跟着换成「作者 / 这篇论文」那一套。
        cases = (
            ("zh", ("作者", "打个比方")),
            ("en", ("The authors", "relay race")),
        )
        for language, markers in cases:
            script = mock_script(
                analysis, meta, duration_min=3, level="intro", language=language
            )
            text = " " + "".join(item["text"] for item in script["segments"]) + " "
            for marker in markers:
                assert marker in text, f"{language} 的 Mock 稿子里没有 {marker!r}：{text[:120]}"
            # 第一/第二人称一个都不许有（品牌话术除外，Mock 里没有品牌段）
            from app.services.prompts.text import first_person_hits

            hits = first_person_hits(script["segments"], language=language)
            assert not hits, f"{language} 的 Mock 稿子里还有第一/第二人称：{hits}"

    def test_expert_level_still_lands_terms_in_plain_words(self):
        """专家档也别把术语扔下不管 —— 费曼那条对所有难度档都成立。"""
        assert "一句大白话" in prompts.LEVEL_GUIDE["expert"]
        assert "plain words" in prompts.LEVEL_GUIDE_EN["expert"]


class TestLoneSurrogates:
    """PDF 里数学字体的花体字母（U+1D400 段）是**代理对**，某些抽取路径会把它拆开，
    留下半个 `\\ud835`。这种字符存不进 UTF-8，httpx 发请求时会抛：

        UnicodeEncodeError: 'utf-8' codec can't encode character '\\ud835'
        in position 12275: surrogates not allowed

    用户报的就是这条「生成失败：内部错误」——错误信息里只有位置没有上下文，
    所以入口处（以及发请求前）都要清掉。
    """

    def test_lone_surrogate_is_dropped(self):
        from app.services.ingest import drop_lone_surrogates

        lone = "注意力" + chr(0xD835) + "矩阵"
        cleaned = drop_lone_surrogates(lone)
        assert cleaned == "注意力矩阵"
        assert cleaned.encode("utf-8")  # 能编码回 UTF-8 才算修好

    def test_paired_surrogates_are_restored(self):
        """成对的要**还原**成真正的字符，不是丢掉：花体字母本身是有信息量的。"""
        from app.services.ingest import drop_lone_surrogates

        # 拆成两个代理码元的写法要按平台字节序走（手写 big-endian 字节在小端机器上会解成别的字）
        paired = (
            "x"
            + "\U0001d400".encode("utf-16", "surrogatepass").decode("utf-16", "surrogatepass")
            + "y"
        )
        cleaned = drop_lone_surrogates(paired)
        assert cleaned == "x\U0001d400y"
        assert len(cleaned) == 3

    def test_clean_text_never_returns_surrogates(self):
        """真正入口是 `clean_text`（PDF 抽取与网页正文都走它）。"""
        from app.services.ingest import clean_text

        cleaned = clean_text("论文标题 " + chr(0xD835) + " 与正文")
        cleaned.encode("utf-8")  # 不抛异常就是通过
        assert chr(0xD835) not in cleaned

    def test_normal_text_is_untouched(self):
        from app.services.ingest import drop_lone_surrogates

        assert drop_lone_surrogates("普通中文 + English 123") == "普通中文 + English 123"

    def test_llm_messages_are_sanitised_before_sending(self):
        """发请求前再兜一道：粘贴文本、模型输出回灌这些路径不经过 PDF 抽取。"""
        from app.services.llm import LLMClient
        from app.config import Settings

        client = LLMClient(Settings(force_mock=True))
        captured: dict = {}

        def fake_post(url, **kwargs):  # 拦在 httpx 之前
            captured.update(kwargs)
            raise RuntimeError("stop here")

        import app.services.llm as llm_module

        monkey = llm_module.httpx.Client
        try:
            llm_module.httpx.Client = lambda *a, **k: type(
                "C", (), {"__enter__": lambda s: s, "__exit__": lambda *a: False,
                          "post": staticmethod(fake_post)}
            )()
            client._chat([{"role": "user", "content": "带代理" + chr(0xD835)}],
                         max_tokens=8, temperature=0.2)
        except Exception:
            pass
        finally:
            llm_module.httpx.Client = monkey
        sent = captured.get("json") or {}
        body = str(sent)
        assert chr(0xD835) not in body


class TestTrimAcceptsAnythingWithinBudget:
    """脚本超长时的兜底：**落进预算就算成功**，并且最多试两次。

    实测有两集写了 1572 字（目标 1050，150%），成片 4.5 分钟而不是 3 分钟。
    原来的接受条件要求「至少减 3%」，于是「压到 1200 字」这种**已经进预算**的结果
    会被整份丢掉，白付一次调用、还留着 1572 字 —— 这条测试就是钉这个判据。
    """

    class _LLM:
        """假模型：按脚本回固定字数，并记下调用了几次。"""

        def __init__(self, word_counts):
            self.word_counts = list(word_counts)
            self.calls = 0

        def _chat_json(self, messages, *, max_tokens, temperature=0.6):
            self.calls += 1
            count = self.word_counts[min(self.calls - 1, len(self.word_counts) - 1)]
            # 造一份「正好这么多字」的稿子（中文一个字算一字）
            text = "字" * max(1, count // 6)
            return {"segments": [{"speaker": "A", "text": text} for _ in range(6)]}

    def _script(self, chars: int) -> dict:
        text = "字" * max(1, chars // 6)
        return {
            "segments": [{"speaker": "A", "text": text} for _ in range(6)],
            "word_count": chars,
        }

    def test_a_result_within_budget_is_accepted(self):
        """1200 字虽然没压到 1050，但已在预算内（1050×1.15=1207）→ 接受。"""
        from app.services.llm import LLMClient

        client = LLMClient.__new__(LLMClient)          # 不走 __init__，只借方法
        client.settings = None
        client._chat_json = self._LLM([1200])._chat_json  # type: ignore[method-assign]
        trimmed = client._trim_script(
            self._script(1572), target=1050, speech_rate=0, language="zh"
        )
        assert trimmed["word_count"] < 1572, trimmed["word_count"]
        assert trimmed["word_count"] <= 1050 * 1.15 + 1

    def test_a_result_over_budget_is_rejected(self):
        """压不动（1400 字 > 预算 1207）→ 保留原稿，而不是留下一个「短了一点」的稿子。"""
        from app.services.llm import LLMClient

        client = LLMClient.__new__(LLMClient)
        client.settings = None
        client._chat_json = self._LLM([1400])._chat_json  # type: ignore[method-assign]
        original = self._script(1572)
        assert client._trim_script(original, target=1050, speech_rate=0, language="zh") is original

    def test_the_retry_prompt_says_it_is_the_second_try(self):
        from app.services.prompts.repair import build_trim_messages

        first = build_trim_messages([{"speaker": "A", "text": "x"}], current_chars=1572, target=1050)
        second = build_trim_messages(
            [{"speaker": "A", "text": "x"}], current_chars=1572, target=1050, attempt=2
        )
        assert "第二次精简" not in first[1]["content"]
        assert "第二次精简" in second[1]["content"]


class TestRetryCarriesTheRealFailure:
    """第二次精简要**带着第一次的真实结果**去，而不是把原稿再压一遍。

    实测的那两集是 1572 字 / 目标 1050（150%）：两次都拿原稿去压、第二次只多一句
    「这次必须砍到 1050」，模型看不到上一次砍到多少、哪几段几乎没动，
    于是原地打转。参考做法（Fibona agent）里最硬的一条就是**修复的输入是真实错误**，
    不是「再试一次」——这一组测试盯的就是这件事。
    """

    class _LLM:
        def __init__(self, word_counts):
            self.word_counts = list(word_counts)
            self.calls: list[list[dict]] = []

        def _chat_json(self, messages, *, max_tokens, temperature=0.6):
            self.calls.append(messages)
            count = self.word_counts[min(len(self.calls) - 1, len(self.word_counts) - 1)]
            text = "字" * max(1, count // 6)
            return {"segments": [{"speaker": "A", "text": text} for _ in range(6)]}

    def _client(self, word_counts):
        from app.services.llm import LLMClient

        client = LLMClient.__new__(LLMClient)
        client.settings = None
        llm = self._LLM(word_counts)
        client._chat_json = llm._chat_json  # type: ignore[method-assign]
        return client, llm

    def _script(self, chars: int) -> dict:
        text = "字" * max(1, chars // 6)
        return {"segments": [{"speaker": "A", "text": text} for _ in range(6)], "word_count": chars}

    def test_second_attempt_gets_the_first_result_as_its_base(self):
        """第二次的原稿必须是**第一次压出来的那一版**，不是 1572 字那份原稿。"""
        target = 1050
        client, llm = self._client([1400, 1100])       # 第一次压到 1400（超预算），第二次压到 1100
        script = self._script(1572)
        trimmed = client._trim_script(
            script, target=target, speech_rate=0, language="zh", attempt=1
        )
        assert trimmed is script, "第一次超预算时返回值仍是原稿（老行为不变）"

        detail: dict = {}
        client._trim_script(
            self._script(1400), target=target, speech_rate=0, language="zh",
            attempt=2, previous={"chars": 1400, "base_chars": 1572}, detail=detail,
        )
        prompt = llm.calls[-1][1]["content"]
        assert "【上一版的结果】" in prompt, prompt[-400:]
        assert "1572" in prompt and "1400" in prompt, "修复提示里要有真实的两个数字"

    def test_the_retry_prompt_lists_what_the_last_pass_actually_cut(self):
        """带上去的必须是**具体到段**的线索：哪几段砍得最少（那才是要动的地方）。"""
        from app.services.prompts.repair import build_trim_messages

        base = [{"speaker": "A", "text": "字" * 60}, {"speaker": "B", "text": "字" * 40}]
        current = [{"speaker": "A", "text": "字" * 58}, {"speaker": "B", "text": "字" * 20}]
        prompt = build_trim_messages(
            current, current_chars=78, target=60, attempt=2,
            previous={"chars": 78, "base_chars": 100, "base_segments": base},
        )[1]["content"]
        assert "第 1 段" in prompt and "砍了 2 字" in prompt
        assert prompt.index("第 1 段") < prompt.index("第 2 段"), "砍得最少的要排在前面"

    def test_no_previous_means_the_prompt_is_unchanged(self):
        """没给 `previous` 时（第一次精简）不能多出那一块 —— 老调用点一字不变。"""
        from app.services.prompts.repair import build_trim_messages

        first = build_trim_messages(
            [{"speaker": "A", "text": "x"}], current_chars=1572, target=1050
        )[1]["content"]
        assert "【上一版的结果】" not in first

    def test_the_trim_loop_keeps_the_shorter_version_when_both_attempts_miss(self):
        """两次都没压进预算 → 先留更短的那一版，再交给**确定性删段**落进预算。

        原来两条出口都返回原稿：「压不到 1050」就把 1572 → 1250 的成果一起丢掉。
        现在更近一步：没进预算就按规则整段删（记录在台账里），保证**时长兑现**。
        """
        from app.services import prompts
        from app.services.llm import LLMClient

        # ⚠️ `speech_rate` 是**百分比档位**（0 = 正常），不是「字/分钟」——
        # 这两个测试原来传 350，被 `effective_rate` 的新校验当场抓住（正是它该抓的）。
        target = prompts.target_chars(3)            # 和被测代码同一个口径（默认档位 0）
        original = int(target * 1.5)
        client, _ = self._client([int(target * 1.32), int(target * 1.18)])
        result = client._repair_length_if_needed(
            self._script(original), analysis={}, duration_min=3, level="intro", speech_rate=0,
        )
        assert result["word_count"] < original, result["word_count"]
        assert result["word_count"] <= target * 1.15 + 1, "没落进预算（时长就兑现不了）"
        ledger = result.get("length_repair") or {}
        assert len(ledger.get("attempts") or []) == 2
        assert ledger.get("pruned") is True and ledger["deleted"], ledger

    def test_the_ledger_is_recorded_even_when_it_succeeds(self):
        """成功也要留下台账（几次、每版多少字）—— 只写日志等于没人看得见。"""
        from app.services import prompts
        from app.services.llm import LLMClient

        target = prompts.target_chars(3)
        client, _ = self._client([int(target * 1.32), int(target * 0.95)])
        result = client._repair_length_if_needed(
            self._script(int(target * 1.5)), analysis={}, duration_min=3,
            level="intro", speech_rate=0,
        )
        ledger = result.get("length_repair")
        assert ledger and ledger["attempts"], ledger
        assert ledger["attempts"][0]["accepted"] is False
        assert ledger["attempts"][-1]["accepted"] is True


class TestNoFirstPerson:
    """用户要求：脚本与解读里不要「我们」「你们」这类第一/第二人称。

    踩过的事：prompt 里**本来就有**「不要用『我们』冒充作者」，但同一段又留了
    「日常口语里表示『我们一起看』的『我们』可以用」这个口子 —— 模型顺着口子写
    「我们做了两件事」「我们这篇论文」「你们怎么解决」，实测一集 41 段里 14 处。
    所以这一轮做两件事：把口子堵上（prompt），并且**能数出来**（确定性检查）。
    """

    def test_the_zh_script_prompt_bans_both_pronouns_with_rewrites(self):
        from app.services.prompts.script import SCRIPT_SYSTEM

        assert "不要出现「我们」「咱们」「你们」" in SCRIPT_SYSTEM
        assert "我们做了两件事" in SCRIPT_SYSTEM, "要给出替换写法，不然模型不知道换成什么"
        assert "这篇论文做了两件事" in SCRIPT_SYSTEM
        # 老的那个口子必须堵上（它就是实测违规的来源）
        assert "「我们」可以用" not in SCRIPT_SYSTEM

    def test_the_en_script_prompt_bans_them_too(self):
        from app.services.prompts.script import SCRIPT_SYSTEM_EN

        assert 'Never use "we", "our", "us" or "you"' in SCRIPT_SYSTEM_EN
        # 老设定「B 管 A 叫 your team」就是「你们」，必须连同设定一起去掉
        assert "addresses A's team as" not in SCRIPT_SYSTEM_EN

    def test_both_analysis_prompts_require_the_third_person(self):
        from app.services.prompts.analysis import ANALYSIS_SYSTEM, ANALYSIS_SYSTEM_EN

        assert "一律用第三人称讲论文" in ANALYSIS_SYSTEM
        assert "never use \"we\", \"our\", \"us\" or \"you\"" in ANALYSIS_SYSTEM_EN

    def test_the_checker_counts_the_right_sentences(self):
        from app.services.prompts.text import first_person_hits

        segments = [
            # 品牌话术里就有「我们」，那是写死的社区语气，不算违规
            {"text": "这里是青稞社区。我们只讲自己真正读过的东西。", "brand": "intro"},
            {"text": "我们做了两件事。"},
            {"text": "那你们怎么解决？"},
            {"text": "作者试过另一条路。"},
        ]
        hits = first_person_hits(segments)
        assert [index for index, _ in hits] == [1, 2], hits

    def test_the_checker_handles_english(self):
        from app.services.prompts.text import first_person_hits

        hits = first_person_hits(
            [{"text": "We did two things."}, {"text": "The authors tried another route."}],
            language="en",
        )
        assert hits == [(0, "We did two things.")], hits

    def test_the_generator_warns_instead_of_staying_silent(self, caplog):
        """模型不守规矩时要**留下痕迹**（只报警不改写：改写是模型的事）。"""
        import logging

        from app.services.llm import LLMClient

        client = LLMClient.__new__(LLMClient)
        client.settings = None
        with caplog.at_level(logging.WARNING):
            client._report_first_person(
                {"segments": [{"text": "我们做了两件事。"}]}, language="zh"
            )
        assert any("第一/第二人称" in record.getMessage() for record in caplog.records)

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            client._report_first_person(
                {"segments": [{"text": "这篇论文做了两件事。"}]}, language="zh"
            )
        assert not caplog.records, "守规矩时不该报警"

    def test_quoted_titles_are_not_counted(self):
        """论文原题里就带 You（「Attention Is All You Need」）—— 引号内不算。

        不跳过的话每集都会误报一次，报警很快就没人看了。
        """
        from app.services.prompts.text import first_person_hits

        hits = first_person_hits(
            [{"text": "《Attention Is All You Need》这篇论文做了一件事。"}], language="zh"
        )
        assert hits == []
        hits = first_person_hits(
            [{"text": "“Attention Is All You Need” does one thing."}], language="en"
        )
        assert hits == []


class TestSpeedPercentIsNotACharRate:
    """`effective_rate(speech_rate)` 的 `speech_rate` 是**百分比偏移**，不是「字/分钟」。

    踩过的坑（我自己踩的）：给它传 350（以为是「350 字/分钟」），
    它会静默算成 `base × 4.5` —— 3 分钟档的目标字数从 1050 变成 4725，
    而且**不报错**，只是时长模型整个偏掉。所以现在越界直接 raise。
    """

    def test_zero_means_the_base_rate(self):
        from app.services.prompts.units import CHARS_PER_MINUTE, effective_rate

        assert effective_rate(0) == CHARS_PER_MINUTE
        assert effective_rate(10) > CHARS_PER_MINUTE
        assert effective_rate(-20) < CHARS_PER_MINUTE

    def test_passing_a_char_rate_explodes_loudly(self):
        import pytest as _pytest

        from app.services.prompts.units import effective_rate

        with _pytest.raises(ValueError, match="百分比偏移"):
            effective_rate(350)

    def test_target_chars_still_works_with_the_default_rate(self):
        """默认档位（0）下，3 分钟档的目标字数仍然按 350 字/分钟算（≈1050）。"""
        from app.services import prompts

        assert 1000 < prompts.target_chars(3) < 1100



class TestPruneToBudget:
    """模型压不下来就**整段删**（确定的规则，可复算）。

    实测：目标 936 字，模型写 1303（+39%），两次精简只减 8%（→1194，仍在预算 1076 之上），
    成片于是比设定时长长 24%~69%。「让模型再删几句」这条路已经试到 #2 了，所以补一条
    不依赖模型的闸。
    """

    SEGS = [
        {"speaker": "A", "brand": "intro", "text": "这里是青稞社区。我们只讲自己读过的东西。"},
        {"speaker": "A", "text": "这是一段很长的解释，" * 20},
        {"speaker": "B", "text": "那基线公平吗？"},
        {"speaker": "A", "text": "在 16 个任务上提升 67%，基线是 ResNet-50。"},
        {"speaker": "A", "text": "所以它的意义在这里。"},
        {"speaker": "B", "text": "今天就聊到这儿。"},
    ]

    def test_within_budget_is_a_noop(self):
        from app.services.prompts.repair import prune_to_budget

        kept, deleted = prune_to_budget(self.SEGS, target_chars=10000, budget_chars=11500)
        assert kept == self.SEGS and deleted == []

    def test_it_deletes_the_longest_digit_free_segment_first(self):
        from app.services.prompts.repair import prune_to_budget

        kept, deleted = prune_to_budget(self.SEGS, target_chars=100, budget_chars=147)
        assert [item["segment"] for item in deleted] == [1], deleted
        assert all("青稞社区" in seg["text"] for seg in kept if seg.get("brand"))

    def test_digits_and_the_ends_are_protected_as_long_as_possible(self):
        """含数字的段（证据）、品牌段、第一段、最后两段 —— **优先不删**。

        注意这里的规矩是「优先级」而不是「绝对不删」：预算极端紧时（比如只剩 11 个字），
        为了把稿子压进时长只能把能删的都删掉。所以断言分两半：
        品牌段与最后一段**永远**在（它们不在候选里），含数字的段**排在最后**才被删。
        """
        from app.services.prompts.repair import prune_to_budget

        # ① 只需要删一两段时，先删不含数字的那段
        _kept, deleted = prune_to_budget(self.SEGS, target_chars=100, budget_chars=147)
        assert all("67%" not in item["head"] for item in deleted), "含数字的段被优先删了"

        # ② 预算极端紧时，能删的都删；但品牌段/第一段/最后两段仍然留着
        kept, deleted = prune_to_budget(self.SEGS, target_chars=10, budget_chars=11, max_delete=8)
        survivors = {seg["text"] for seg in kept}
        assert any("青稞社区" in text for text in survivors), "品牌段被删了"
        assert "今天就聊到这儿。" in survivors, "最后一段被删了"
        assert deleted

    def test_it_stops_at_the_delete_cap(self):
        from app.services.prompts.repair import prune_to_budget

        kept, deleted = prune_to_budget(self.SEGS, target_chars=1, budget_chars=1, max_delete=1)
        assert len(deleted) == 1, "删段数量必须有上限，不能把稿子删空"

    def test_the_ledger_says_what_was_deleted(self):
        from app.services.prompts.repair import prune_to_budget

        _kept, deleted = prune_to_budget(self.SEGS, target_chars=100, budget_chars=147)
        assert set(deleted[0]) == {"segment", "chars", "head", "unit"}


class TestTheTrimLoopFallsBackToDeletion:
    def test_a_stubborn_script_is_pruned_into_budget(self):
        """两次精简都压不下来 → 整段删，并且台账里记着删了哪些段。"""
        from app.services import prompts
        from app.services.llm import LLMClient

        class _Stubborn:
            calls = 0

            def _chat_json(self, messages, *, max_tokens, temperature=0.6):
                _Stubborn.calls += 1
                # 每次交回一版**更短但仍然超预算**的稿子：两次精简都「压不动」
                text = "这一段的解释很长" * 20
                return {"segments": [{"speaker": "A", "text": text} for _ in range(9)]}

        client = LLMClient.__new__(LLMClient)
        client.settings = None
        client._chat_json = _Stubborn()._chat_json  # type: ignore[method-assign]
        target = prompts.target_chars(3)
        body = "这一段的解释很长" * 25          # 每段 200 字
        segments = [
            {"speaker": "A" if i % 2 == 0 else "B", "text": body,
             **({"brand": "intro"} if i == 0 else {})}
            for i in range(10)
        ]
        script = {"segments": segments, "word_count": sum(len(s["text"]) for s in segments)}
        result = client._repair_length_if_needed(
            script, analysis={}, duration_min=3, level="intro", speech_rate=0,
        )
        ledger = result.get("length_repair") or {}
        assert ledger.get("pruned") is True, ledger
        assert ledger["deleted"], "删段台账是空的"
        assert result["word_count"] <= target * 1.15 + 1
