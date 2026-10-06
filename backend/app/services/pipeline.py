"""流水线编排：论文文本 → 结构化解读 → 双人脚本 → 播客音频 → 落库。

进度映射（与 docs/API.md 的状态机一致）：
    parsing 10 → analyzing 35 → scripting 55 → synthesizing 75 → completed 100
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import Database
from . import prompts
from .ingest import (
    IngestError,
    clean_text,
    extract_pdf_text,
    fetch_url_text,
    guess_arxiv_id,
    guess_title,
    truncate_smart,
)
from .figures import extract_figures, render_first_page
from .illustration import generate_illustration
from .llm import LLMClient, LLMError
from .podcast_tts import PodcastTTSClient, PodcastTTSError, probe_duration
from .video import VideoError, compose_video, ffmpeg_available

logger = logging.getLogger(__name__)

# 阶段 -> (status, progress, 中文文案)
STAGES = {
    "parsing": ("parsing", 10, "正在解析论文"),
    "analyzing": ("analyzing", 35, "正在深度解读"),
    "scripting": ("scripting", 55, "正在生成播客脚本"),
    "synthesizing": ("synthesizing", 75, "正在合成播客音频"),
    "completed": ("completed", 100, "已完成"),
}


class Pipeline:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.llm = LLMClient(settings)
        self.tts = PodcastTTSClient(settings)

    # ---------- 工具 ----------

    def _set_stage(self, episode_id: str, stage: str, **extra: Any) -> None:
        status, progress, label = STAGES[stage]
        self.db.update_episode(
            episode_id, status=status, progress=progress, stage_label=label, **extra
        )

    def _fail(self, episode_id: str, message: str) -> None:
        logger.error("任务 %s 失败：%s", episode_id, message)
        self.db.update_episode(
            episode_id,
            status="failed",
            stage_label="失败",
            error=message,
        )

    async def _compose_video_safely(
        self,
        episode_id: str,
        *,
        segments: list[dict[str, Any]],
        timings: list[Any],
        audio_path: Path,
        audio_duration: float,
        title: str,
        analysis: dict[str, Any],
    ) -> dict[str, Any]:
        """合成视频解读播客。

        视频是增强项：任何失败都只记日志并返回空字段，绝不影响已经可用的音频。
        所以要放在音频落库之后。

        用 to_thread 跑：compose_video 是同步的，而且现在会为缺少原图的段落
        现场生成配图（每张约 10 秒）。直接调用会**阻塞事件循环**几十秒，
        期间前端轮询拿不到任何响应，看起来像服务挂了。
        """
        if not self.settings.enable_video:
            logger.info("视频合成已关闭（ENABLE_VIDEO=false）")
            return {}
        if not ffmpeg_available():
            logger.warning("系统未安装 ffmpeg，跳过视频合成")
            return {}
        if not timings:
            logger.warning("没有逐段时序，跳过视频合成")
            return {}

        record = self.db.get_episode(episode_id) or {}
        illustration = record.get("illustration") or {}

        try:
            result = await asyncio.to_thread(
                compose_video,
                segments=segments,
                timings=timings,
                audio_path=audio_path,
                audio_duration=audio_duration,
                cover_path=record.get("cover_path"),
                figures=record.get("figures") or [],
                illustration_png=illustration.get("png_path"),
                work_dir=self.settings.video_work_dir / episode_id,
                output_path=self.settings.video_dir / f"{episode_id}.mp4",
                title=title,
                llm=self.llm,
                analysis=analysis,
                max_topic_images=self.settings.max_topic_images,
            )
        except VideoError as exc:
            logger.warning("视频合成失败（音频不受影响）：%s", exc)
            return {}
        except Exception as exc:  # noqa: BLE001
            logger.exception("视频合成出现未预期错误（音频不受影响）")
            return {}

        logger.info(
            "视频已生成：%d 帧 / %.1f 秒 / %.1f MB（配图分配：%s）",
            result.scene_count,
            result.duration_sec,
            result.bytes_written / 1024 / 1024,
            result.assignment,
        )
        return {
            "video_path": str(result.video_path),
            "video": {
                **result.to_dict(),
                "url": f"/api/episodes/{episode_id}/video",
            },
        }

    async def rebuild_video(self, episode_id: str) -> dict[str, Any]:
        """用现有素材重新合成视频（配图被人工校正后用）。

        关键点：**复用上次的画面分配，不再问模型**。因为
        1) 再问一次结果可能不一样，用户会觉得「我就转了个图，怎么画面全变了」
        2) 主题图会被重新生成一遍（4 次模型调用 + 几十秒），纯属浪费

        音频、脚本、解读都不动 —— 只重新渲染幻灯片并编码。
        """
        record = self.db.get_episode(episode_id)
        if not record:
            raise VideoError("播客不存在")

        stored = record.get("video") or {}
        timings_raw = record.get("timings") or []
        script = record.get("script") or {}
        segments = script.get("segments") or []

        if not timings_raw or not segments:
            raise VideoError("缺少脚本或时间轴，无法重新合成")
        if not record.get("audio_path") or not Path(record["audio_path"]).exists():
            raise VideoError("音频已丢失，无法重新合成")
        if not ffmpeg_available():
            raise VideoError("系统未安装 ffmpeg，无法合成视频")

        from .podcast_tts import RoundTiming

        timings = [
            RoundTiming(
                index=int(t.get("index", i)),
                speaker=str(t.get("speaker") or ""),
                start=float(t.get("start") or 0.0),
                end=float(t.get("end") or 0.0),
            )
            for i, t in enumerate(timings_raw)
        ]

        result = await asyncio.to_thread(
            compose_video,
            segments=segments,
            timings=timings,
            audio_path=Path(record["audio_path"]),
            audio_duration=float(record.get("audio_duration_sec") or 0.0),
            cover_path=record.get("cover_path"),
            figures=record.get("figures") or [],
            illustration_png=(record.get("illustration") or {}).get("png_path"),
            work_dir=self.settings.video_work_dir / episode_id,
            output_path=self.settings.video_dir / f"{episode_id}.mp4",
            title=record.get("title") or "论文解读",
            llm=None,
            analysis=record.get("analysis") or {},
            preset_scenes=stored.get("scenes") or None,
            preset_assets=stored.get("assets") or None,
        )

        fields = {
            "video_path": str(result.video_path),
            "video": {
                **result.to_dict(),
                "url": f"/api/episodes/{episode_id}/video",
            },
        }
        self.db.update_episode(episode_id, **fields)
        logger.info(
            "视频已重新合成：%d 帧 / %.1f 秒（复用画面分配：%s）",
            result.scene_count,
            result.duration_sec,
            result.assignment,
        )
        return fields

    # ---------- 解析阶段 ----------

    def resolve_paper(self, episode: dict[str, Any]) -> tuple[str, bytes | None]:
        """把三种来源统一成清洗后的正文，并尽量带出 PDF 原始字节。

        返回 (正文, PDF字节或None)。配图和封面都要从原始 PDF 渲染，
        所以这里把字节一并带出来，避免二次下载。
        """
        source_type = episode["source_type"]
        source_ref = episode.get("source_ref") or ""
        pdf_bytes: bytes | None = None

        if source_type == "pdf":
            path = Path(source_ref)
            if not path.exists():
                raise IngestError("上传的 PDF 文件已丢失，请重新上传")
            pdf_bytes = path.read_bytes()
            raw = extract_pdf_text(pdf_bytes)

        elif source_type == "url":
            raw, _, pdf_bytes = fetch_url_text(source_ref)

        elif source_type == "text":
            raw = episode.get("raw_text") or source_ref
            raw = clean_text(raw)

        else:
            raise IngestError(f"不支持的来源类型：{source_type}")

        text = clean_text(raw)
        if len(text) < 200:
            raise IngestError("论文有效正文过短（少于 200 字），无法生成有内容的解读")

        return truncate_smart(text, self.settings.max_paper_chars), pdf_bytes

    # ---------- 主流程 ----------

    async def run(self, episode_id: str) -> bool:
        """执行完整流水线。返回是否成功。异常会被捕获并写入 failed 状态。"""
        episode = self.db.get_episode(episode_id)
        if not episode:
            logger.warning("任务 %s 不存在，跳过", episode_id)
            return False

        options = episode.get("options") or {}
        duration_min = int(options.get("duration_min") or 5)
        level = str(options.get("level") or "intro")
        voice_a = options.get("voice_a") or self.settings.default_voice_a
        voice_b = options.get("voice_b") or self.settings.default_voice_b
        speech_rate = int(self.settings.podcast_speech_rate or 0)

        try:
            # ---- 1. 解析 ----
            self._set_stage(episode_id, "parsing")
            paper_text, pdf_bytes = self.resolve_paper(episode)

            # 封面 = PDF 第一页；正文插图按图注提取。
            # 两者都是增强项，失败只记日志，绝不让整期播客失败。
            cover_fields: dict[str, Any] = {}
            if pdf_bytes:
                cover = render_first_page(pdf_bytes, self.settings.cover_dir / f"{episode_id}.png")
                if cover:
                    cover_path, cover_w, cover_h = cover
                    cover_fields = {
                        "cover_path": str(cover_path),
                        "cover_width": cover_w,
                        "cover_height": cover_h,
                    }
                figures = extract_figures(
                    pdf_bytes,
                    self.settings.figure_dir,
                    episode_id,
                    auto_upright=self.settings.auto_upright_figures,
                )
                if figures:
                    self.db.update_episode(
                        episode_id, figures=[f.to_dict() for f in figures]
                    )
                if cover_fields:
                    self.db.update_episode(episode_id, **cover_fields)

            title_hint = episode.get("title") or ""
            if not title_hint or title_hint == "处理中":
                title_hint = guess_title(paper_text, fallback=title_hint or "未命名论文")

            # ---- 2. 解读 ----
            self._set_stage(episode_id, "analyzing", title=title_hint or "未命名论文")
            paper_meta, analysis = self.llm.analyze_paper(paper_text, title_hint=title_hint)

            # 用户显式指定的标题优先；否则采用模型从正文里认出的正式标题
            title_locked = bool(options.get("title_locked"))
            if paper_meta.get("title") and not title_locked:
                title_hint = paper_meta["title"]
            if not paper_meta.get("arxiv_id"):
                paper_meta["arxiv_id"] = guess_arxiv_id(episode.get("source_ref") or "")

            self.db.update_episode(
                episode_id,
                title=title_hint or "未命名论文",
                paper_meta=paper_meta,
                analysis=analysis,
            )

            # 生成信息图。text 来源没有 PDF，就用它当封面。
            illustration = generate_illustration(
                self.llm,
                analysis,
                paper_meta,
                self.settings.illustration_dir,
                episode_id,
            )
            update_fields: dict[str, Any] = {
                "illustration": illustration.to_dict()
            }
            if not cover_fields and illustration.png_path:
                update_fields["cover_path"] = illustration.png_path
                update_fields["cover_width"] = illustration.width
                update_fields["cover_height"] = illustration.height
            self.db.update_episode(episode_id, **update_fields)

            # ---- 3. 脚本 ----
            self._set_stage(episode_id, "scripting")
            script = self.llm.generate_script(
                analysis,
                paper_meta,
                duration_min=duration_min,
                level=level,
                speech_rate=speech_rate,
            )
            self.db.update_episode(episode_id, script=script)

            # ---- 4. 合成 ----
            self._set_stage(episode_id, "synthesizing")
            audio_path = self.settings.audio_dir / f"{episode_id}.mp3"

            def on_round(index: int, speaker: str) -> None:
                total = max(len(script["segments"]), 1)
                # 合成阶段占 75 → 99 的进度区间
                progress = 75 + int(24 * (index + 1) / total)
                self.db.update_episode(
                    episode_id,
                    progress=min(progress, 99),
                    stage_label=f"正在合成播客音频（第 {index + 1}/{total} 段）",
                )

            result = await self.tts.synthesize(
                segments=script["segments"],
                voice_a=voice_a,
                voice_b=voice_b,
                output_path=audio_path,
                on_round=on_round,
                speech_rate=speech_rate,
            )

            # 先把音频和逐段时序落库——即使视频合成失败，音频也已经可用了
            self.db.update_episode(
                episode_id,
                audio_path=str(result.audio_path),
                audio_duration_sec=result.duration_sec,
                audio_bytes=result.bytes_written,
                podcast_task_id=result.task_id,
                finished_round=result.finished_round,
                timings=[t.to_dict() for t in result.timings],
            )

            # ---- 5. 视频 ----
            video_fields = await self._compose_video_safely(
                episode_id,
                segments=script["segments"],
                timings=result.timings,
                audio_path=result.audio_path,
                audio_duration=result.duration_sec or 0.0,
                title=title_hint or "论文解读",
                analysis=analysis,
            )

            # ---- 6. 完成 ----
            self.db.update_episode(
                episode_id,
                status="completed",
                progress=100,
                stage_label="已完成",
                error=None,
                **video_fields,
            )
            logger.info("任务 %s 完成：%s", episode_id, result.audio_path)
            return True

        except (IngestError, LLMError, PodcastTTSError) as exc:
            self._fail(episode_id, str(exc))
            return False
        except Exception as exc:  # noqa: BLE001 - 兜底，避免 worker 静默死掉
            logger.exception("任务 %s 出现未预期错误", episode_id)
            self._fail(episode_id, f"内部错误：{exc}")
            return False


# --------------------------------------------------------------------------
# 产物导出（供下载接口使用）
# --------------------------------------------------------------------------


def render_script_text(episode: dict[str, Any]) -> str:
    script = episode.get("script") or {}
    segments = script.get("segments") or []
    if not segments:
        return "（暂无脚本）"

    lines = [f"《{episode.get('title') or '未命名论文'}》", "双人播客脚本", "=" * 40, ""]
    for segment in segments:
        speaker = "主播A" if segment.get("speaker") == "A" else "主播B"
        lines.append(f"【{speaker}】{segment.get('text', '')}")
        lines.append("")
    return "\n".join(lines)


def _markdown_section(heading: str, body: Any) -> list[str]:
    """把一个小节渲染成 Markdown 行。纯函数，不依赖外部可变状态。

    （早先这里写成闭包内 `out += ...`，会因 Python 的 augmented assignment 规则
    让 `out` 变成闭包局部变量并抛 UnboundLocalError，所以改成返回列表。）
    """
    if not body:
        return []
    if isinstance(body, list):
        items = [str(item) for item in body if str(item).strip()]
        if not items:
            return []
        return [f"## {heading}", "", *[f"- {item}" for item in items], ""]
    return [f"## {heading}", "", str(body), ""]


def render_analysis_markdown(episode: dict[str, Any]) -> str:
    meta = episode.get("paper_meta") or {}
    analysis = episode.get("analysis") or {}
    title = episode.get("title") or meta.get("title") or "未命名论文"

    out: list[str] = [f"# {title}", ""]

    facts: list[str] = []
    if meta.get("authors"):
        facts.append(f"**作者**：{', '.join(meta['authors'])}")
    if meta.get("venue") or meta.get("year"):
        facts.append(f"**发表**：{meta.get('venue') or ''} {meta.get('year') or ''}".strip())
    if meta.get("arxiv_id"):
        facts.append(f"**arXiv**：[{meta['arxiv_id']}](https://arxiv.org/abs/{meta['arxiv_id']})")
    if meta.get("keywords"):
        facts.append(f"**关键词**：{', '.join(meta['keywords'])}")
    if facts:
        out.extend(facts)
        out.append("")

    if meta.get("abstract"):
        out.extend(["## 摘要", "", meta["abstract"], ""])

    for heading, body in (
        ("研究背景", analysis.get("background")),
        ("核心创新点", analysis.get("innovations")),
        ("研究方法", analysis.get("method")),
        ("实验结果", analysis.get("experiments")),
        ("核心结论", analysis.get("conclusion")),
        ("存在不足", analysis.get("limitations")),
        ("行业应用价值", analysis.get("value")),
        ("未来研究方向", analysis.get("future")),
    ):
        out.extend(_markdown_section(heading, body))

    script = episode.get("script") or {}
    if script.get("segments"):
        minutes = (script.get("est_duration_sec") or 0) / 60
        out += [
            "---",
            "",
            f"*播客脚本 {script.get('word_count', 0)} 字，预计时长约 {minutes:.1f} 分钟*",
            "",
        ]

    return "\n".join(out)


def estimate_audio_duration(path: Path) -> float | None:
    return probe_duration(path)
