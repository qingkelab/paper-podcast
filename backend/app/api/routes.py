"""HTTP 接口层。字段与 docs/API.md 严格一致。"""

from __future__ import annotations

import hmac
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from .. import auth as auth_lib
from ..config import Settings
from ..db import Database
from ..models import (
    Album,
    AlbumAssignRequest,
    AlbumDetail,
    AlbumWriteRequest,
    BatchFailure,
    BatchRequest,
    BatchResult,
    Episode,
    FigureRotateRequest,
    EpisodeList,
    EpisodeListItem,
    EpisodeOptions,
    HealthResponse,
    LoginRequest,
    OptionItem,
    OptionsResponse,
    PasswordChangeRequest,
    RegisterRequest,
    ShareAuthor,
    ShareView,
    User,
)
from ..services.figures import PdfAssetsError, rotate_image_file
from ..services.video import VideoError, is_video_stale
from ..services.ingest import IngestError, fetch_url_text, guess_title
from ..services.pipeline import (
    LanguageNotFound,
    render_analysis_markdown,
    render_script_text,
)
from .. import voices as voice_catalog
from ..voices import DURATIONS, LEVELS, VOICES, normalize_voice
from ..worker import TaskQueue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

MAX_PDF_BYTES = 40 * 1024 * 1024  # 40MB，覆盖绝大多数论文
MAX_TEXT_CHARS = 200_000


# --------------------------------------------------------------------------
# 依赖获取
# --------------------------------------------------------------------------


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _db(request: Request) -> Database:
    return request.app.state.db


def _queue(request: Request) -> TaskQueue:
    return request.app.state.queue


# --------------------------------------------------------------------------
# 记录 -> 响应模型
# --------------------------------------------------------------------------


def _video_provenance_unknown(
    stored_video: dict[str, Any],
    record: dict[str, Any],
    version: dict[str, Any] | None = None,
) -> bool:
    """视频没有留下素材版本记录时，保守地认为它可能过时。

    这类是「加版本追踪之前」生成的旧视频。无法证明它跟当前配图一致，
    与其假装没问题，不如提示可以重新合成 —— 重合成一次之后就准了。
    没有配图的集不存在这个问题。
    """
    if stored_video.get("asset_versions"):
        return False
    version = version or {}
    has_assets = bool(
        record.get("cover_path")
        or record.get("figures")
        or (version.get("illustration") or record.get("illustration") or {}).get(
            "png_path"
        )
    )
    return has_assets


def _asset_version(path: Any) -> str:
    """给静态资源生成一个内容版本号，用于 URL 上的缓存击穿。

    ⚠️ 为什么必需：配图/封面这些资源的 URL 是固定的（/figures/f3 之类），
    但内容会因为「重新提取」「改进算法」而变化。如果只按 URL 做长缓存，
    客户端会一直用旧图 —— 实测踩过：把整体转了的配图修正后重新生成，
    浏览器里看到的还是没转过的旧图，白排查了一轮。
    用「修改时间 + 大小」拼一个短版本号，内容一变 URL 就变，缓存自然失效。
    """
    try:
        stat = Path(path).stat()
    except (OSError, TypeError):
        return "0"
    return f"{int(stat.st_mtime)}-{stat.st_size}"


def _version_payload(
    record: dict[str, Any],
    episode_id: str,
    version: dict[str, Any],
    *,
    include_large: bool = True,
) -> dict[str, Any]:
    """把一个语言版本渲染成响应里的 `versions[lang]`。

    音频/视频的 URL 不带 `?lang=`，而是**每个语言各自一条** ——
    带 query 的 URL 在 `<audio src>` 和缓存里都容易被搞混，
    分开的路径也更利于 CDN 和浏览器缓存。
    """
    audio_path = version.get("audio_path")
    audio_url = None
    if audio_path and Path(audio_path).exists():
        query = _lang_query(record, version)
        audio_url = (
            f"/api/episodes/{episode_id}/audio{query}"
            f"{'&' if query else '?'}v={_asset_version(audio_path)}"
        )

    video = None
    video_path = version.get("video_path")
    if include_large and video_path and Path(video_path).exists():
        stored_video = version.get("video") or {}
        query = _lang_query(record, version)
        video = {
            "url": (
                f"/api/episodes/{episode_id}/video{query}"
                f"{'&' if query else '?'}v={_asset_version(video_path)}"
            ),
            "duration_sec": stored_video.get("duration_sec"),
            "scene_count": stored_video.get("scene_count"),
            "bytes": stored_video.get("bytes"),
            "stale": is_video_stale(stored_video)
            or _video_provenance_unknown(stored_video, record, version),
        }

    illustration = None
    stored = version.get("illustration") if include_large else None
    if stored and stored.get("png_path") and Path(stored["png_path"]).exists():
        query = _lang_query(record, version)
        illustration = {
            "png_url": (
                f"/api/episodes/{episode_id}/illustration.png{query}"
                f"{'&' if query else '?'}v={_asset_version(stored['png_path'])}"
            ),
            "svg_url": (
                f"/api/episodes/{episode_id}/illustration.svg{query}"
                f"{'&' if query else '?'}v={_asset_version(stored.get('svg_path'))}"
            ),
            "width": int(stored.get("width") or 0),
            "height": int(stored.get("height") or 0),
            "source": stored.get("source") or "fallback",
        }

    return {
        "language": version.get("language") or _primary_language(record),
        "paper_meta": version.get("paper_meta"),
        "analysis": version.get("analysis") if include_large else None,
        "script": version.get("script") if include_large else None,
        "illustration": illustration,
        "audio_url": audio_url,
        "audio_duration_sec": version.get("audio_duration_sec"),
        "audio_bytes": version.get("audio_bytes"),
        "video": video,
    }


def _primary_language(record: dict[str, Any]) -> str:
    options = record.get("options") or {}
    lang = str(options.get("language") or "").lower()
    return lang if lang in ("zh", "en") else "zh"


def _lang_query(record: dict[str, Any], version: dict[str, Any]) -> str:
    """非主语言的资源 URL 要带上 `?lang=`。主语言不带，保持 URL 稳定。"""
    lang = version.get("language")
    if not lang or lang == _primary_language(record):
        return ""
    return f"?lang={lang}"


def _planned_languages(record: dict[str, Any]) -> list[str]:
    """创建这一集时**要求**产出哪些语言。

    要和 `Episode.languages`（实际已产出哪些）区分开：
    双语任务还在跑的时候，前者已经是 `["zh","en"]`，后者可能只有 `["zh"]`。
    它是请求记录的一部分，所以直接读 options 里存下的值。
    """
    options = record.get("options") or {}
    raw = options.get("languages")
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",")]
    ordered: list[str] = []
    for item in [options.get("language"), *[str(x).lower() for x in (raw or []) if x]]:
        if item in ("zh", "en") and item not in ordered:
            ordered.append(item)
    return ordered


