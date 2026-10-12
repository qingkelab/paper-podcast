"""单集：创建（PDF 上传 / 链接 / 粘贴文本 / 批量）、列表、详情、删除、重试。

单个与批量两条入口共用 `_parse_options()` 解析生成参数；删除要覆盖**每一个语言
版本**的产物（见 `delete_episode()` 的注释），别只看顶层字段。
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Query, Request, Response
from starlette.datastructures import UploadFile as StarletteUploadFile

from ... import auth as auth_lib
from ...config import Settings
from ...db import DEFAULT_EPISODE_SORT, EPISODE_ORDER, RUNNING_STATUS
from ...models import BatchResult, Episode, EpisodeList, PublicLibrary
from ...services.ingest import IngestError, fetch_url_text, guess_title
from ...voices import normalize_voice
from .shared import (
    _all_versions,
    _primary_language,
    _share_asset_urls,
    router,
    _check_generation_quota,
    _current_user_id,
    _db,
    _queue,
    _require_album,
    _require_episode,
    _settings,
    to_episode,
)

logger = logging.getLogger(__name__)


MAX_PDF_BYTES = 40 * 1024 * 1024  # 40MB，覆盖绝大多数论文
MAX_TEXT_CHARS = 200_000


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
    _check_generation_quota(request)
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
    _check_generation_quota(request, wants=limit)

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


VALID_STATUS = {
    "queued", "parsing", "analyzing", "scripting", "synthesizing", "completed", "failed",
}


def _check_sort(sort: str) -> str:
    """排序是**封闭白名单**：未知取值 400，不静默退回默认排序。

    「我的库」和「公开库」两条列表共用它 —— 各写一遍的话，迟早有一边允许了
    一个另一边会报错的排序串。
    """
    if sort not in EPISODE_ORDER:
        raise HTTPException(
            status_code=400,
            detail=f"未知排序：{sort}（可选：{', '.join(EPISODE_ORDER)}）",
        )
    return sort


@router.get("/library", response_model=PublicLibrary)
async def public_library(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None),
    sort: str = Query(DEFAULT_EPISODE_SORT),
):
    """**公开库**：所有点过分享、并且已经跑完的单集。**免登录**。

    为什么要有它：产品介绍页原来靠 `/api/showcase` 只露一期，访客看完一期就没了；
    而「先登录才能看别人的作品」对一个作品集式的站点是反过来的 —— 想让人看见，
    就得先让人看见。这里给的是**一页列表**，点进去走的是公开分享页
    `/share/{token}`（同一套免登录资源通道）。

    **只含 `visibility='public'` 且 `status='completed'`**：私有单集、别人库里
    没公开的东西、跑失败的半成品，一个都不会出现在这里（条件的唯一来源是
    `db._PUBLIC_WHERE`）。返回的是白名单结构 `PublicEpisodeItem` ——
    不含内部 id、user_id、options、source_ref（那可能是磁盘绝对路径）、raw_text。

    `q=` 搜标题、`sort=` 见 `db.EPISODE_ORDER`，未知取值 400。
    """
    sort = _check_sort(sort)
    records, total = _db(request).list_public_episodes(
        limit=limit,
        offset=offset,
        q=(q or "").strip() or None,
        sort=sort,
    )
    return {
        "items": [_public_item(request, r) for r in records],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _public_item(request: Request, record: dict[str, Any]) -> dict[str, Any]:
    """一行公开库卡片。作者只给展示名（和分享页同一套规矩）。"""
    token = str(record.get("share_token") or "")
    versions = _all_versions(record)
    primary = _primary_language(record)
    surface = versions.get(primary) or (next(iter(versions.values())) if versions else {})
    owner = _db(request).get_user(record["user_id"]) if record.get("user_id") else None
    # ⚠️ 主语言**不能读 `record["language"]`**：那一列在库里根本不存在，
    # 主语言是由 `versions` / `options` 推出来的（`_primary_language`）。
    # 踩过：写成 `record.get("language")` 会让 `versions.get(None)` 恒为空，
    # 于是每个卡片都显示「没有视频、没有音频」（端点看着是 200，数据全是 false）。
    return {
        "token": token,
        "title": record.get("title") or "",
        "author": {"display_name": owner["display_name"]} if owner else None,
        "created_at": record.get("created_at") or "",
        "language": primary,
        "languages": list(versions.keys()) or [primary],
        "duration_sec": surface.get("audio_duration_sec") or record.get("audio_duration_sec"),
        "cover_url": _share_asset_urls(record, token).get("cover_url"),
        "has_video": bool(surface.get("video_path") or surface.get("video")),
        "has_audio": bool(surface.get("audio_path")),
    }


@router.get("/episodes", response_model=EpisodeList)
async def list_episodes(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
    q: str | None = Query(None),
    album: str | None = Query(None),
    album_id: str | None = Query(None),
    sort: str = Query(DEFAULT_EPISODE_SORT),
):
    """**只返回自己的单集**。开放模式下没有 user，返回全部（那时也没别人）。

    `album=` 传专辑 id 只看那一辑；传 `none` 看没归辑的。
    `status=running` 是伪状态：所有**还没跑完的**（排队/解析/解读/脚本/合成）。
    `sort=` 见 `db.EPISODE_ORDER` 的白名单；未知取值直接 400，
    不静默退回默认排序 —— 前端写错了排序应当场知道，
    而不是变成「点了按标题排，顺序没变」这种要盯半天才发现的问题。
    """
    if status and status not in VALID_STATUS and status != RUNNING_STATUS:
        raise HTTPException(status_code=400, detail=f"未知状态：{status}")
    sort = _check_sort(sort)

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
        sort=sort,
    )
    return {"items": [to_episode(r, include_large=False) for r in records], "total": total}


@router.get("/episodes/{episode_id}", response_model=Episode)
async def get_episode(request: Request, episode_id: str):
    return to_episode(_require_episode(request, episode_id))


def _remove_quietly(path_value: Any) -> None:
    if not path_value:
        return
    try:
        Path(path_value).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("删除文件失败 %s：%s", path_value, exc)


def _remove_tree_quietly(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        logger.warning("删除目录失败 %s：%s", path, exc)


@router.delete("/episodes/{episode_id}", status_code=204)
async def delete_episode(request: Request, episode_id: str) -> Response:
    """删掉这一集以及它的全部产物。

    ⚠️ 必须覆盖**每一个语言版本**，不能只看顶层字段：
    双语集每个语言各有一份信息图、音频、视频，只看顶层会漏掉非主语言那份，
    那些文件就永远留在磁盘上了。
    """
    record = _require_episode(request, episode_id)

    # 收集所有语言的产物路径（顶层字段只镜像主语言）
    versions = record.get("versions") or {}
    if not versions:
        versions = {
            "zh": {
                "illustration": record.get("illustration"),
                "audio_path": record.get("audio_path"),
                "video_path": record.get("video_path"),
            }
        }

    for key in ("audio_path", "cover_path", "video_path"):
        _remove_quietly(record.get(key))

    for version in versions.values():
        _remove_quietly(version.get("audio_path"))
        _remove_quietly(version.get("video_path"))
        for key in ("svg_path", "png_path"):
            _remove_quietly((version.get("illustration") or {}).get(key))
        # 视频用到的段落配图（现场生成的 topic 图）也在 video.assets 里
        for asset_path in (version.get("video") or {}).get("assets", {}).values():
            _remove_quietly(asset_path)

    for figure in record.get("figures") or []:
        _remove_quietly(figure.get("path"))

    for key in ("svg_path", "png_path"):
        _remove_quietly((record.get("illustration") or {}).get(key))

    if record.get("source_type") == "pdf" and record.get("source_ref"):
        _remove_quietly(record["source_ref"])

    # 渲染中间产物（每帧 SVG、段落配图）整个目录一起删。
    # 这些文件不在数据库里，只能按目录名（= episode id）找 —— 实测漏了这一步，
    # 删过的单集会在 data/video-frames/ 下留一整套帧文件，永远没人回收。
    _remove_tree_quietly(_settings(request).video_work_dir / episode_id)

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
