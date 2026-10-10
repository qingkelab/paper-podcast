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
from .. import voices as voice_catalog
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
from .. import branding
from .figures import extract_figures, render_first_page
from .illustration import generate_illustration
from .llm import LLMClient, LLMError, build_script_payload, split_long_segments
from .podcast_tts import PodcastTTSClient, PodcastTTSError, RoundTiming, probe_duration
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

# 阶段文案里的语言后缀。写「正在深度解读（英文）」比只改百分比有用：
# 双语模式下这一集要跑两遍，不写清楚会让人以为卡住了。
LANGUAGE_SUFFIX = {"zh": "", "en": "（英文）"}


class LanguageNotFound(ValueError):
    """请求了一个这一集并没有产出的语言版本（接口层映射成 404）。"""


def audio_filename(episode_id: str, language: str, primary: str) -> str:
    """音频文件名。主语言不带后缀，保证老链接和旧文件继续有效。"""
    return f"{episode_id}.mp3" if language == primary else f"{episode_id}.{language}.mp3"


def video_filename(
    episode_id: str, language: str, primary: str, orientation: str = "portrait"
) -> str:
    """视频文件名。横版加 `.landscape` 后缀，**竖版的名字一个字都不改** ——
    已经发布出去的链接、缓存、以及别处按文件名找文件的逻辑都靠它保持不变。
    """
    stem = f"{episode_id}.mp4" if language == primary else f"{episode_id}.{language}.mp4"
    if (orientation or "portrait").lower() in ("landscape", "horizontal", "16:9"):
        stem = stem.replace(".mp4", ".landscape.mp4")
    return stem