def _all_versions(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """这一集的全部语言版本。

    双语之前生成的集没有 `versions`，这里把顶层字段当成主语言那一版 ——
    前端因此只需要处理一种形状，不必为老数据写特例。
    """
    stored = record.get("versions") or {}
    if stored:
        return stored
    if not (record.get("script") or record.get("audio_path")):
        return {}
    primary = _primary_language(record)
    return {
        primary: {
            "language": primary,
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
    }


def to_episode(record: dict[str, Any], *, include_large: bool = True) -> dict[str, Any]:
    episode_id = record["id"]

    cover_url = None
    cover_path = record.get("cover_path")
    if cover_path and Path(cover_path).exists():
        cover_url = f"/api/episodes/{episode_id}/cover?v={_asset_version(cover_path)}"

    # 列表接口不返回 figures/illustration：详情页才用得到，列表带了会让响应变大。
    # 但 cover_url 要保留 —— 列表卡片显示封面缩略图。
    figures = []
    for figure in (record.get("figures") or []) if include_large else []:
        path = figure.get("path")
        if not path or not Path(path).exists():
            continue
        figures.append(
            {
                "id": figure["id"],
                "kind": figure.get("kind") or "figure",
                "label": figure.get("label") or "",
                "caption": figure.get("caption") or "",
                "page": int(figure.get("page") or 1),
                "url": (
                    f"/api/episodes/{episode_id}/figures/{figure['id']}"
                    f"?v={_asset_version(path)}"
                ),
                "width": int(figure.get("width") or 0),
                "height": int(figure.get("height") or 0),
            }
        )

    primary = _primary_language(record)
    stored_versions = _all_versions(record)
    versions = {
        lang: _version_payload(record, episode_id, version, include_large=include_large)
        for lang, version in stored_versions.items()
    }
    languages = list(versions.keys())
    # 顶层字段镜像主语言那一版。老前端不改也能用；新前端切语言时读 versions。
    surface = versions.get(primary) or (
        next(iter(versions.values())) if versions else {}
    )

    options = record.get("options") or {}
    return {
        "id": record["id"],
        "title": record["title"],
        "source_type": record["source_type"],
        "source_ref": record.get("source_ref"),
        "status": record["status"],
        "stage_label": record.get("stage_label") or "",
        "progress": record.get("progress") or 0,
        "error": record.get("error"),
        "options": EpisodeOptions(
            duration_min=int(options.get("duration_min") or 5),
            level=options.get("level") or "intro",
            voice_a=options.get("voice_a") or "",
            voice_b=options.get("voice_b") or "",
            language=primary,
            # options.languages = 创建时要求的版本；languages = 已经产出的版本。
            # 双语任务跑到一半时两者会不一样，这是有意的。
            languages=_planned_languages(record) or languages,
        ).model_dump(),
        "language": primary,
        "languages": languages,
        "versions": versions,
        "paper_meta": surface.get("paper_meta") or record.get("paper_meta"),
        "analysis": surface.get("analysis") if include_large else None,
        "script": surface.get("script") if include_large else None,
        "cover_url": cover_url,
        "cover_width": record.get("cover_width"),
        "cover_height": record.get("cover_height"),
        "figures": figures,
        "illustration": surface.get("illustration"),
        "video": surface.get("video"),
        "audio_url": surface.get("audio_url"),
        "audio_duration_sec": surface.get("audio_duration_sec"),
        "audio_bytes": surface.get("audio_bytes"),
        # V2：分享与分组。user_id 不下发 —— 前端不需要，也没法验证别人的 id。
        "visibility": record.get("visibility") or "private",
        "share_token": record.get("share_token"),
        "album_id": record.get("album_id"),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _current_user_id(request: Request) -> str | None:
    """新建单集时记归属。开放模式下没有 user，落 None（之后被第一个注册者认领）。"""
    user = auth_lib.current_user(request)
    return user["id"] if user else None


def _require_episode(request: Request, episode_id: str) -> dict[str, Any]:
    """取单集，并**顺带校验归属**。别人的单集返回 404（不是 403）。

    用 404 不用 403 是有意的：403 等于告诉对方「这个 id 真实存在，只是不是你的」，
    拿它枚举一遍就能摸出别人的数据规模。404 什么都不泄漏。
    """
    record = _db(request).get_episode(episode_id)
    if not record:
        raise HTTPException(status_code=404, detail="播客不存在")
    user = auth_lib.user_or_401(request)
    if user is not None and record.get("user_id") not in (None, user["id"]):
        raise HTTPException(status_code=404, detail="播客不存在")
    return record


def _require_album(request: Request, album_id: str) -> dict[str, Any]:
    """取专辑（含归属校验）。

    开放模式下没有 user，就按「无主专辑」处理 —— 但开放模式只在库里
    一个用户都没有时成立，那时也不可能有专辑，所以这里实质上是要求登录。
    """
    user = auth_lib.user_or_401(request)
    if user is None:
        raise HTTPException(status_code=404, detail="专辑不存在")
    album = _db(request).get_album(album_id, user_id=user["id"])
    if not album:
        raise HTTPException(status_code=404, detail="专辑不存在")
    return album


def _resolve_version(
    request: Request, record: dict[str, Any], lang: str | None
) -> tuple[str, dict[str, Any]]:
    """把 `?lang=` 解析成 (语言, 版本记录)；这一集没有该语言时 404。"""
    try:
        return _queue(request).pipeline.resolve_version(record, lang)
    except LanguageNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _download_filename(record: dict[str, Any], language: str, suffix: str) -> str:
    """下载文件名。非主语言的产物加语言后缀，避免两份存成一个名字。"""
    base = _safe_filename(record["title"])
    tag = "" if language == _primary_language(record) else f"-{language}"
    return f"{base}{tag}{suffix}"


# --------------------------------------------------------------------------
# 基础信息
# --------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """**免登录**：前端要靠它判断该不该跳登录页。

    `mode="open"` 表示库里还没有任何用户（不需要登录），
    这时前端不显示登录入口 —— 否则会出现「要求登录但没有账号可登」的死循环。
    """
    settings = _settings(request)
    return HealthResponse(
        status="ok",
        version=settings.version,
        modes={"llm": settings.llm_mode, "tts": settings.tts_mode},
        mode="open" if _db(request).is_open_mode() else "auth",
    )


@router.get("/options", response_model=OptionsResponse)
async def options() -> OptionsResponse:
    """**免登录**：只是一份音色/时长目录，没有任何私人数据。

    首页（产品介绍页）在登录前也要能显示状态，401 会让它误报「后端未连接」。
    """
    return OptionsResponse(
        durations=DURATIONS,
        levels=LEVELS,
        voices=VOICES,
        languages=[
            OptionItem(value=code, label=label)
            for code, label in voice_catalog.LANGUAGE_LABELS.items()
        ],
    )


# --------------------------------------------------------------------------
# 创建任务
# --------------------------------------------------------------------------


@router.post("/episodes", status_code=201, response_model=Episode)
async def create_episode(request: Request):
    """支持两种 Content-Type：
    - multipart/form-data：PDF 上传（字段 file + duration_min/level/voice_a/voice_b）
    - application/json：链接或纯文本导入
    """
    auth_lib.user_or_401(request)
    settings = _settings(request)
    content_type = (request.headers.get("content-type") or "").lower()

    if content_type.startswith("multipart/form-data"):
        return await _create_from_upload(request, settings)
    return await _create_from_json(request, settings)


async def _create_from_upload(request: Request, settings: Settings) -> dict[str, Any]:
    form = await request.form()
    upload = form.get("file")
    # 注意：这里必须用 Starlette 的 UploadFile 做判据。手动调用 request.form()
    # 返回的是 starlette 的实例，而 fastapi.UploadFile 是它的**子类**，
    # 用 fastapi.UploadFile 做 isinstance 会恒为 False，导致所有上传都被拒。
    if upload is None:
        raise HTTPException(status_code=400, detail="缺少 file 字段（PDF 文件）")
    if not isinstance(upload, StarletteUploadFile):
        raise HTTPException(status_code=400, detail="file 字段必须是一个文件")

    filename = upload.filename or "paper.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="只支持 PDF 文件，请上传 .pdf")

    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传的文件是空的")
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"文件过大（{len(data) // 1024 // 1024}MB），上限 {MAX_PDF_BYTES // 1024 // 1024}MB",
        )
    if data[:5] != b"%PDF-":
        raise HTTPException(status_code=400, detail="这个文件不是有效的 PDF（缺少 PDF 文件头）")

    episode_options = _parse_options(form, settings)

    record = _db(request).create_episode(
        title=Path(filename).stem or "未命名论文",
        source_type="pdf",
        source_ref=None,  # 落盘后再回填
        options=episode_options,
        user_id=_current_user_id(request),
    )

    # 存盘：用 episode id 命名，避免同名文件互相覆盖
    target = settings.upload_dir / f"{record['id']}.pdf"
    target.write_bytes(data)
    _db(request).update_episode(record["id"], source_ref=str(target))

    await _queue(request).submit(record["id"])
    refreshed = _db(request).get_episode(record["id"])
    assert refreshed is not None
    return to_episode(refreshed)


async def _create_from_json(request: Request, settings: Settings) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    source_type = payload.get("source_type")
    if source_type not in ("url", "text"):
        raise HTTPException(status_code=400, detail="source_type 只能是 url 或 text")

    episode_options = _parse_options(payload.get("options") or {}, settings)
    custom_title = (payload.get("title") or "").strip()

    if source_type == "url":
        url = (payload.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="source_type=url 时必须提供 url")
        if not re.match(r"^https?://", url, re.I):
            raise HTTPException(status_code=400, detail="链接必须以 http:// 或 https:// 开头")

        # 建任务前先探一次链接，能把「抓不到」的失败提前反馈给用户，
        # 而不是让他等两分钟才看到 failed。
        try:
            text, _, _ = fetch_url_text(url)
        except IngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        record = _db(request).create_episode(
            title=custom_title or guess_title(text, fallback="链接论文"),
            source_type="url",
            source_ref=url,
            options=episode_options,
            user_id=_current_user_id(request),
        )

    else:  # text
        text = (payload.get("text") or "").strip()
        if len(text) < 200:
            raise HTTPException(
                status_code=400, detail="粘贴的文本太短（少于 200 字），不足以生成解读"
            )
        if len(text) > MAX_TEXT_CHARS:
            raise HTTPException(
                status_code=400, detail=f"文本过长（超过 {MAX_TEXT_CHARS} 字），请改为上传 PDF"
            )
        record = _db(request).create_episode(
            title=custom_title or guess_title(text, fallback="粘贴的论文"),
            source_type="text",
            source_ref=None,
            options=episode_options,
            user_id=_current_user_id(request),
        )
        _db(request).update_episode(record["id"], raw_text=text)

    # 用户自己填了标题就别让模型覆盖掉。PDF 上传时标题来自文件名、链接导入时
    # 来自页面标题，这两者都只是「猜的」，模型解读出的正式标题更准。
    if custom_title:
        _db(request).update_episode(
            record["id"], options={**episode_options, "title_locked": True}
        )

    await _queue(request).submit(record["id"])
    refreshed = _db(request).get_episode(record["id"])
    assert refreshed is not None
    return to_episode(refreshed)


def _parse_options(source: Any, settings: Settings) -> dict[str, Any]:
    """从表单或 JSON 里解析生成参数，越界值一律回退默认。

    注意：必须用注入的 settings，不能调 get_settings()——后者是进程级缓存，
    在测试和「多实例不同配置」场景下会拿到错误的默认音色。
    """

    def raw(key: str, default: Any = None) -> Any:
        if hasattr(source, "get"):
            value = source.get(key, default)
        else:
            value = default
        return value if value not in (None, "") else default

    try:
        duration = int(raw("duration_min", 5))
    except (TypeError, ValueError):
        duration = 5
    if duration not in (3, 5, 10):
        duration = 5

    level = str(raw("level", "intro"))
    if level not in ("intro", "advanced", "expert"):
        level = "intro"

    # 语言：主语言 + 实际产出哪些版本。都只接受目录里的值，
    # 避免前端传个 "jp" 把整条流水线带进未知分支。
    language = str(raw("language", settings.default_language)).lower()
    if language not in ("zh", "en"):
        language = settings.default_language if settings.default_language in ("zh", "en") else "zh"

    requested = raw("languages", settings.language_list)
    if isinstance(requested, str):
        requested = [x.strip() for x in requested.split(",")]
    languages: list[str] = []
    for item in [language, *[str(x).lower() for x in (requested or []) if x]]:
        if item in ("zh", "en") and item not in languages:
            languages.append(item)

    return {
        "duration_min": duration,
        "level": level,
        "voice_a": normalize_voice(
            _as_str(raw("voice_a")), settings.default_voice_a
        ),
        "voice_b": normalize_voice(
            _as_str(raw("voice_b")), settings.default_voice_b
        ),
        "language": language,
        "languages": languages or [language],
    }


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


# --------------------------------------------------------------------------
# V2：账号与会话
# --------------------------------------------------------------------------


def _user_payload(user: dict[str, Any]) -> dict[str, Any]:
    """对外只给这几个字段。**password_hash 绝不出现** —— 这里白名单式构造，
    不是 `{**user}` 再删，免得以后加了字段（比如重置口令用的 token）被顺手带出去。"""
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or "",
        "created_at": user["created_at"],
    }


@router.post("/auth/register", status_code=201, response_model=User)
async def register(request: Request, payload: RegisterRequest, response: Response):
    """注册即登录（直接下发会话 cookie）。

    **第一个注册成功的用户会认领所有无主单集** —— V1 时代的数据不会变成孤儿。
    """
    settings = _settings(request)
    db = _db(request)

    if settings.signup_code and not hmac.compare_digest(
        payload.signup_code or "", settings.signup_code
    ):
        raise HTTPException(status_code=403, detail="邀请码不正确")

    try:
        username = auth_lib.normalize_username(payload.username)
        password = auth_lib.normalize_password(payload.password)
    except auth_lib.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    if db.get_user_by_username(username):
        raise HTTPException(status_code=409, detail="这个用户名已经被用了")

    first_user = db.is_open_mode()
    user = db.create_user(
        user_id=auth_lib.new_user_id(),
        username=username,
        password_hash=auth_lib.hash_password(password),
        display_name=auth_lib.normalize_display_name(payload.display_name),
    )

    if first_user:
        claimed = db.claim_orphan_episodes(user["id"])
        logger.info("首个账号 %s 认领了 %d 集无主数据", username, claimed)

    token = auth_lib.new_session_token()
    db.create_session(
        token=token,
        user_id=user["id"],
        expires_at=auth_lib.expiry_from_now(settings.session_ttl_days),
    )
    auth_lib.set_session_cookie(response, token, settings)
    return _user_payload(user)


@router.post("/auth/login", response_model=User)
async def login(request: Request, payload: LoginRequest, response: Response):
    """登录。用户名不存在与口令错误返回**同一个**提示，别告诉对方哪个错了。

    失败次数超限会先被限流挡住（429）—— 否则这个接口就是个可以无限尝试的爆破入口。
    """
    settings = _settings(request)
    db = _db(request)
    throttle: auth_lib.LoginThrottle = request.app.state.login_throttle
    attempt_key = throttle.key(
        (payload.username or "").strip().lower(), auth_lib.client_key(request)
    )

    wait = throttle.retry_after(attempt_key)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请 {max(wait // 60, 1)} 分钟后再试",
            headers={"Retry-After": str(wait)},
        )

    try:
        username = auth_lib.normalize_username(payload.username)
    except auth_lib.AuthError as exc:
        raise HTTPException(status_code=401, detail="用户名或口令不正确") from exc

    user = db.get_user_by_username(username)
    # 用户不存在时也跑一次哈希校验：否则「不存在」比「口令错」快得多，
    # 从响应时间上就能枚举出哪些用户名是真实存在的。
    stored = user["password_hash"] if user else auth_lib.hash_password("dummy-password")
    ok = auth_lib.verify_password(payload.password or "", stored)
    if not user or not ok:
        throttle.record_failure(attempt_key)
        raise HTTPException(status_code=401, detail="用户名或口令不正确")

    # 登录成功清掉失败记录：否则之前攒的失败会把之后的正常登录也一起锁住
    throttle.reset(attempt_key)
    # 顺手清过期会话（省一个定时任务）
    db.purge_expired_sessions()

    token = auth_lib.new_session_token()
    db.create_session(
        token=token,
        user_id=user["id"],
        expires_at=auth_lib.expiry_from_now(settings.session_ttl_days),
    )
    auth_lib.set_session_cookie(response, token, settings)
    return _user_payload(user)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request) -> Response:
    """幂等：没登录也返回 204，前端不必先判断状态。"""
    token = auth_lib.session_cookie(request)
    if token:
        _db(request).delete_session(token)
    response = Response(status_code=204)
    auth_lib.clear_session_cookie(response)
    return response


@router.post("/auth/password", status_code=204)
async def change_password(request: Request, payload: PasswordChangeRequest) -> Response:
    """改自己的口令。

    **必须验旧口令**：否则会话 cookie 被偷走就等于永久接管账号。
    改完把**其他**会话全部删掉（保留当前这一个）——
    口令换了之后，别处还挂着旧会话是最常见的「明明改了密码还是被盗」。
    """
    user = auth_lib.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录")

    if not auth_lib.verify_password(payload.current_password or "", user["password_hash"]):
        raise HTTPException(status_code=401, detail="当前口令不正确")

    try:
        new_password = auth_lib.normalize_password(payload.new_password)
    except auth_lib.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    db = _db(request)
    db.update_user_password(user["id"], auth_lib.hash_password(new_password))

    # 只删「其他」会话，保留当前这条 —— 不能先全删再补回，那样一旦漏补
    # 用户改完口令自己就被踢出去了
    db.delete_other_sessions(user["id"], auth_lib.session_cookie(request))
    return Response(status_code=204)


@router.get("/auth/me", response_model=User)
async def me(request: Request):
    """未登录 401。前端启动时用它判断登录态。"""
    user = auth_lib.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录")
    return _user_payload(user)


# --------------------------------------------------------------------------
# V2：个人专辑
# --------------------------------------------------------------------------


def _cover_url_for_episode(record: dict[str, Any]) -> str | None:
    path = record.get("cover_path")
    if path and Path(path).exists():
        return f"/api/episodes/{record['id']}/cover?v={_asset_version(path)}"
    return None


def _album_payload(request: Request, album: dict[str, Any]) -> dict[str, Any]:
    """专辑摘要。封面取专辑里最新一集的封面。"""
    db = _db(request)
    episodes, _ = db.list_episodes(album_id=album["id"], limit=1, offset=0)
    cover_url = _cover_url_for_episode(episodes[0]) if episodes else None
    return {
        "id": album["id"],
        "title": album["title"],
        "description": album.get("description"),
        "episode_count": db.count_episodes_in_album(album["id"]),
        "cover_url": cover_url,
        "created_at": album["created_at"],
        "updated_at": album["updated_at"],
    }


@router.get("/albums", response_model=list[Album])
async def list_albums(request: Request):
    user = auth_lib.user_or_401(request)
    if user is None:
        return []
    return [_album_payload(request, album) for album in _db(request).list_albums(user["id"])]


@router.post("/albums", status_code=201, response_model=Album)
async def create_album(request: Request, payload: AlbumWriteRequest):
    user = auth_lib.user_or_401(request)
    if user is None:
        raise HTTPException(status_code=401, detail="需要登录")

    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="专辑名称不能为空")
    if len(title) > 60:
        raise HTTPException(status_code=400, detail="专辑名称最长 60 字")

    description = (payload.description or "").strip() or None
    album = _db(request).create_album(
        album_id=auth_lib.new_album_id(),
        user_id=user["id"],
        title=title,
        description=description[:200] if description else None,
    )
    return _album_payload(request, album)


@router.get("/albums/{album_id}", response_model=AlbumDetail)
async def get_album(request: Request, album_id: str):
    album = _require_album(request, album_id)
    payload = _album_payload(request, album)
    records, _ = _db(request).list_episodes(album_id=album_id, limit=100, offset=0)
    payload["episodes"] = [to_episode(r, include_large=False) for r in records]
    return payload


@router.patch("/albums/{album_id}", response_model=Album)
async def update_album(request: Request, album_id: str, payload: AlbumWriteRequest):
    album = _require_album(request, album_id)
    user = auth_lib.current_user(request)
    assert user is not None  # _require_album 已经保证了这一点

    fields: dict[str, Any] = {}
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="专辑名称不能为空")
        fields["title"] = title[:60]
    if payload.description is not None:
        fields["description"] = payload.description.strip()[:200] or None

    _db(request).update_album(album_id, user_id=user["id"], **fields)
    refreshed = _db(request).get_album(album_id, user_id=user["id"])
    assert refreshed is not None
    return _album_payload(request, refreshed)


@router.delete("/albums/{album_id}", status_code=204)
async def delete_album(request: Request, album_id: str) -> Response:
    """只删专辑，**不删里面的单集**（单集的 album_id 置空）。"""
    album = _require_album(request, album_id)
    user = auth_lib.current_user(request)
    assert user is not None
    _db(request).delete_album(album["id"], user_id=user["id"])
    return Response(status_code=204)


@router.post("/albums/{album_id}/episodes", response_model=AlbumDetail)
async def assign_album_episodes(
    request: Request, album_id: str, payload: AlbumAssignRequest
):
    """把单集加入专辑。幂等：已经在里面的再传一次没有副作用。"""
    album = _require_album(request, album_id)
    user = auth_lib.current_user(request)
    assert user is not None

    ids = [item for item in (payload.episode_ids or []) if item]
    if not ids:
        raise HTTPException(status_code=400, detail="episode_ids 不能为空")
    if len(ids) > 100:
        raise HTTPException(status_code=400, detail="一次最多加 100 集")

    db = _db(request)
    # 只加自己的单集：别人的 id 直接忽略（不报错，也不泄漏它们存在）
    db.assign_episodes_to_album(album["id"], ids, user_id=user["id"])
    db.touch_album(album["id"])
    return await get_album(request, album_id)


@router.delete("/albums/{album_id}/episodes/{episode_id}", response_model=AlbumDetail)
async def remove_album_episode(request: Request, album_id: str, episode_id: str):
    album = _require_album(request, album_id)
    user = auth_lib.current_user(request)
    assert user is not None
    _db(request).remove_episode_from_album(album["id"], episode_id, user_id=user["id"])
    _db(request).touch_album(album["id"])
    return await get_album(request, album_id)


# --------------------------------------------------------------------------
# V2：一键分享
# --------------------------------------------------------------------------


@router.post("/episodes/{episode_id}/share", response_model=Episode)
async def enable_share(request: Request, episode_id: str):
    """设为公开并返回分享 token。

    **已有 token 时保持不变**：重复调用不能让已经发出去的链接失效 ——
    用户点两次「分享」，第二次就把刚才发给别人的链接弄失效，是最恼人的那种 bug。
    """
    record = _require_episode(request, episode_id)
    token = record.get("share_token") or auth_lib.new_share_token()
    _db(request).update_episode(episode_id, visibility="public", share_token=token)
    refreshed = _require_episode(request, episode_id)
    return to_episode(refreshed)


@router.delete("/episodes/{episode_id}/share", response_model=Episode)
async def disable_share(request: Request, episode_id: str):
    """取消公开：链接立即失效（token 一并清掉）。"""
    _require_episode(request, episode_id)
    _db(request).update_episode(episode_id, visibility="private", share_token=None)
    refreshed = _require_episode(request, episode_id)
    return to_episode(refreshed)


@router.post("/episodes/{episode_id}/share/reset", response_model=Episode)
async def reset_share(request: Request, episode_id: str):
    """换一个新 token（旧链接立即失效）。用于「链接被传出去了想收回」。"""
    _require_episode(request, episode_id)
    _db(request).update_episode(
        episode_id, visibility="public", share_token=auth_lib.new_share_token()
    )
    refreshed = _require_episode(request, episode_id)
    return to_episode(refreshed)


# --------------------------------------------------------------------------
# V2：公开分享（**全部免登录**）
# --------------------------------------------------------------------------


def _share_episode(request: Request, token: str) -> dict[str, Any]:
    record = _db(request).episode_by_share_token(token)
    if not record:
        raise HTTPException(status_code=404, detail="分享链接已失效")
    return record


def _share_asset_urls(record: dict[str, Any], token: str | None) -> dict[str, Any]:
    """封面地址。公开链接走 `/api/share/{token}/cover`（原来的资源接口现在要登录），
    作者自己看的时候走受保护的 `/api/episodes/{id}/cover`。"""
    out: dict[str, Any] = {}
    cover_path = record.get("cover_path")
    if cover_path and Path(cover_path).exists():
        out["cover_url"] = (
            f"{_asset_base(record, token)}/cover?v={_asset_version(cover_path)}"
        )
    return out


def _asset_base(record: dict[str, Any], token: str | None) -> str:
    """资源 URL 的前缀。

    - `token` 给了 → `/api/share/{token}`（免登录的公开通道）
    - 否则 → `/api/episodes/{id}`（受保护通道，请求者就是作者本人）

    两条路走同一套拼装代码，避免「公开页能看、作者自己看却是另一份实现」这种分叉 ——
    那种分叉迟早会有一边忘了改。
    """
    if token:
        return f"/api/share/{token}"
    return f"/api/episodes/{record['id']}"


def _lang_query_for(language: str | None) -> str:
    return f"?lang={language}" if language else ""


def _share_figures(
    record: dict[str, Any], token: str | None
) -> list[dict[str, Any]]:
    base = _asset_base(record, token)
    figures = []
    for figure in record.get("figures") or []:
        path = figure.get("path")
        if not path or not Path(path).exists():
            continue
        figures.append(
            {
                "id": figure["id"],
                "kind": figure.get("kind") or "figure",
                "label": figure.get("label") or "",
                "caption": figure.get("caption") or "",
                "page": int(figure.get("page") or 1),
                "url": f"{base}/figures/{figure['id']}?v={_asset_version(path)}",
                "width": int(figure.get("width") or 0),
                "height": int(figure.get("height") or 0),
            }
        )
    return figures


def _share_version_payload(
    record: dict[str, Any], token: str | None, version: dict[str, Any]
) -> dict[str, Any]:
    base = _asset_base(record, token)
    language = version.get("language") or _primary_language(record)
    query = _lang_query_for(language)

    audio_path = version.get("audio_path")
    audio_url = None
    if audio_path and Path(audio_path).exists():
        audio_url = f"{base}/audio{query}&v={_asset_version(audio_path)}"

    video = None
    video_path = version.get("video_path")
    if video_path and Path(video_path).exists():
        stored = version.get("video") or {}
        video = {
            "url": f"{base}/video{query}&v={_asset_version(video_path)}",
            "duration_sec": stored.get("duration_sec"),
            "scene_count": stored.get("scene_count"),
            "bytes": stored.get("bytes"),
            "stale": False,  # 公开页不显示「可以重新合成」这类只有作者在意的状态
        }

    illustration = None
    stored_illus = version.get("illustration") or {}
    if stored_illus.get("png_path") and Path(stored_illus["png_path"]).exists():
        illustration = {
            "png_url": (
                f"{base}/illustration.png{query}&v={_asset_version(stored_illus['png_path'])}"
            ),
            "svg_url": (
                f"{base}/illustration.svg{query}"
                f"&v={_asset_version(stored_illus.get('svg_path'))}"
            ),
            "width": int(stored_illus.get("width") or 0),
            "height": int(stored_illus.get("height") or 0),
            "source": stored_illus.get("source") or "fallback",
        }

    return {
        "language": language,
        "paper_meta": version.get("paper_meta"),
        "analysis": version.get("analysis"),
        "script": version.get("script"),
        "illustration": illustration,
        "audio_url": audio_url,
        "audio_duration_sec": version.get("audio_duration_sec"),
        "audio_bytes": version.get("audio_bytes"),
        "video": video,
    }


def _build_share_view(
    request: Request,
    record: dict[str, Any],
    token: str | None,
    *,
    include_author: bool = True,
) -> dict[str, Any]:
    """把一期渲染成公开视图。share 链接与首页展示共用它 ——

    共用是刻意的：两处各写一份，迟早会有一边忘了加新字段（比如双语）。
    """
    versions = _all_versions(record)
    payload: dict[str, Any] = {
        "token": token,
        "title": record["title"],
        "language": _primary_language(record),
        "languages": list(versions.keys()),
        "versions": {
            lang: _share_version_payload(record, token, version)
            for lang, version in versions.items()
        },
        "figures": _share_figures(record, token),
        "created_at": record["created_at"],
        **_share_asset_urls(record, token),
    }

    primary = _primary_language(record)
    surface = payload["versions"].get(primary) or (
        next(iter(payload["versions"].values())) if payload["versions"] else {}
    )
    payload.update(
        {
            "paper_meta": surface.get("paper_meta") or record.get("paper_meta"),
            "analysis": surface.get("analysis"),
            "script": surface.get("script"),
            "illustration": surface.get("illustration"),
            "audio_url": surface.get("audio_url"),
            "audio_duration_sec": surface.get("audio_duration_sec"),
            "audio_bytes": surface.get("audio_bytes"),
            "video": surface.get("video"),
        }
    )

    if include_author:
        owner = _db(request).get_user(record["user_id"]) if record.get("user_id") else None
        if owner:
            payload["author"] = ShareAuthor(
                display_name=owner.get("display_name") or owner["username"]
            )
    return payload


@router.get("/share/{token}", response_model=ShareView)
async def get_share(request: Request, token: str):
    """公开视图。**免登录**。

    刻意不是完整 Episode：不给 id / options / source_ref / raw_text / error，
    作者是谁也只给展示名。别人拿到链接能看能听，但看不到内部结构。
    """
    return _build_share_view(request, _share_episode(request, token), token)


@router.get("/share/{token}/video")
async def get_share_video(request: Request, token: str, lang: str | None = Query(None)):
    record = _share_episode(request, token)
    _, version = _resolve_version(request, record, lang)
    path = version.get("video_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="这一集还没有视频")
    return _ranged_response(request, Path(path), "video/mp4")


@router.get("/share/{token}/audio")
async def get_share_audio(request: Request, token: str, lang: str | None = Query(None)):
    record = _share_episode(request, token)
    _, version = _resolve_version(request, record, lang)
    path = version.get("audio_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="这一集还没有音频")
    return _ranged_response(
        request, Path(path), mimetypes.guess_type(path)[0] or "audio/mpeg"
    )


@router.get("/share/{token}/cover")
async def get_share_cover(request: Request, token: str):
    record = _share_episode(request, token)
    cover_path = record.get("cover_path")
    if not cover_path:
        raise HTTPException(status_code=404, detail="这一集还没有封面")
    return _png_response(Path(cover_path))


@router.get("/share/{token}/figures/{figure_id}")
async def get_share_figure(request: Request, token: str, figure_id: str):
    record = _share_episode(request, token)
    for figure in record.get("figures") or []:
        if figure.get("id") == figure_id:
            return _png_response(Path(figure.get("path") or ""))
    raise HTTPException(status_code=404, detail="配图不存在")


@router.get("/share/{token}/illustration.png")
async def get_share_illustration_png(
    request: Request, token: str, lang: str | None = Query(None)
):
    record = _share_episode(request, token)
    _, version = _resolve_version(request, record, lang)
    stored = version.get("illustration") or {}
    if not stored.get("png_path"):
        raise HTTPException(status_code=404, detail="这一集还没有生成配图")
    return _png_response(Path(stored["png_path"]))


@router.get("/share/{token}/illustration.svg")
async def get_share_illustration_svg(
    request: Request, token: str, lang: str | None = Query(None)
):
    record = _share_episode(request, token)
    _, version = _resolve_version(request, record, lang)
    stored = version.get("illustration") or {}
    svg_path = stored.get("svg_path")
    if not svg_path or not Path(svg_path).exists():
        raise HTTPException(status_code=404, detail="这一集还没有生成配图")
    return FileResponse(
        Path(svg_path),
        media_type="image/svg+xml",
        headers={
            "Cache-Control": _IMAGE_CACHE,
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/share/{token}/script.txt")
async def get_share_script(request: Request, token: str, lang: str | None = Query(None)):
    record = _share_episode(request, token)
    language, version = _resolve_version(request, record, lang)
    content = render_script_text({**record, **version}, language)
    filename = _download_filename(record, language, "-脚本.txt")
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(filename)}"
        },
    )


@router.get("/share/{token}/analysis.md")
async def get_share_analysis(request: Request, token: str, lang: str | None = Query(None)):
    record = _share_episode(request, token)
    language, version = _resolve_version(request, record, lang)
    content = render_analysis_markdown({**record, **version}, language)
    filename = _download_filename(record, language, "-解读.md")
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(filename)}"
        },
    )


@router.get("/showcase", response_model=ShareView)
async def get_showcase(request: Request):
    """首页展示用的一期。**免登录** —— 产品介绍页要在登录前就能看到真东西。

    - **登录了**：取自己最新一期已完成的（优先多语言），资源走受保护地址
    - **没登录**：取最新一期**公开分享**的（作者点过「分享」的那些），资源走 share 地址
    - 都没有 → `404`，前端把展示区整块隐藏

    为什么不做成「谁都能看所有别人的」：那是把私有数据默认公开了。
    想看真实产物就先点一下分享 —— 这正好也让分享功能有了用处。
    """
    db = _db(request)
    user = auth_lib.current_user(request)

    picked: dict[str, Any] | None = None
    if user:
        candidates, _ = db.list_episodes(
            limit=20, offset=0, status="completed", user_id=user["id"]
        )
        # 优先多语言：一屏就能把「视频 + 双语 + 切换器」三件事讲清楚
        def language_count(item: dict[str, Any]) -> int:
            return len((item.get("versions") or {}))

        picked = max(candidates, key=language_count, default=None)
    else:
        rows = db.query_public_episodes(limit=20)
        picked = rows[0] if rows else None

    if not picked:
        raise HTTPException(status_code=404, detail="还没有可以展示的成品")

    # 作者自己看：资源走 /api/episodes/{id}/…（有 cookie）
    # 别人看：走 /api/share/{token}/…（公开通道）
    is_owner = bool(user and picked.get("user_id") == user["id"])
    token = None
    if not is_owner:
        token = picked.get("share_token")
        if not token:
            raise HTTPException(status_code=404, detail="还没有可以展示的成品")

    return _build_share_view(request, picked, token, include_author=not is_owner)


# --------------------------------------------------------------------------
# V2：批量生成
# --------------------------------------------------------------------------


def _batch_limit(request: Request) -> int:
    return max(int(_settings(request).max_batch_size or 20), 1)


@router.post("/episodes/batch", status_code=201, response_model=BatchResult)
async def create_batch(request: Request):
    """一次提交多篇论文。

    **单项失败不影响其他项**：抓不到的链接进 `failed` 并给出原因，
    能用的照常入队。一个链接写错就让整批白等，比慢一点糟糕得多。
    """
    auth_lib.user_or_401(request)
    settings = _settings(request)
    content_type = (request.headers.get("content-type") or "").lower()
    limit = _batch_limit(request)

    if content_type.startswith("multipart/form-data"):
        return await _batch_from_upload(request, settings, limit)
    return await _batch_from_json(request, settings, limit)


async def _batch_from_upload(request: Request, settings: Settings, limit: int) -> dict[str, Any]:
    form = await request.form()
    uploads = [
        item
        for item in form.getlist("files")
        if isinstance(item, StarletteUploadFile)
    ]
    if not uploads:
        raise HTTPException(status_code=400, detail="缺少 files 字段（PDF 文件，可多选）")
    if len(uploads) > limit:
        raise HTTPException(status_code=400, detail=f"一次最多 {limit} 篇")

    episode_options = _parse_options(form, settings)
    db = _db(request)
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for upload in uploads:
        name = upload.filename or "paper.pdf"
        try:
            if not name.lower().endswith(".pdf"):
                raise ValueError("只支持 PDF 文件")
            data = await upload.read()
            if not data:
                raise ValueError("文件是空的")
            if len(data) > MAX_PDF_BYTES:
                raise ValueError(f"文件过大（{len(data) // 1024 // 1024}MB）")
            if data[:5] != b"%PDF-":
                raise ValueError("不是有效的 PDF（缺少 PDF 文件头）")

            record = db.create_episode(
                title=Path(name).stem or "未命名论文",
                source_type="pdf",
                source_ref=None,
                options=episode_options,
                user_id=_current_user_id(request),
            )
            target = settings.upload_dir / f"{record['id']}.pdf"
            target.write_bytes(data)
            db.update_episode(record["id"], source_ref=str(target))
            await _queue(request).submit(record["id"])
            refreshed = db.get_episode(record["id"])
            assert refreshed is not None
            created.append(to_episode(refreshed, include_large=False))
        except (ValueError, OSError) as exc:
            failed.append({"ref": name, "reason": str(exc)})

    return {"created": created, "failed": failed, "total": len(created) + len(failed)}


async def _batch_from_json(request: Request, settings: Settings, limit: int) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    source_type = payload.get("source_type") or "url"
    if source_type not in ("url", "text"):
        raise HTTPException(status_code=400, detail="source_type 只能是 url 或 text")

    raw_items = payload.get("urls") if source_type == "url" else payload.get("texts")
    if not isinstance(raw_items, list) or not raw_items:
        key = "urls" if source_type == "url" else "texts"
        raise HTTPException(status_code=400, detail=f"缺少 {key}（非空数组）")
    if len(raw_items) > limit:
        raise HTTPException(status_code=400, detail=f"一次最多 {limit} 篇")

    episode_options = _parse_options(payload.get("options") or {}, settings)
    custom_title = (payload.get("title") or "").strip()
    db = _db(request)
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for item in raw_items:
        ref = str(item or "").strip()
        try:
            if source_type == "url":
                if not re.match(r"^https?://", ref, re.I):
                    raise ValueError("链接必须以 http:// 或 https:// 开头")
                text, _, _ = fetch_url_text(ref)
                title = custom_title or guess_title(text, fallback="链接论文")
                record = db.create_episode(
                    title=title,
                    source_type="url",
                    source_ref=ref,
                    options=episode_options,
                    user_id=_current_user_id(request),
                )
            else:
                if len(ref) < 200:
                    raise ValueError("文本太短（少于 200 字）")
                record = db.create_episode(
                    title=custom_title or guess_title(ref, fallback="粘贴的论文"),
                    source_type="text",
                    source_ref=None,
                    options=episode_options,
                    user_id=_current_user_id(request),
                )
                db.update_episode(record["id"], raw_text=ref)

            await _queue(request).submit(record["id"])
            refreshed = db.get_episode(record["id"])
            assert refreshed is not None
            created.append(to_episode(refreshed, include_large=False))
        except (IngestError, ValueError) as exc:
            failed.append({"ref": ref[:120], "reason": str(exc)})

    return {"created": created, "failed": failed, "total": len(created) + len(failed)}


# --------------------------------------------------------------------------
# 查询 / 管理
# --------------------------------------------------------------------------


@router.get("/episodes", response_model=EpisodeList)
async def list_episodes(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
    q: str | None = Query(None),
    album: str | None = Query(None),
    album_id: str | None = Query(None),
):
    """**只返回自己的单集**。开放模式下没有 user，返回全部（那时也没别人）。

    `album=` 传专辑 id 只看那一辑；传 `none` 看没归辑的。
    """
    valid_status = {"queued", "parsing", "analyzing", "scripting", "synthesizing", "completed", "failed"}
    if status and status not in valid_status:
        raise HTTPException(status_code=400, detail=f"未知状态：{status}")

    user = auth_lib.user_or_401(request)
    wants_album = album_id or album

    if wants_album and wants_album != "none":
        # 先校验专辑归属：否则拿别人的专辑 id 会得到一个「空列表」，
        # 看起来像「这辑里没东西」，实际是「这不是你的辑」
        _require_album(request, wants_album)

    records, total = _db(request).list_episodes(
        limit=limit,
        offset=offset,
        status=status or None,
        q=(q or "").strip() or None,
        user_id=user["id"] if user else None,
        album_id=None if wants_album in (None, "none") else wants_album,
        unassigned_only=wants_album == "none",
    )
    return {"items": [to_episode(r, include_large=False) for r in records], "total": total}


@router.get("/episodes/{episode_id}", response_model=Episode)
async def get_episode(request: Request, episode_id: str):
    return to_episode(_require_episode(request, episode_id))


@router.delete("/episodes/{episode_id}", status_code=204)
async def delete_episode(request: Request, episode_id: str) -> Response:
    record = _require_episode(request, episode_id)

    for key in ("audio_path", "cover_path", "video_path"):
        path_value = record.get(key)
        if path_value:
            try:
                Path(path_value).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("删除文件失败 %s：%s", path_value, exc)

    for figure in record.get("figures") or []:
        try:
            Path(figure.get("path") or "").unlink(missing_ok=True)
        except OSError:
            pass

    for key in ("svg_path", "png_path"):
        path_value = (record.get("illustration") or {}).get(key)
        if path_value:
            try:
                Path(path_value).unlink(missing_ok=True)
            except OSError:
                pass

    if record.get("source_type") == "pdf" and record.get("source_ref"):
        try:
            Path(record["source_ref"]).unlink(missing_ok=True)
        except OSError:
            pass

    _db(request).delete_episode(episode_id)
    return Response(status_code=204)


@router.post("/episodes/{episode_id}/retry", response_model=Episode)
async def retry_episode(request: Request, episode_id: str):
    record = _require_episode(request, episode_id)
    if record["status"] != "failed":
        raise HTTPException(
            status_code=409, detail=f"只有失败的任务可以重试（当前状态：{record['status']}）"
        )

    _db(request).update_episode(
        episode_id,
        status="queued",
        stage_label="排队中",
        progress=0,
        error=None,
        retry_count=0,
    )
    await _queue(request).submit(episode_id)
    refreshed = _db(request).get_episode(episode_id)
    assert refreshed is not None
    return to_episode(refreshed)


# --------------------------------------------------------------------------
# 产物下载
# --------------------------------------------------------------------------


def _ranged_response(request: Request, path: Path, media_type: str) -> Response:
    """带 Range 支持的媒体响应。

    音视频拖动进度条都依赖 `206 + Content-Range`，Starlette 不会自动处理，
    所以这里手动实现。缺了它 Safari 直接不能播。
    """
    file_size = path.stat().st_size
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=3600"},
        )

    match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match:
        raise HTTPException(status_code=416, detail="Range 头格式不正确")

    start_raw, end_raw = match.groups()
    if start_raw:
        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1
    else:
        # bytes=-N 表示最后 N 字节
        if not end_raw:
            raise HTTPException(status_code=416, detail="Range 头格式不正确")
        start = max(file_size - int(end_raw), 0)
        end = file_size - 1

    if start >= file_size or start > end:
        return Response(
            status_code=416, headers={"Content-Range": f"bytes */{file_size}"}
        )
    end = min(end, file_size - 1)
    length = end - start + 1

    def iterate() -> Iterator[bytes]:
        remaining = length
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iterate(),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
        },
    )


@router.get("/episodes/{episode_id}/audio")
async def get_audio(request: Request, episode_id: str, lang: str | None = Query(None)):
    record = _require_episode(request, episode_id)
    _, version = _resolve_version(request, record, lang)
    audio_path = version.get("audio_path")
    if not audio_path or not Path(audio_path).exists():
        raise HTTPException(status_code=404, detail="这一集还没有音频")

    path = Path(audio_path)
    media_type = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
    return _ranged_response(request, path, media_type)


@router.post("/episodes/{episode_id}/video/rebuild", response_model=Episode)
async def rebuild_video(
    request: Request, episode_id: str, lang: str | None = Query(None)
):
    """用现有素材重新合成视频（配图人工校正后用）。

    音频、脚本、解读都不动，只重新渲染画面并编码。**复用上次的画面分配**，
    不再调用模型 —— 否则「我只转了一张图，怎么画面全变了」。

    双语集要用 `?lang=` 指定重合成哪一版；不带则重合成主语言那一版。

    重新合成是重活（渲染 + 编码，约 20 秒），所以同步等待而不是丢进队列：
    调用方（前端）要明确知道什么时候能看到新视频。
    """
    record = _require_episode(request, episode_id)
    # 先校验语言：未知语言应当是 404，而不是走到「还没有视频」的 409
    _resolve_version(request, record, lang)
    if not record.get("video_path") and not (record.get("versions") or {}):
        raise HTTPException(status_code=409, detail="这一集还没有视频，无法重新合成")

    try:
        await _queue(request).pipeline.rebuild_video(episode_id, lang)
    except LanguageNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VideoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    refreshed = _db(request).get_episode(episode_id)
    assert refreshed is not None
    return to_episode(refreshed)


@router.get("/episodes/{episode_id}/video")
async def get_video(request: Request, episode_id: str, lang: str | None = Query(None)):
    """视频解读播客（MP4）。

    同样支持 Range：视频拖动进度条比音频更依赖它，而且播放器通常先发一个
    小 range 探测 moov box。
    """
    record = _require_episode(request, episode_id)
    _, version = _resolve_version(request, record, lang)
    video_path = version.get("video_path")
    if not video_path or not Path(video_path).exists():
        raise HTTPException(status_code=404, detail="这一集还没有视频")

    return _ranged_response(request, Path(video_path), "video/mp4")


@router.get("/episodes/{episode_id}/script.txt")
async def get_script(request: Request, episode_id: str, lang: str | None = Query(None)):
    record = _require_episode(request, episode_id)
    language, version = _resolve_version(request, record, lang)
    content = render_script_text({**record, **version}, language)
    filename = _download_filename(record, language, "-脚本.txt")
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(filename)}"
        },
    )


@router.get("/episodes/{episode_id}/analysis.md")
async def get_analysis(request: Request, episode_id: str, lang: str | None = Query(None)):
    record = _require_episode(request, episode_id)
    language, version = _resolve_version(request, record, lang)
    content = render_analysis_markdown({**record, **version}, language)
    filename = _download_filename(record, language, "-解读.md")
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(filename)}"
        },
    )


# --------------------------------------------------------------------------
# 配图资源
# --------------------------------------------------------------------------

# 配图是长缓存资源：文件名里带 episode id，内容不会变
_IMAGE_CACHE = "public, max-age=86400"


def _png_response(path: Path) -> FileResponse | Response:
    if not path.exists():
        raise HTTPException(status_code=404, detail="配图不存在")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": _IMAGE_CACHE}
    )


@router.get("/episodes/{episode_id}/cover")
async def get_cover(request: Request, episode_id: str):
    """封面：PDF 第一页的渲染图（text 来源则是生成的信息图）。"""
    record = _require_episode(request, episode_id)
    cover_path = record.get("cover_path")
    if not cover_path:
        raise HTTPException(status_code=404, detail="这一集还没有封面")
    return _png_response(Path(cover_path))


@router.get("/episodes/{episode_id}/figures/{figure_id}")
async def get_figure(request: Request, episode_id: str, figure_id: str):
    """论文原图。按图注定位后从 PDF 渲染出来的区域。"""
    record = _require_episode(request, episode_id)
    for figure in record.get("figures") or []:
        if figure.get("id") == figure_id:
            return _png_response(Path(figure.get("path") or ""))
    raise HTTPException(status_code=404, detail="配图不存在")


@router.post("/episodes/{episode_id}/figures/{figure_id}/rotate", response_model=Episode)
async def rotate_figure(
    request: Request, episode_id: str, figure_id: str, payload: FigureRotateRequest
):
    """人工校正配图方向（顺时针 / 逆时针 90°）。

    自动判定不可能总是对：论文配图千奇百怪，而判断「图正不正」最终要靠人眼。
    所以给一个手动兜底 —— 看的人觉得歪了，转一下就好。

    PNG 旋转 90° 无损，转多了转回来即可。文件 mtime 变化会让 URL 上的
    版本号跟着变，浏览器不会再用缓存的旧图。
    """
    record = _require_episode(request, episode_id)
    figures = record.get("figures") or []

    target = next((f for f in figures if f.get("id") == figure_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="配图不存在")

    path = Path(target.get("path") or "")
    if not path.exists():
        raise HTTPException(status_code=404, detail="配图文件已丢失")

    try:
        width, height = rotate_image_file(path, payload.direction)
    except PdfAssetsError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    target["width"] = width
    target["height"] = height
    _db(request).update_episode(episode_id, figures=figures)

    refreshed = _db(request).get_episode(episode_id)
    assert refreshed is not None
    return to_episode(refreshed)


@router.delete("/episodes/{episode_id}/figures/{figure_id}", response_model=Episode)
async def delete_figure(request: Request, episode_id: str, figure_id: str):
    """删掉一张不需要的配图（比如论文里提取到的装饰性图表）。

    只从这一集里移除，不删源 PDF。
    """
    record = _require_episode(request, episode_id)
    figures = record.get("figures") or []

    remaining = [f for f in figures if f.get("id") != figure_id]
    if len(remaining) == len(figures):
        raise HTTPException(status_code=404, detail="配图不存在")

    try:
        Path(next(f["path"] for f in figures if f["id"] == figure_id)).unlink(missing_ok=True)
    except (OSError, KeyError, StopIteration):
        pass

    _db(request).update_episode(episode_id, figures=remaining)
    refreshed = _db(request).get_episode(episode_id)
    assert refreshed is not None
    return to_episode(refreshed)


@router.get("/episodes/{episode_id}/illustration.png")
async def get_illustration_png(
    request: Request, episode_id: str, lang: str | None = Query(None)
):
    """生成的信息图（栅格版），用于列表缩略图等场景。"""
    record = _require_episode(request, episode_id)
    _, version = _resolve_version(request, record, lang)
    stored = version.get("illustration") or {}
    if not stored.get("png_path"):
        raise HTTPException(status_code=404, detail="这一集还没有生成配图")
    return _png_response(Path(stored["png_path"]))


@router.get("/episodes/{episode_id}/illustration.svg")
async def get_illustration_svg(
    request: Request, episode_id: str, lang: str | None = Query(None)
):
    """生成的信息图（原始 SVG）。

    直出 SVG 是为了让里面的 SMIL 动画能播放——截图成 PNG 就变死图了。
    内容是模型生成的，已在入库前做过白名单清洗（无 script / 无外链），
    这里再补一层 CSP 兜底。前端务必用 <img>/<object> 引用，不要内联进 HTML。
    """
    record = _require_episode(request, episode_id)
    _, version = _resolve_version(request, record, lang)
    stored = version.get("illustration") or {}
    svg_path = stored.get("svg_path")
    if not svg_path or not Path(svg_path).exists():
        raise HTTPException(status_code=404, detail="这一集还没有生成配图")

    return FileResponse(
        Path(svg_path),
        media_type="image/svg+xml",
        headers={
            "Cache-Control": _IMAGE_CACHE,
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "论文").strip("_ ")
    return (cleaned or "论文")[:60]


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
