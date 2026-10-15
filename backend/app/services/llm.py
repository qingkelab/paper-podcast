"""豆包方舟（Volcengine Ark）大模型客户端。

两件事：论文结构化解读、双人播客脚本生成。没有配置密钥时自动降级为 Mock，
使整条流水线在零密钥情况下也能端到端跑通。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import Settings
from . import prompts

logger = logging.getLogger(__name__)

# 长度容差。实测模型对字数预算的遵守程度不稳定（3 分钟档一度欠 35%、
# 另一轮又超 23%），所以两头都要兜：低于下限补一次扩写，高于上限压一次。
# 超出这个区间才多花一次模型调用。
LENGTH_REPAIR_THRESHOLD = 0.9   # 低于目标的 90% → 扩写
LENGTH_TRIM_THRESHOLD = 1.15    # 高于目标的 115% → 精简


# --------------------------------------------------------------------------
# 解析/归一化搬去了 `llm_parse.py`，无密钥时的假数据搬去了 `llm_mock.py`
# --------------------------------------------------------------------------
#
# 为什么搬：这个文件原来是 958 行的「客户端 + 归一化 + Mock 素材」三合一 ——
# 想改一句 Mock 文案得先读一遍 HTTP 错误码映射（用户的原话：
# 「不要每次改都要读全部代码」）。
#
# 依赖方向只能是 llm → llm_parse / llm_mock：`LLMClient.analyze_paper` 要调
# `mock_analysis`，所以 mock 那边绝不能 import 本模块。据此归位两点：
# **`LLMError` 住在 `llm_parse.py`**（`parse_json_response` 要抛它）、
# **`build_script_payload` / `split_long_segments` 也住在 `llm_parse.py`**
# （`llm_mock` 要用它们，放到 mock 里就反了）。
#
# 下面**原样再导出一次**：老调用点（tests 里有几十处 `from .llm import ...`）
# 一行都不用改；新代码请直接 `from .llm_parse import ...` / `from .llm_mock import ...`。
from .llm_mock import (  # noqa: F401
    _MOCK_META_POOL,
    _mock_analysis_en,
    _mock_script_en,
    _pick_mock_meta,
    mock_analysis,
    mock_script,
)
from .llm_parse import (  # noqa: F401
    _BREAK_TOKENS,
    _JSON_FENCE,
    LLMError,
    _as_list,
    _as_str,
    _extract_balanced_object,
    _normalize_analysis,
    _normalize_meta,
    _normalize_segments,
    _split_text,
    build_script_payload,
    parse_json_response,
    split_long_segments,
)


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class LLMClient:
    """论文解读 + 播客脚本生成。

    支持两个提供方，它们都是 OpenAI 兼容的 /chat/completions：
    - DeepSeek（默认）：https://api.deepseek.com
    - 豆包方舟（Ark）：https://ark.cn-beijing.volces.com/api/v3
    切换只改配置，调用代码完全一致。都没配密钥时降级为 Mock。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.mode = settings.llm_mode
        self.mock = self.mode == "mock"
        self.base_url, self.api_key, self.model, self.timeout = settings.llm_credentials
        self.provider_label = {"deepseek": "DeepSeek", "doubao": "豆包方舟"}.get(
            self.mode, "Mock"
        )
        if self.mock:
            logger.warning(
                "大模型未配置（缺少 DEEPSEEK_API_KEY 或 ARK_API_KEY），"
                "解读与脚本将使用 Mock 数据"
            )
        else:
            logger.info("文本解读使用 %s（model=%s）", self.provider_label, self.model)

    # ---------- 对外 ----------

    def analyze_paper(
        self, paper_text: str, *, title_hint: str = "", language: str = "zh"
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回 (paper_meta, analysis)。"""
        if self.mock:
            return mock_analysis(paper_text, title_hint=title_hint, language=language)

        data = self._chat_json(
            prompts.build_analysis_messages(
                paper_text, title_hint=title_hint, language=language
            ),
            max_tokens=4000,
        )
        meta = _normalize_meta(data.get("paper_meta"))
        analysis = _normalize_analysis(data.get("analysis"))
        if not analysis["innovations"] and not analysis["method"]:
            raise LLMError("模型未能给出有效的论文解读内容")
        return meta, analysis

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 4000,
        temperature: float = 0.7,
    ) -> str:
        """通用文本补全，返回模型原始输出。

        用在不需要 JSON 的场景（比如让模型写一张配图 SVG）。
        Mock 模式下抛错，由调用方决定降级策略。
        """
        if self.mock:
            raise LLMError("Mock 模式不支持通用补全")

        payload_messages = messages
        # 这里是自由文本输出（SVG），不能被 response_format=json_object 干扰
        return self._chat(
            payload_messages,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=False,
        )

    def generate_script(
        self,
        analysis: dict[str, Any],
        paper_meta: dict[str, Any] | None,
        *,
        duration_min: int,
        level: str,
        speech_rate: int = 0,
        padding_sec: float = 0.0,
        language: str = "zh",
    ) -> dict[str, Any]:
        """返回 script dict：{segments, word_count, est_duration_sec}。"""
        if self.mock:
            return mock_script(
                analysis,
                paper_meta,
                duration_min=duration_min,
                level=level,
                speech_rate=speech_rate,
                padding_sec=padding_sec,
                language=language,
            )

        data = self._chat_json(
            prompts.build_script_messages(
                analysis,
                paper_meta,
                duration_min=duration_min,
                level=level,
                speech_rate=speech_rate,
                padding_sec=padding_sec,
                language=language,
            ),
            max_tokens=8000,
            temperature=0.9,  # 脚本需要文采，适度放开
        )
        segments = _normalize_segments(data.get("segments"))
        if len(segments) < 4:
            raise LLMError("模型生成的播客脚本过短，无法合成")

        script = build_script_payload(
            segments, speech_rate=speech_rate, padding_sec=padding_sec, language=language
        )
        result = self._repair_length_if_needed(
            script,
            analysis=analysis,
            duration_min=duration_min,
            level=level,
            speech_rate=speech_rate,
            padding_sec=padding_sec,
            language=language,
        )
        self._report_first_person(result, language=language)
        return result

    @staticmethod
    def _report_first_person(script: dict[str, Any], *, language: str) -> None:
        """数一遍「我们/你们」这类第一/第二人称，**只报警不改写**。

        为什么要数：prompt 里早就写着「不要用『我们』冒充作者」，但同一段 prompt 又留了
        「日常口语里的『我们』可以用」这个口子 —— 实测一集 41 段里出现 14 处
        （「我们做了两件事」「我们这篇论文」「你们怎么解决」）。规矩写进 prompt
        不等于模型会守，所以至少要让**不遵守这件事可见**（口径见
        `prompts.text.first_person_hits`）。
        """
        from .prompts.text import first_person_hits

        hits = first_person_hits(script.get("segments") or [], language=language)
        if not hits:
            return
        logger.warning(
            "脚本里有 %d 处第一/第二人称（要求是第三人称讲解）：%s",
            len(hits),
            "；".join(f"第 {index} 段「{text[:18]}…」" for index, text in hits[:5]),
        )

    def _repair_length_if_needed(
        self,
        script: dict[str, Any],
        *,
        analysis: dict[str, Any],
        duration_min: int,
        level: str,
        speech_rate: int,
        padding_sec: float = 0.0,
        language: str = "zh",
    ) -> dict[str, Any]:
        """生成后做一次确定性长度检查，偏短就补一次扩写。

        为什么不只靠 prompt：实测同一套 prompt 下，3 分钟档超出 7%、
        5 分钟档却欠了 35%（1065 字 vs 目标 1650 字）。模型对字数的遵守
        程度不稳定，所以用「检查实际字数 → 不达标才补救」兜底，
        而不是反复调措辞。只补一次，避免无限循环和成本失控。
        """
        target = prompts.target_chars(duration_min, speech_rate, padding_sec, language)
        actual = script.get("word_count", 0)

        if actual > target * LENGTH_TRIM_THRESHOLD:
            # **最多两次**，而且第二次要**带着第一次的真实结果**去（不是重来一遍）。
            #
            # 改之前是这样：两次都拿**原稿**去压，第二次只多一句「这次必须砍到 X 字」。
            # 模型看不到上一次砍到多少、哪几段几乎没动，于是经常把同样那几段再砍一遍 ——
            # 实测两集就是 1572 字（目标 1050，150%）原地打转。
            # 现在：① 上一版**更短就用它当下一次的原稿**（不把已砍掉的部分还回去）；
            # ② 把「上一版多少字、还多多少、每段砍了多少」一起喂进去。
            current = script
            previous: dict[str, Any] | None = None
            attempts: list[dict[str, Any]] = []
            for attempt in (1, 2):
                detail: dict[str, Any] = {}
                trimmed = self._trim_script(
                    current,
                    target=target,
                    speech_rate=speech_rate,
                    padding_sec=padding_sec,
                    language=language,
                    attempt=attempt,
                    previous=previous,
                    detail=detail,
                )
                attempts.append(
                    {
                        "attempt": attempt,
                        "before": detail.get("before"),
                        "after": detail.get("after"),
                        "target": target,
                        "budget": int(target * LENGTH_TRIM_THRESHOLD),
                        "accepted": bool(detail.get("accepted")),
                    }
                )
                if detail.get("accepted"):
                    trimmed["length_repair"] = {"attempts": attempts}
                    return trimmed
                candidate = detail.get("candidate")
                if candidate is not None:
                    # 这一版虽然还超预算，但确实更短了 → 拿它当下一次的原稿
                    current = candidate
                    previous = {
                        "chars": detail.get("after"),
                        "base_chars": detail.get("before"),
                        "base_segments": current.get("segments", []),
                    }
                if attempt == 2:
                    best = detail.get("after")
                    if candidate is not None and best is not None and best < actual:
                        logger.warning(
                            "两次精简都没压进预算（%d → %d 字，目标 %d，预算上限 %d），"
                            "沿用**更短的那一版**（比原稿短 %d 字）",
                            actual,
                            best,
                            target,
                            int(target * LENGTH_TRIM_THRESHOLD),
                            actual - best,
                        )
                        candidate["length_repair"] = {
                            "attempts": attempts,
                            "still_over_budget": True,
                        }
                        return candidate
                    logger.warning(
                        "两次精简都没压进预算（%d 字，目标 %d，预算上限 %d），沿用原脚本",
                        trimmed.get("word_count", actual),
                        target,
                        int(target * LENGTH_TRIM_THRESHOLD),
                    )
                    trimmed["length_repair"] = {
                        "attempts": attempts,
                        "still_over_budget": True,
                    }
                    return trimmed
            return script

        if actual >= target * LENGTH_REPAIR_THRESHOLD:
            return script

        logger.info(
            "脚本偏短（%d 字 / 目标 %d 字，达 %.0f%%），追加一次扩写",
            actual,
            target,
            actual / max(target, 1) * 100,
        )

        try:
            data = self._chat_json(
                prompts.build_expand_messages(
                    script["segments"],
                    current_chars=actual,
                    target=target,
                    language=language,
                ),
                max_tokens=12000,
                temperature=0.85,
            )
        except LLMError as exc:
            # 扩写失败不该让整个任务失败：原脚本本身可用，只是短一些。
            logger.warning("扩写失败，沿用原脚本：%s", exc)
            return script

        expanded = _normalize_segments(data.get("segments"))
        if len(expanded) < 4:
            logger.warning("扩写结果无效（%d 段），沿用原脚本", len(expanded))
            return script

        repaired = build_script_payload(
            expanded, speech_rate=speech_rate, padding_sec=padding_sec, language=language
        )

        # 扩写反而更短说明模型没按要求做，保留原稿更稳妥
        if repaired["word_count"] <= actual:
            logger.warning(
                "扩写后并未变长（%d → %d 字），沿用原脚本",
                actual,
                repaired["word_count"],
            )
            return script

        logger.info(
            "扩写完成：%d → %d 字（目标 %d 字）", actual, repaired["word_count"], target
        )
        return repaired

    def _trim_script(
        self,
        script: dict[str, Any],
        *,
        target: int,
        speech_rate: int,
        padding_sec: float = 0.0,
        language: str = "zh",
        attempt: int = 1,
        previous: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """脚本超长时压缩一次。

        与扩写对称：只做一次、失败就保留原稿（返回值）。超长比偏短危害小，
        所以宁可保留原稿（听众多听一会儿）也不要因为压缩丢掉关键信息。

        `previous` 是**上一次精简的真实结果**（`chars` / `base_chars` / `base_segments`），
        会作为修复线索进 prompt —— 见 `prompts.repair.trim_feedback_lines`。
        `detail` 是出参：把这一轮的 before/after/是否采纳、以及**被拒的那一版稿子**
        （`candidate`）交回调用方 —— 「压不到预算」不等于「白压」，下一轮就从那一版继续，
        而不是把已经砍掉的部分还回去重来。
        """
        actual = script.get("word_count", 0)
        logger.info(
            "脚本偏长（%d 字 / 目标 %d 字，达 %.0f%%），追加一次精简",
            actual,
            target,
            actual / max(target, 1) * 100,
        )

        try:
            data = self._chat_json(
                prompts.build_trim_messages(
                    script["segments"],
                    current_chars=actual,
                    target=target,
                    language=language,
                    attempt=attempt,
                    previous=previous,
                ),
                max_tokens=8000,
                temperature=0.4,  # 压缩要稳，不要发挥
            )
        except LLMError as exc:
            logger.warning("精简失败，沿用原脚本：%s", exc)
            if detail is not None:
                detail.update({"attempt": attempt, "before": actual, "error": str(exc)[:120], "accepted": False})
            return script

        trimmed_segments = _normalize_segments(data.get("segments"))
        if len(trimmed_segments) < 4:
            logger.warning("精简结果无效（%d 段），沿用原脚本", len(trimmed_segments))
            if detail is not None:
                detail.update(
                    {"attempt": attempt, "before": actual, "error": f"精简结果只有 {len(trimmed_segments)} 段", "accepted": False}
                )
            return script

        trimmed = build_script_payload(
            trimmed_segments,
            speech_rate=speech_rate,
            padding_sec=padding_sec,
            language=language,
        )

        # **接受条件：落进预算就算成功**（≤ 目标 × LENGTH_TRIM_THRESHOLD）。
        #
        # 原来要求「至少减 3%」，这是个坏判据：模型一次没压到 1050、只压到 1200 时，
        # 那个结果**已经在预算内**（1200 ≤ 1050×1.15），却因为「减得不够 3%」被整份丢掉，
        # 白付一次调用、还留着 1572 字的原稿。实测有两集正是 1572 字 / 目标 1050（150%）。
        if detail is not None:
            detail.update(
                {
                    "attempt": attempt,
                    "before": actual,
                    "after": trimmed["word_count"],
                    "target": target,
                    "budget": int(target * LENGTH_TRIM_THRESHOLD),
                }
            )

        budget = target * LENGTH_TRIM_THRESHOLD
        if trimmed["word_count"] > budget:
            logger.warning(
                "精简不够（%d → %d 字，目标 %d，预算上限 %d），稍后重试或沿用原脚本",
                actual,
                trimmed["word_count"],
                target,
                int(budget),
            )
            if detail is not None:
                # 被拒**但确实更短**的那一版：下一轮从它继续压，别把砍掉的还回去
                detail["candidate"] = trimmed
                detail["accepted"] = False
            return script
        reduction = (actual - trimmed["word_count"]) / max(actual, 1)
        if detail is not None:
            detail["accepted"] = True
        logger.info(
            "精简接受：%d → %d 字（目标 %d，预算上限 %d，减了 %.1f%%）",
            actual,
            trimmed["word_count"],
            target,
            int(budget),
            reduction * 100,
        )

        # 压过头了（低于下限）也不要——宁可长一点
        if trimmed["word_count"] < target * LENGTH_REPAIR_THRESHOLD:
            logger.warning(
                "精简过度（%d → %d 字，低于目标 %d 字的 %d%%），沿用原脚本",
                actual,
                trimmed["word_count"],
                target,
                int(LENGTH_REPAIR_THRESHOLD * 100),
            )
            if detail is not None:
                detail["accepted"] = False        # 压过头了，不能当下一轮的底稿
            return script

        logger.info("精简完成：%d → %d 字（目标 %d 字）", actual, trimmed["word_count"], target)
        return trimmed

    # ---------- 内部 ----------

    def _chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float = 0.6,
    ) -> dict[str, Any]:
        raw = self._chat(
            messages, max_tokens=max_tokens, temperature=temperature, json_mode=True
        )
        return parse_json_response(raw)

    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool = True,
    ) -> str:
        # **发请求前把孤立代理字符清掉**：这种字符存不进 UTF-8，httpx 会在
        # `json=payload` 里抛 UnicodeEncodeError（错误信息只有位置、没有上下文，
        # 极难定位）。入口那层已经清过一次，这里再兜一道 —— 粘贴文本、模型输出回灌
        # 这些路径不经过 PDF 抽取（见 `ingest.drop_lone_surrogates`）。
        from .ingest import drop_lone_surrogates

        messages = [
            {**message, "content": drop_lone_surrogates(str(message.get("content") or ""))}
            for message in messages
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            # 两个提供方都支持 JSON 输出模式。开启后模型不会在外面裹客套话，
            # 解析成功率显著提升（parse_json_response 仍保留兜底）。
            # 但生成 SVG 时必须关掉，否则模型会把 SVG 塞进 JSON 字符串里。
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url.rstrip('/')}/chat/completions"

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError(f"{self.provider_label} 请求超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.provider_label} 请求失败：{exc}") from exc

        if response.status_code == 401:
            raise LLMError(
                f"{self.provider_label} 鉴权失败（401），请检查 API Key 是否正确/是否欠费"
            )
        if response.status_code == 402:
            raise LLMError(f"{self.provider_label} 账户余额不足（402）")
        if response.status_code == 404:
            raise LLMError(
                f"{self.provider_label} 接口或模型不存在（404）：model={self.model!r}，"
                f"base_url={self.base_url}"
            )
        if response.status_code == 429:
            raise LLMError(f"{self.provider_label} 触发限流（429），请稍后重试")
        if response.status_code >= 400:
            raise LLMError(
                f"{self.provider_label} 返回错误 {response.status_code}：{response.text[:300]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError(f"{self.provider_label} 返回了非 JSON 响应") from exc

        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"{self.provider_label} 返回中没有 choices：{str(body)[:200]}")
        content = (choices[0].get("message") or {}).get("content") or ""
        if not isinstance(content, str) or not content.strip():
            raise LLMError(f"{self.provider_label} 返回了空 message")

        usage = body.get("usage") or {}
        if usage:
            logger.info(
                "%s 用量：prompt=%s completion=%s",
                self.provider_label,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
            )
        return content