def language_plan(options: dict[str, Any], settings: Settings) -> tuple[str, list[str]]:
    """这一集要产出哪些语言，以及哪一个是主语言。

    主语言固定排在最前面 —— 它是顶层字段镜像的那一版，也决定标题与封面。
    """
    primary = str(options.get("language") or settings.default_language or "zh").lower()
    if primary not in ("zh", "en"):
        primary = "zh"

    raw = options.get("languages") or settings.language_list
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",")]
    ordered: list[str] = []
    for lang in [primary, *[str(x).lower() for x in raw if x]]:
        if lang in ("zh", "en") and lang not in ordered:
            ordered.append(lang)
    return primary, ordered or [primary]


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

    def _db_stage(
        self, episode_id: str, stage: str, progress: int, label: str, **extra: Any
    ) -> None:
        """指定进度与文案的阶段更新。

        双语模式下同一阶段要跑两遍，进度得按语言切片，
        而且要能在文案里标出「这是哪一版」。
        """
        status = STAGES[stage][0]
        self.db.update_episode(
            episode_id, status=status, progress=progress, stage_label=label, **extra
        )

    def _voices_for(
        self, language: str, options: dict[str, Any], primary: str
    ) -> tuple[str, str]:
        """这一版的两位主播音色。

        用户只在前端选过一次音色（主语言那一档），英文版必须换成英文音色 ——
        拿中文音色念英文虽然也能出声，但口音很明显。
        """
        default_a, default_b = voice_catalog.default_voices(language, self.settings)
        if language == primary:
            return (
                voice_catalog.normalize_voice(
                    options.get("voice_a") or None, default_a
                ),
                voice_catalog.normalize_voice(
                    options.get("voice_b") or None, default_b
                ),
            )
        return default_a, default_b

    @staticmethod
    def _mirror_fields(
        language: str,
        primary: str,
        version: dict[str, Any],
    ) -> dict[str, Any]:
        """非主语言版本不进顶层字段。

        顶层的 `analysis`/`script`/`audio_url`/`video` 是**主语言那一版**的镜像，
        老前端不改也能正常显示；另一语言只在 `versions[lang]` 里。
        """
        if language != primary:
            return {}
        fields: dict[str, Any] = {
            "paper_meta": version.get("paper_meta"),
            "analysis": version.get("analysis"),
            "script": version.get("script"),
            "illustration": version.get("illustration"),
        }
        if version.get("audio_path"):
            fields["audio_path"] = version["audio_path"]
            fields["audio_duration_sec"] = version.get("audio_duration_sec")
            fields["audio_bytes"] = version.get("audio_bytes")
            fields["timings"] = version.get("timings") or []
            # task_id / finished_round 是断点续传用的，主语言那一版记在顶层
            fields["podcast_task_id"] = version.get("podcast_task_id")
            fields["finished_round"] = version.get("finished_round")
        if version.get("video_path"):
            fields["video_path"] = version["video_path"]
            fields["video"] = version.get("video")
        return fields

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
        language: str = "zh",
        output_path: Path | None = None,
        illustration_png: str | None = None,
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
        if illustration_png is None:
            illustration_png = (record.get("illustration") or {}).get("png_path")

        try:
            result = await asyncio.to_thread(
                compose_video,
                segments=segments,
                timings=timings,
                audio_path=audio_path,
                audio_duration=audio_duration,
                cover_path=record.get("cover_path"),
                figures=record.get("figures") or [],
                illustration_png=illustration_png,
                work_dir=self.settings.video_work_dir / episode_id / language,
                output_path=output_path
                or (self.settings.video_dir / f"{episode_id}.mp4"),
                title=title,
                llm=self.llm,
                analysis=analysis,
                max_topic_images=self.settings.max_topic_images,
                language=language,
            )
        except VideoError as exc:
            logger.warning("视频合成失败（音频不受影响）：%s", exc)
            return {}
        except Exception as exc:  # noqa: BLE001
            logger.exception("视频合成出现未预期错误（音频不受影响）")
            return {}

        logger.info(
            "视频已生成（%s）：%d 帧 / %.1f 秒 / %.1f MB（配图分配：%s）",
            language,
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

    # ---------- 语言版本 ----------

    def _version_records(self, record: dict[str, Any]) -> dict[str, Any]:
        return dict(record.get("versions") or {})

    def available_languages(self, record: dict[str, Any]) -> list[str]:
        """这一集**实际有**哪些语言版本。

        不能拿服务端配置的语言列表来判：那会让「服务端支持中英双语」
        被误当成「每一集都有英文版」，于是老数据上 `?lang=en` 会**静默返回中文内容**
        （200 + 主语言），前端以为切成功了，其实什么都没变。
        """
        versions = self._version_records(record)
        if versions:
            return list(versions.keys())
        # 有产物但没有 versions = 双语之前生成的集，只有主语言那一版
        if record.get("script") or record.get("audio_path"):
            return [language_plan(record.get("options") or {}, self.settings)[0]]
        # 还在生成中的新任务：按创建时请求的语言回答，
        # 这样 `?lang=en` 得到的是「还没有音频」而不是「没有这个语言版本」
        return language_plan(record.get("options") or {}, self.settings)[1]

    def resolve_version(
        self, record: dict[str, Any], language: str | None
    ) -> tuple[str, dict[str, Any]]:
        """把 `?lang=` 解析成 (语言, 该版本记录)。

        缺省取主语言。**老数据没有 versions**（双语之前生成的集），
        这时把顶层字段当成主语言的那一版 —— 不必迁移就能继续用。
        """
        options = record.get("options") or {}
        primary, _ = language_plan(options, self.settings)
        versions = self._version_records(record)
        available = self.available_languages(record)

        if language:
            lang = language.strip().lower()
            if lang not in available:
                raise LanguageNotFound(f"这一集没有 {lang} 版本")
        else:
            lang = primary if primary in available else (available[0] if available else primary)

        if lang in versions:
            return lang, versions[lang]

        # 兼容路径：双语之前生成的集，顶层就是唯一那一版
        return lang, {
            "language": lang,
            "paper_meta": record.get("paper_meta"),
            "analysis": record.get("analysis"),
            "script": record.get("script"),
            "illustration": record.get("illustration"),
            "audio_path": record.get("audio_path"),
            "audio_duration_sec": record.get("audio_duration_sec"),
            "audio_bytes": record.get("audio_bytes"),
            "timings": record.get("timings") or [],
            "video_path": record.get("video_path"),
            "video": record.get("video"),
        }

    async def rebuild_video(
        self,
        episode_id: str,
        language: str | None = None,
        orientation: str = "portrait",
    ) -> dict[str, Any]:
        """用现有素材重新合成视频（配图被人工校正后用）。

        关键点：**复用上次的画面分配，不再问模型**。因为
        1) 再问一次结果可能不一样，用户会觉得「我就转了个图，怎么画面全变了」
        2) 主题图会被重新生成一遍（4 次模型调用 + 几十秒），纯属浪费

        音频、脚本、解读都不动 —— 只重新渲染幻灯片并编码。
        双语集要指定 `language`，否则只重合成主语言那一版。

        `orientation="landscape"` 会额外产出**横版**（1920×1080）那一份，
        存进 `video_landscape`（竖版 `video` 原样保留）—— 两种画幅可以同时在。
        """
        record = self.db.get_episode(episode_id)
        if not record:
            raise VideoError("播客不存在")

        options = record.get("options") or {}
        primary, _ = language_plan(options, self.settings)
        lang, version = self.resolve_version(record, language)

        stored = version.get("video") or {}
        timings_raw = version.get("timings") or []
        script = version.get("script") or {}
        segments = script.get("segments") or []

        if not timings_raw or not segments:
            raise VideoError("缺少脚本或时间轴，无法重新合成")
        audio_path = version.get("audio_path")
        if not audio_path or not Path(audio_path).exists():
            raise VideoError("音频已丢失，无法重新合成")
        if not ffmpeg_available():
            raise VideoError("系统未安装 ffmpeg，无法合成视频")

        timings = [
            RoundTiming(
                index=int(t.get("index", i)),
                speaker=str(t.get("speaker") or ""),
                start=float(t.get("start") or 0.0),
                end=float(t.get("end") or 0.0),
            )
            for i, t in enumerate(timings_raw)
        ]

        landscape = (orientation or "portrait").lower() in ("landscape", "horizontal", "16:9")
        output_path = self.settings.video_dir / video_filename(
            episode_id, lang, primary, orientation
        )
        result = await asyncio.to_thread(
            compose_video,
            segments=segments,
            timings=timings,
            audio_path=Path(audio_path),
            audio_duration=float(version.get("audio_duration_sec") or 0.0),
            cover_path=record.get("cover_path"),
            figures=record.get("figures") or [],
            illustration_png=(version.get("illustration") or {}).get("png_path")
            or (record.get("illustration") or {}).get("png_path"),
            work_dir=self.settings.video_work_dir / episode_id / lang,
            output_path=output_path,
            title=record.get("title") or "论文解读",
            # 复用画面时不给模型（那条规则见 TestPresetReuse）；强调行文案在
            # `video.scenes[i].point` 里，缺了才由维护脚本单独补一次（见 AGENTS.md）
            llm=None,
            analysis=version.get("analysis") or {},
            preset_scenes=stored.get("scenes") or None,
            preset_assets=stored.get("assets") or None,
            # 封面标题同样是「写一次、以后复用」的数据，重合成时不该换一句
            preset_hook=str(stored.get("hook") or ""),
            language=lang,
            orientation="landscape" if landscape else "portrait",
        )

        video = {
            **result.to_dict(),
            "url": f"/api/episodes/{episode_id}/video"
            + ("?orientation=landscape" if landscape else ""),
            "orientation": "landscape" if landscape else "portrait",
            # 存文件路径：横版不在 video_path 列里（那是竖版的），媒体路由靠它定位
            "path": str(result.video_path),
        }
        # 横版只覆盖 `video_landscape`，绝不碰 `video`（竖版是默认形态，不能被横版顶掉）
        video_field = "video_landscape" if landscape else "video"

        # 回写：双语集写进对应语言那一版；**只有主语言**才镜像到顶层字段。
        #
        # 顶层字段代表「主语言那一版」（契约 §1）。第一版这里无条件写 `fields["video"]`，
        # 于是重新合成英文版会把中文那一版从顶层挤掉 —— 实测把英文的强调行
        # 写进了中文集的顶层 `video`，列表/播放器读顶层字段时看到的就是英文那版。
        mirrored = lang == primary or not self._version_records(record)
        fields: dict[str, Any] = {}
        if mirrored:
            fields[video_field] = video
            if not landscape:
                fields["video_path"] = str(result.video_path)

        if self._version_records(record) or lang != primary:
            versions = self._version_records(record)
            entry = dict(versions.get(lang) or {})
            entry["language"] = lang
            if not landscape:
                entry["video_path"] = str(result.video_path)
            entry[video_field] = video
            versions[lang] = entry
            fields["versions"] = versions

        if fields:
            self.db.update_episode(episode_id, **fields)
        logger.info(
            "视频已重新合成（%s/%s）：%d 帧 / %.1f 秒（复用画面分配：%s）",
            lang,
            "横版" if landscape else "竖版",
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
        speech_rate = int(self.settings.podcast_speech_rate or 0)
        primary, languages = language_plan(options, self.settings)

        # 每个语言版本分到一段进度区间。双语时不能都从 35 开始 ——
        # 那会让进度条倒着走（中文跑完 75，英文又回 35），看起来像卡死重来。
        slab = (99 - 15) / max(len(languages), 1)

        def _slab(index: int) -> tuple[int, int, int, int]:
            start = 15 + index * slab
            return (
                int(start),
                int(start + slab * 0.20),
                int(start + slab * 0.40),
                int(start + slab),
            )

        try:
            # ---- 1. 解析（跨语言共用：同一份 PDF，封面与配图只提一次）----
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

            versions: dict[str, Any] = {}
            title = title_hint
            last_audio_path: Path | None = None

            # ---- 2~5. 逐语言：解读 → 脚本 → 音频 → 视频 ----
            for position, language in enumerate(languages):
                analyze_at, script_at, synth_at, slab_end = _slab(position)
                suffix = LANGUAGE_SUFFIX.get(language, f"（{language}）")
                first = position == 0

                # ---- 解读 ----
                self._db_stage(
                    episode_id,
                    "analyzing",
                    analyze_at,
                    f"正在深度解读{suffix}",
                    **({"title": title_hint or "未命名论文"} if first else {}),
                )
                paper_meta, analysis = self.llm.analyze_paper(
                    paper_text, title_hint=title_hint, language=language
                )

                # 用户显式指定的标题优先；否则采用模型从正文里认出的正式标题。
                # 标题只认**主语言**那一版：它是这一集的对外名字。
                if first:
                    title_locked = bool(options.get("title_locked"))
                    if paper_meta.get("title") and not title_locked:
                        previous = title_hint
                        title_hint = paper_meta["title"]
                        # ⚠️ 必须写回数据库。`guess_title` 只是从正文里猜第一行，
                        # 猜错是常事（arXiv PDF 的首页常把授权声明排在标题前面，
                        # 实测抓到过「Provided proper attribution is provided…」当标题）。
                        # 双语改造时这里漏了写回，等于让启发式猜测永久盖住模型认出的
                        # 正式标题 —— 而且只在「猜错」时才看得出来。
                        if title_hint != previous:
                            logger.info("标题改用模型认出的：%s → %s", previous, title_hint)
                    title = title_hint or "未命名论文"
                    self.db.update_episode(episode_id, title=title)
                else:
                    # 非主语言版本沿用主语言的标题，避免列表/分享链接出现两个名字
                    paper_meta = {**paper_meta, "title": title_hint or paper_meta.get("title")}
                if not paper_meta.get("arxiv_id"):
                    paper_meta["arxiv_id"] = guess_arxiv_id(episode.get("source_ref") or "")

                # ---- 信息图（跟随语言）----
                illustration = generate_illustration(
                    self.llm,
                    analysis,
                    paper_meta,
                    self.settings.illustration_dir,
                    episode_id if first else f"{episode_id}.{language}",
                    language=language,
                )

                version: dict[str, Any] = {
                    "language": language,
                    "paper_meta": paper_meta,
                    "analysis": analysis,
                    "illustration": illustration.to_dict(),
                }

                # text 来源没有 PDF 首页可当封面，就用**主语言**那张信息图兜底。
                # 封面是跨语言共用的，不能让英文版的信息图把中文版的封面顶掉。
                if first and not cover_fields and illustration.png_path:
                    cover_fields = {
                        "cover_path": illustration.png_path,
                        "cover_width": illustration.width,
                        "cover_height": illustration.height,
                    }
                    self.db.update_episode(episode_id, **cover_fields)

                # ---- 脚本 ----
                self._db_stage(episode_id, "scripting", script_at, f"正在生成播客脚本{suffix}")
                brand_chars = (
                    branding.brand_char_count(language)
                    if self.settings.enable_brand_intro_outro
                    else 0
                )
                padding_sec = prompts.compute_padding_sec(
                    head_music=self.settings.podcast_head_music,
                    tail_music=self.settings.podcast_tail_music,
                    brand_chars=brand_chars,
                    speech_rate=speech_rate,
                    language=language,
                )
                script = self.llm.generate_script(
                    analysis,
                    paper_meta,
                    duration_min=duration_min,
                    level=level,
                    speech_rate=speech_rate,
                    padding_sec=padding_sec,
                    language=language,
                )

                # 单轮超长的发言要切开 —— TTS 接口对单个 round 的字符数有硬上限，
                # 越界会直接报 40000010 并让整条流水线失败重跑（代价很大）。
                # 英文尤其容易撞线：一段 50 个词就有 300 字符上下。
                body = split_long_segments(script["segments"])
                if len(body) != len(script["segments"]):
                    logger.info(
                        "单轮超长发言已切分%s：%d 段 → %d 段",
                        suffix,
                        len(script["segments"]),
                        len(body),
                    )

                # 注入社区品牌片头/片尾。
                # 放在长度修复**之后**：它们是固定开销，不该被模型扩写或精简碰到；
                # 注入后再重算字数与时长，让 UI 上显示的是整期（含片头片尾）的预估。
                if self.settings.enable_brand_intro_outro:
                    merged = [
                        {k: v for k, v in seg.items() if k != "round"}
                        for seg in branding.intro_segments(language)
                        + body
                        + branding.outro_segments(language)
                    ]
                    script = build_script_payload(
                        merged,
                        speech_rate=speech_rate,
                        padding_sec=prompts.compute_padding_sec(
                            head_music=self.settings.podcast_head_music,
                            tail_music=self.settings.podcast_tail_music,
                            speech_rate=speech_rate,
                            language=language,
                        ),
                        language=language,
                    )
                    logger.info(
                        "已注入品牌片头片尾%s：正片 %d 段 → 整期 %d 段",
                        suffix,
                        len(body),
                        len(script["segments"]),
                    )
                elif len(body) != len(script["segments"]):
                    # 没开品牌话术，但切过分：也要重建一遍让 round / 字数 / 时长跟上
                    script = build_script_payload(
                        [{k: v for k, v in seg.items() if k != "round"} for seg in body],
                        speech_rate=speech_rate,
                        padding_sec=padding_sec,
                        language=language,
                    )

                version["script"] = script

                # ---- 音频 ----
                self._db_stage(episode_id, "synthesizing", synth_at, f"正在合成播客音频{suffix}")
                voice_a, voice_b = self._voices_for(language, options, primary)
                audio_path = self.settings.audio_dir / audio_filename(
                    episode_id, language, primary
                )

                def on_round(index: int, speaker: str, _s=script, _l=language, _e=slab_end) -> None:
                    total = max(len(_s["segments"]), 1)
                    progress = synth_at + int((_e - synth_at) * (index + 1) / total)
                    self.db.update_episode(
                        episode_id,
                        progress=min(progress, 99),
                        stage_label=(
                            f"正在合成播客音频{suffix}（第 {index + 1}/{total} 段）"
                        ),
                    )

                result = await self.tts.synthesize(
                    segments=script["segments"],
                    voice_a=voice_a,
                    voice_b=voice_b,
                    output_path=audio_path,
                    on_round=on_round,
                    speech_rate=speech_rate,
                )
                last_audio_path = result.audio_path

                version.update(
                    {
                        "audio_path": str(result.audio_path),
                        "audio_duration_sec": result.duration_sec,
                        "audio_bytes": result.bytes_written,
                        "podcast_task_id": result.task_id,
                        "finished_round": result.finished_round,
                        "timings": [t.to_dict() for t in result.timings],
                    }
                )
                versions[language] = version

                # 每一版单独落库：主语言那一版同时镜像到顶层字段，
                # 这样「音频已经好了」在双语模式下也是第一时间可见的。
                mirror = self._mirror_fields(language, primary, version)
                self.db.update_episode(episode_id, versions=versions, **mirror)

                # ---- 视频 ----
                video_fields = await self._compose_video_safely(
                    episode_id,
                    segments=script["segments"],
                    timings=result.timings,
                    audio_path=result.audio_path,
                    audio_duration=result.duration_sec or 0.0,
                    title=title,
                    analysis=analysis,
                    language=language,
                    output_path=self.settings.video_dir
                    / video_filename(episode_id, language, primary),
                    illustration_png=illustration.png_path,
                )
                if video_fields:
                    version.update(
                        {
                            "video_path": video_fields.get("video_path"),
                            "video": video_fields.get("video"),
                        }
                    )
                    versions[language] = version
                    mirror = self._mirror_fields(language, primary, version)
                    self.db.update_episode(episode_id, versions=versions, **mirror)

            # ---- 6. 完成 ----
            self.db.update_episode(
                episode_id,
                status="completed",
                progress=100,
                stage_label="已完成",
                error=None,
            )
            logger.info("任务 %s 完成：%s（语言：%s）", episode_id, last_audio_path, languages)
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


def render_script_text(episode: dict[str, Any], language: str = "zh") -> str:
    script = episode.get("script") or {}
    segments = script.get("segments") or []
    if not segments:
        return "（暂无脚本）" if language == "zh" else "(no script yet)"

    labels = ("Host A", "Host B") if language == "en" else ("主播A", "主播B")
    if language == "en":
        head = [
            f"《{episode.get('title') or 'Untitled paper'}》",
            "Two-host podcast script",
            "=" * 40,
            "",
        ]
    else:
        head = [f"《{episode.get('title') or '未命名论文'}》", "双人播客脚本", "=" * 40, ""]

    lines = list(head)
    for segment in segments:
        speaker = labels[0] if segment.get("speaker") == "A" else labels[1]
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


# 解读文档的标题/字段名。英文版导出的文档要是英文的 ——
# 一份全中文标题配英文正文的 Markdown 很别扭。
_DOC_LABELS = {
    "zh": {
        "authors": "作者",
        "published": "发表",
        "keywords": "关键词",
        "abstract": "摘要",
        "sections": (
            ("研究背景", "background"),
            ("核心创新点", "innovations"),
            ("研究方法", "method"),
            ("实验结果", "experiments"),
            ("核心结论", "conclusion"),
            ("存在不足", "limitations"),
            ("行业应用价值", "value"),
            ("未来研究方向", "future"),
        ),
        "untitled": "未命名论文",
        "footer": "*播客脚本 {count} 字，预计时长约 {minutes:.1f} 分钟*",
        "unit": "字",
    },
    "en": {
        "authors": "Authors",
        "published": "Published",
        "keywords": "Keywords",
        "abstract": "Abstract",
        "sections": (
            ("Background", "background"),
            ("Key contributions", "innovations"),
            ("Method", "method"),
            ("Experiments", "experiments"),
            ("Conclusion", "conclusion"),
            ("Limitations", "limitations"),
            ("Why it matters", "value"),
            ("Open questions", "future"),
        ),
        "untitled": "Untitled paper",
        "footer": "*Script: {count} words, estimated runtime ~{minutes:.1f} min*",
        "unit": "words",
    },
}


def render_analysis_markdown(episode: dict[str, Any], language: str = "zh") -> str:
    labels = _DOC_LABELS.get(language, _DOC_LABELS["zh"])
    meta = episode.get("paper_meta") or {}
    analysis = episode.get("analysis") or {}
    title = episode.get("title") or meta.get("title") or labels["untitled"]

    out: list[str] = [f"# {title}", ""]

    facts: list[str] = []
    if meta.get("authors"):
        facts.append(f"**{labels['authors']}**：{', '.join(meta['authors'])}")
    if meta.get("venue") or meta.get("year"):
        facts.append(
            f"**{labels['published']}**：{meta.get('venue') or ''} {meta.get('year') or ''}".strip()
        )
    if meta.get("arxiv_id"):
        facts.append(f"**arXiv**：[{meta['arxiv_id']}](https://arxiv.org/abs/{meta['arxiv_id']})")
    if meta.get("keywords"):
        facts.append(f"**{labels['keywords']}**：{', '.join(meta['keywords'])}")
    if facts:
        out.extend(facts)
        out.append("")

    if meta.get("abstract"):
        out.extend([f"## {labels['abstract']}", "", meta["abstract"], ""])

    for heading, key in labels["sections"]:
        out.extend(_markdown_section(heading, analysis.get(key)))

    script = episode.get("script") or {}
    if script.get("segments"):
        minutes = (script.get("est_duration_sec") or 0) / 60
        out += [
            "---",
            "",
            labels["footer"].format(
                count=script.get("word_count", 0), minutes=minutes
            ),
            "",
        ]

    return "\n".join(out)


def estimate_audio_duration(path: Path) -> float | None:
    return probe_duration(path)
