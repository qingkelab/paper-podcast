"""大模型提供方（DeepSeek / 豆包方舟 / Mock）的解析与请求构造测试。

不需要任何真实密钥：HTTP 层用 httpx.MockTransport 拦截。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.services import llm as llm_module
from app.services.llm import LLMClient, LLMError


def make_settings(**overrides) -> Settings:
    base = dict(
        force_mock=False,
        llm_provider="auto",
        deepseek_api_key="",
        ark_api_key="",
        ark_model="",
    )
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------
# 提供方解析
# --------------------------------------------------------------------------


class TestProviderResolution:
    def test_mock_when_no_keys(self):
        assert make_settings().llm_mode == "mock"

    def test_auto_prefers_deepseek(self):
        s = make_settings(deepseek_api_key="ds-key", ark_api_key="ark-key", ark_model="ep-1")
        assert s.llm_mode == "deepseek"

    def test_auto_falls_back_to_doubao(self):
        s = make_settings(ark_api_key="ark-key", ark_model="ep-1")
        assert s.llm_mode == "doubao"

    def test_ark_without_model_is_not_usable(self):
        """方舟必须有接入点/模型 ID 才能调用，只给 key 不算就绪。"""
        assert make_settings(ark_api_key="ark-key").llm_mode == "mock"

    def test_explicit_provider_overrides_priority(self):
        s = make_settings(
            llm_provider="doubao",
            deepseek_api_key="ds-key",
            ark_api_key="ark-key",
            ark_model="ep-1",
        )
        assert s.llm_mode == "doubao"

    def test_explicit_provider_without_key_is_mock(self):
        s = make_settings(llm_provider="deepseek", ark_api_key="ark-key", ark_model="ep-1")
        assert s.llm_mode == "mock"

    def test_force_mock_wins_over_everything(self):
        s = make_settings(
            force_mock=True,
            deepseek_api_key="ds-key",
            ark_api_key="ark-key",
            ark_model="ep-1",
        )
        assert s.llm_mode == "mock"


class TestCredentials:
    def test_deepseek_credentials(self):
        s = make_settings(deepseek_api_key="ds-key")
        base_url, key, model, _ = s.llm_credentials
        assert base_url == "https://api.deepseek.com"
        assert key == "ds-key"
        assert model == "deepseek-chat"

    def test_doubao_credentials(self):
        s = make_settings(ark_api_key="ark-key", ark_model="ep-123")
        base_url, key, model, _ = s.llm_credentials
        assert "ark.cn-beijing.volces.com" in base_url
        assert key == "ark-key"
        assert model == "ep-123"


# --------------------------------------------------------------------------
# 请求构造
# --------------------------------------------------------------------------


@pytest.fixture
def capture_request(monkeypatch):
    """把 httpx.Client 换成一个带 MockTransport 的客户端，并记录请求。"""
    captured: dict = {}

    def make(settings: Settings, response_body: dict | None = None, status: int = 200):
        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                status,
                json=response_body
                or {"choices": [{"message": {"content": '{"ok": true}'}}]},
            )

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        monkeypatch.setattr(
            llm_module.httpx, "Client", lambda **kwargs: real_client(transport=transport)
        )
        return LLMClient(settings), captured

    return make


class TestRequestShape:
    def test_deepseek_url_and_auth(self, capture_request):
        client, captured = capture_request(make_settings(deepseek_api_key="ds-key"))
        client._chat([{"role": "user", "content": "hi"}], max_tokens=100, temperature=0.6)

        assert captured["method"] == "POST"
        assert captured["url"] == "https://api.deepseek.com/chat/completions"
        assert captured["headers"]["authorization"] == "Bearer ds-key"

    def test_doubao_url_and_auth(self, capture_request):
        client, captured = capture_request(
            make_settings(ark_api_key="ark-key", ark_model="ep-123")
        )
        client._chat([{"role": "user", "content": "hi"}], max_tokens=100, temperature=0.6)

        assert captured["url"] == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        assert captured["headers"]["authorization"] == "Bearer ark-key"
        assert captured["body"]["model"] == "ep-123"

    def test_uses_json_output_mode(self, capture_request):
        """两个提供方都支持 json_object，开启后模型不会在外面裹客套话。"""
        client, captured = capture_request(make_settings(deepseek_api_key="ds-key"))
        client._chat([{"role": "user", "content": "hi"}], max_tokens=100, temperature=0.6)
        assert captured["body"]["response_format"] == {"type": "json_object"}

    def test_trailing_slash_in_base_url_is_normalized(self, capture_request):
        client, captured = capture_request(
            make_settings(deepseek_api_key="k", deepseek_base_url="https://api.deepseek.com/")
        )
        client._chat([{"role": "user", "content": "hi"}], max_tokens=1, temperature=0)
        assert captured["url"] == "https://api.deepseek.com/chat/completions"


# --------------------------------------------------------------------------
# 错误映射
# --------------------------------------------------------------------------


class TestErrorMapping:
    @pytest.mark.parametrize(
        "status,fragment",
        [
            (401, "鉴权失败"),
            (402, "余额不足"),
            (404, "不存在"),
            (429, "限流"),
            (500, "返回错误 500"),
        ],
    )
    def test_http_errors_become_actionable_messages(self, capture_request, status, fragment):
        client, _ = capture_request(make_settings(deepseek_api_key="k"), status=status)
        with pytest.raises(LLMError) as exc:
            client._chat([{"role": "user", "content": "hi"}], max_tokens=1, temperature=0)
        assert fragment in str(exc.value)

    def test_error_message_names_the_provider(self, capture_request):
        """报错要指明是哪个提供方，否则排查时不知道该去看谁的控制台。"""
        client, _ = capture_request(make_settings(deepseek_api_key="k"), status=401)
        with pytest.raises(LLMError) as exc:
            client._chat([{"role": "user", "content": "hi"}], max_tokens=1, temperature=0)
        assert "DeepSeek" in str(exc.value)

    def test_empty_choices_is_rejected(self, capture_request):
        client, _ = capture_request(
            make_settings(deepseek_api_key="k"), response_body={"choices": []}
        )
        with pytest.raises(LLMError):
            client._chat([{"role": "user", "content": "hi"}], max_tokens=1, temperature=0)


class TestMockFallback:
    def test_mock_client_does_not_touch_network(self, monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError("Mock 模式不应该发起任何网络请求")

        monkeypatch.setattr(llm_module.httpx, "Client", explode)
        client = LLMClient(make_settings())

        meta, analysis = client.analyze_paper("attention is all you need " * 50)
        assert meta["title"]
        assert analysis["innovations"]

        script = client.generate_script(analysis, meta, duration_min=5, level="intro")
        assert script["segments"]
        assert {s["speaker"] for s in script["segments"]} == {"A", "B"}


# --------------------------------------------------------------------------
# 长度修复（扩写补足）
# --------------------------------------------------------------------------


class TestLengthRepair:
    """模型不守字数预算是实测出来的问题，不能只靠 prompt 措辞。

    实测：同一套 prompt，3 分钟档超出 7%，5 分钟档欠 35%。
    所以生成后要做确定性检查，偏短才补一次扩写。
    """

    @staticmethod
    def _client_with_calls(monkeypatch, script_chars: int, expanded_chars: int | None):
        """构造一个 LLMClient，把两次调用的返回内容都控制住。"""
        calls: list[dict] = []

        def fake_chat_json(self, messages, *, max_tokens, temperature=0.6):
            calls.append({"messages": messages, "max_tokens": max_tokens})
            if len(calls) == 1:
                n = script_chars
            else:
                if expanded_chars is None:
                    raise LLMError("扩写调用失败")
                n = expanded_chars
            # 造一段总字数可控的合法脚本
            per = max(n // 6, 1)
            return {
                "segments": [
                    {"speaker": "A" if i % 2 == 0 else "B", "text": "字" * per}
                    for i in range(6)
                ]
            }

        monkeypatch.setattr(LLMClient, "_chat_json", fake_chat_json)
        return LLMClient(make_settings(deepseek_api_key="k")), calls

    def test_short_script_triggers_expansion(self, monkeypatch):
        from app.services import prompts

        target = prompts.target_chars(5)
        client, calls = self._client_with_calls(
            monkeypatch, script_chars=int(target * 0.5), expanded_chars=target
        )
        script = client.generate_script({}, {}, duration_min=5, level="intro")

        assert len(calls) == 2, "偏短的脚本应当触发第二次扩写调用"
        assert script["word_count"] >= target * 0.9

    def test_expansion_prompt_asks_for_whole_script(self, monkeypatch):
        """扩写必须要求输出完整脚本，否则我们会把片段当成整篇。"""
        from app.services import prompts

        target = prompts.target_chars(5)
        client, calls = self._client_with_calls(
            monkeypatch, script_chars=int(target * 0.5), expanded_chars=target
        )
        client.generate_script({}, {}, duration_min=5, level="intro")

        expand_user = calls[1]["messages"][-1]["content"]
        assert "完整脚本" in expand_user
        assert "不是只输出新增部分" in expand_user

    def test_adequate_script_skips_expansion(self, monkeypatch):
        """字数达标就不该多花一次调用（成本和延迟都要省）。"""
        from app.services import prompts

        target = prompts.target_chars(5)
        client, calls = self._client_with_calls(
            monkeypatch, script_chars=int(target * 1.0), expanded_chars=None
        )
        client.generate_script({}, {}, duration_min=5, level="intro")
        assert len(calls) == 1

    def test_expansion_failure_keeps_original(self, monkeypatch):
        """扩写失败不能让任务挂掉——原脚本本身是可用的。"""
        from app.services import prompts

        target = prompts.target_chars(5)
        client, calls = self._client_with_calls(
            monkeypatch, script_chars=int(target * 0.5), expanded_chars=None
        )
        script = client.generate_script({}, {}, duration_min=5, level="intro")

        assert len(calls) == 2
        assert script["segments"], "扩写失败时应当保留原脚本"
        assert script["word_count"] > 0

    def test_expansion_that_gets_shorter_is_discarded(self, monkeypatch):
        """扩写结果反而更短，说明模型没按要求做，保留原稿。"""
        from app.services import prompts

        target = prompts.target_chars(5)
        original = int(target * 0.5)
        client, _ = self._client_with_calls(
            monkeypatch, script_chars=original, expanded_chars=original // 2
        )
        script = client.generate_script({}, {}, duration_min=5, level="intro")
        assert script["word_count"] >= original * 0.9
