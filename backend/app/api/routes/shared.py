"""接口层的公共零件：router、依赖、序列化、鉴权归属、配额、通用响应。

**判据：被两个以上接口模块用到的东西才放这里。** 只服务单个接口的工具留在那个模块
（比如 `_parse_options()` 在 `episodes.py`）。各接口模块一律 `from .shared import ...`，
**模块之间不互相 import** —— 否则「改一个接口要读几个文件」这件事又会回来。

`router` 也定义在这里：各模块 `from .shared import router` 之后用 `@router.get(...)`
注册自己的接口。`app/main.py` 照样写 `from .api.routes import router`（由包的
`__init__.py` 按原顺序 import 各模块，一个字都不用改）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from ... import auth as auth_lib
from ...config import Settings
from ...db import Database
from ...models import EpisodeOptions, ShareAuthor
from ...services.pipeline import LanguageNotFound
from ...services.video import is_video_stale
from ...worker import TaskQueue

router = APIRouter(prefix="/api")


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
            # 封面大字标题（逐语言一份，用户可以改，见 PATCH /cover）
            "hook": str(stored_video.get("hook") or ""),
            "poster_url": _poster_url(episode_id, stored_video, query),
            # 标题改了但还没重新合成（视频里烘的还是旧标题）
            "title_pending": bool(stored_video.get("title_pending")),
            # 这份画面是 **HTML（Chrome 无头）** 还是 **SVG（resvg）** 画的。
            # 两条路径同时在（没装 Chrome 就自动降级），所以「同一篇论文两台机器
            # 出的画面不一样」得靠它才分得清 —— 只躺在库里的话这条线索等于查不到：
            # `VideoInfo.renderer` 一直是个恒为 null 的字段。
            "renderer": str(stored_video.get("renderer") or "") or None,
            # 视频**没出来**时的原因（见 `pipeline._record_video_failure`）。
            # 没有这个字段的话，「音频有、视频没有」在页面上完全看不出来 ——
            # 实测连着三集都被当成「项目坏了」，每次都要人工翻服务器日志。
            "error": str(stored_video.get("error") or "") or None,
        }

    # 横版：与竖版并存的一份（`?orientation=landscape`，1920×1080）
    video_landscape = None
    if include_large:
        stored_landscape = version.get("video_landscape") or {}
        landscape_path = stored_landscape.get("path")
        if landscape_path and Path(landscape_path).exists():
            query = _lang_query(record, version)
            separator = "&" if query else "?"
            video_landscape = {
                "url": (
                    f"/api/episodes/{episode_id}/video{query}"
                    f"{separator}orientation=landscape&v={_asset_version(landscape_path)}"
                ),
                "duration_sec": stored_landscape.get("duration_sec"),
                "scene_count": stored_landscape.get("scene_count"),
                "bytes": stored_landscape.get("bytes"),
                # 横版和竖版用的是同一批素材，过时判断一致
                "stale": is_video_stale(stored_landscape)
                or _video_provenance_unknown(stored_landscape, record, version),
                "orientation": "landscape",
                # 两种画幅共用同一句封面标题（它属于这个语言版本，不属于某一份文件）
                "hook": str((version.get("video") or {}).get("hook") or ""),
                # 同理：改标题对横竖两版都还没生效
                "title_pending": bool((version.get("video") or {}).get("title_pending")),
                # 但静帧是**各自一份**（横竖两个画面完全不一样）
                "poster_url": _poster_url(episode_id, stored_landscape, query, orientation="landscape"),
                # 横版和竖版可能落在不同的渲染路径上（比如竖版渲染完 Chrome 挂了、
                # 横版退回 SVG），所以这个字段是**各报各的**，不是共用
                "renderer": str(stored_landscape.get("renderer") or "") or None,
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
        "video_landscape": video_landscape,
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
            # 横版也要带进来：漏掉它的话，单语言集（没有 versions）生成横版后
            # 接口会看不到那一份 —— 实测踩到过
            "video_landscape": record.get("video_landscape"),
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
        # 顶层镜像主语言那一版（同 video），老前端不改也能看到横版
        "video_landscape": surface.get("video_landscape"),
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


def _quota_window_start() -> str:
    """配额窗口起点：24 小时前（滑动窗口）。

    刻意用滑动窗口而不是自然日：自然日的「今天」取决于服务器时区，
    而用户在哪都能用；而且午夜一到额度齐刷刷重置，反而更容易被集中薅。
    """
    return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")


def _check_generation_quota(request: Request, wants: int = 1) -> None:
    """还有没有额度。超出抛 429，并在头里给出重置时刻。

    一次生成要调大模型 + 语音合成 + 视频编码，是真金白银。有了多账号之后，
    不给上限就等于把账单交给任何注册进来的人。
    """
    settings = _settings(request)
    limit = int(settings.daily_generation_limit or 0)
    if limit <= 0:
        return
    user = auth_lib.current_user(request)
    if user is None:
        # 开放模式（库里没有账号）：本来就只有部署者自己用，不设限
        return

    since = _quota_window_start()
    used = _db(request).count_episodes_since(user["id"], since)
    if used + wants > limit:
        resets_at = (
            datetime.fromisoformat(since) + timedelta(hours=24)
        ).isoformat(timespec="seconds")
        raise HTTPException(
            status_code=429,
            detail=(
                f"24 小时内最多生成 {limit} 期（已用 {used}）。"
                f"额度会在 {resets_at[:16].replace('T', ' ')} UTC 前后恢复。"
            ),
            headers={"X-Quota-Reset": resets_at},
        )


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
# 公开视图的构造（分享页与首页展示块共用）
# --------------------------------------------------------------------------


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
        poster_path = str(stored.get("poster") or "")
        video = {
            "url": f"{base}/video{query}&v={_asset_version(video_path)}",
            "duration_sec": stored.get("duration_sec"),
            "scene_count": stored.get("scene_count"),
            "bytes": stored.get("bytes"),
            "stale": False,  # 公开页不显示「可以重新合成」这类只有作者在意的状态
            # ⚠️ 下面这几个**作者通道一直是给的，公开通道漏了** —— 后果很具体：
            # 展示页那一块是「首帧海报 + 播放按钮」，没有 poster_url 就只能退回
            # 论文首页原图（**没有标题、不点播放永远是静止图**），
            # 看起来就像「展示页没有视频」。两条通道共用同一套拼装是本文件的规矩，
            # 这里就是那个「一边忘了加新字段」的实例。
            "poster_url": (
                f"{base}/video/poster{query}&v={_asset_version(poster_path)}"
                if poster_path and Path(poster_path).exists()
                else None
            ),
            "orientation": stored.get("orientation") or "portrait",
            "hook": str(stored.get("hook") or ""),
            "renderer": str(stored.get("renderer") or "") or None,
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


# --------------------------------------------------------------------------
# 视频静帧 / 横版的地址（序列化与路由都要用）
# --------------------------------------------------------------------------


def _poster_url(
    episode_id: str, stored: dict[str, Any], query: str, *, orientation: str = "portrait"
) -> str | None:
    """视频第一帧静帧的地址（前端当 `<video poster>` 用）。

    没有这份文件时返回 None：前端会退回用论文首页当封面 —— 那是**老视频**的行为，
    新合成的视频都会带上静帧（见 `video.capture_poster`）。
    """
    path = stored.get("poster")
    if not path or not Path(path).exists():
        return None
    separator = "&" if query else "?"
    extra = "" if orientation == "portrait" else f"orientation={orientation}&"
    return (
        f"/api/episodes/{episode_id}/video/poster{query}"
        f"{separator}{extra}v={_asset_version(path)}"
    )


def _landscape_path(version: dict[str, Any]) -> str | None:
    """横版视频的文件路径。存在 `video_landscape.path` 里（见 pipeline.rebuild_video）。"""
    stored = version.get("video_landscape") or {}
    path = stored.get("path")
    return str(path) if path else None


# 配图是长缓存资源：文件名里带 episode id，内容不会变
_IMAGE_CACHE = "public, max-age=86400"


def _png_response(path: Path) -> FileResponse | Response:
    if not path.exists():
        raise HTTPException(status_code=404, detail="配图不存在")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": _IMAGE_CACHE}
    )


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "论文").strip("_ ")
    return (cleaned or "论文")[:60]


def _quote(value: str) -> str:
    return quote(value, safe="")
