"""HTTP 接口层。字段与 docs/API.md 严格一致。"""

from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from ..config import Settings
from ..db import Database
from ..models import (
    Episode,
    FigureRotateRequest,
    EpisodeList,
    EpisodeListItem,
    EpisodeOptions,
    HealthResponse,
    OptionsResponse,
)
from ..services.figures import PdfAssetsError, rotate_image_file
from ..services.video import VideoError, is_video_stale
from ..services.ingest import IngestError, fetch_url_text, guess_title
from ..services.pipeline import render_analysis_markdown, render_script_text
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


def _video_provenance_unknown(stored_video: dict[str, Any], record: dict[str, Any]) -> bool:
    """视频没有留下素材版本记录时，保守地认为它可能过时。

    这类是「加版本追踪之前」生成的旧视频。无法证明它跟当前配图一致，
    与其假装没问题，不如提示可以重新合成 —— 重合成一次之后就准了。
    没有配图的集不存在这个问题。
    """
    if stored_video.get("asset_versions"):
        return False
    has_assets = bool(
        record.get("cover_path")
        or record.get("figures")
        or (record.get("illustration") or {}).get("png_path")
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


def to_episode(record: dict[str, Any], *, include_large: bool = True) -> dict[str, Any]:
    episode_id = record["id"]
    audio_url = None
    if record.get("audio_path") and Path(record["audio_path"]).exists():
        audio_url = f"/api/episodes/{episode_id}/audio"

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

    # 列表接口不带 video（列表里不播视频，省流量）
    video = None
    video_path = record.get("video_path") if include_large else None
    if video_path and Path(video_path).exists():
        stored_video = record.get("video") or {}
        video = {
            "url": f"/api/episodes/{episode_id}/video?v={_asset_version(video_path)}",
            "duration_sec": stored_video.get("duration_sec"),
            "scene_count": stored_video.get("scene_count"),
            "bytes": stored_video.get("bytes"),
            "stale": is_video_stale(stored_video) or _video_provenance_unknown(
                stored_video, record
            ),
        }

    illustration = None
    stored = record.get("illustration") if include_large else None
    if stored and stored.get("png_path") and Path(stored["png_path"]).exists():
        illustration = {
            "png_url": (
                f"/api/episodes/{episode_id}/illustration.png"
                f"?v={_asset_version(stored['png_path'])}"
            ),
            "svg_url": (
                f"/api/episodes/{episode_id}/illustration.svg"
                f"?v={_asset_version(stored.get('svg_path'))}"
            ),
            "width": int(stored.get("width") or 0),
            "height": int(stored.get("height") or 0),
            "source": stored.get("source") or "fallback",
        }

    options = record.get("options") or {}
    data = {
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
        ).model_dump(),
        "paper_meta": record.get("paper_meta"),
        "analysis": record.get("analysis") if include_large else None,
        "script": record.get("script") if include_large else None,
        "cover_url": cover_url,
        "cover_width": record.get("cover_width"),
        "cover_height": record.get("cover_height"),
        "figures": figures,
        "illustration": illustration,
        "video": video,
        "audio_url": (
            f"{audio_url}?v={_asset_version(record.get('audio_path'))}"
            if audio_url
            else None
        ),
        "audio_duration_sec": record.get("audio_duration_sec"),
        "audio_bytes": record.get("audio_bytes"),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }
    return data


def _require_episode(request: Request, episode_id: str) -> dict[str, Any]:
    record = _db(request).get_episode(episode_id)
    if not record:
        raise HTTPException(status_code=404, detail="播客不存在")
    return record


# --------------------------------------------------------------------------
# 基础信息
# --------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    settings = _settings(request)
    return HealthResponse(
        status="ok",
        version=settings.version,
        modes={"llm": settings.llm_mode, "tts": settings.tts_mode},
    )


@router.get("/options", response_model=OptionsResponse)
async def options() -> OptionsResponse:
    return OptionsResponse(durations=DURATIONS, levels=LEVELS, voices=VOICES)


# --------------------------------------------------------------------------
# 创建任务
# --------------------------------------------------------------------------


@router.post("/episodes", status_code=201, response_model=Episode)
async def create_episode(request: Request):
    """支持两种 Content-Type：
    - multipart/form-data：PDF 上传（字段 file + duration_min/level/voice_a/voice_b）
    - application/json：链接或纯文本导入
    """
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

    return {
        "duration_min": duration,
        "level": level,
        "voice_a": normalize_voice(
            _as_str(raw("voice_a")), settings.default_voice_a
        ),
        "voice_b": normalize_voice(
            _as_str(raw("voice_b")), settings.default_voice_b
        ),
    }


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


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
):
    valid_status = {"queued", "parsing", "analyzing", "scripting", "synthesizing", "completed", "failed"}
    if status and status not in valid_status:
        raise HTTPException(status_code=400, detail=f"未知状态：{status}")

    records, total = _db(request).list_episodes(
        limit=limit,
        offset=offset,
        status=status or None,
        q=(q or "").strip() or None,
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
async def get_audio(request: Request, episode_id: str):
    record = _require_episode(request, episode_id)
    audio_path = record.get("audio_path")
    if not audio_path or not Path(audio_path).exists():
        raise HTTPException(status_code=404, detail="这一集还没有音频")

    path = Path(audio_path)
    media_type = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
    return _ranged_response(request, path, media_type)


@router.post("/episodes/{episode_id}/video/rebuild", response_model=Episode)
async def rebuild_video(request: Request, episode_id: str):
    """用现有素材重新合成视频（配图人工校正后用）。

    音频、脚本、解读都不动，只重新渲染画面并编码。**复用上次的画面分配**，
    不再调用模型 —— 否则「我只转了一张图，怎么画面全变了」。

    重新合成是重活（渲染 + 编码，约 20 秒），所以同步等待而不是丢进队列：
    调用方（前端）要明确知道什么时候能看到新视频。
    """
    record = _require_episode(request, episode_id)
    if not record.get("video_path"):
        raise HTTPException(status_code=409, detail="这一集还没有视频，无法重新合成")

    try:
        await _queue(request).pipeline.rebuild_video(episode_id)
    except VideoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    refreshed = _db(request).get_episode(episode_id)
    assert refreshed is not None
    return to_episode(refreshed)


@router.get("/episodes/{episode_id}/video")
async def get_video(request: Request, episode_id: str):
    """视频解读播客（MP4）。

    同样支持 Range：视频拖动进度条比音频更依赖它，而且播放器通常先发一个
    小 range 探测 moov box。
    """
    record = _require_episode(request, episode_id)
    video_path = record.get("video_path")
    if not video_path or not Path(video_path).exists():
        raise HTTPException(status_code=404, detail="这一集还没有视频")

    return _ranged_response(request, Path(video_path), "video/mp4")


@router.get("/episodes/{episode_id}/script.txt")
async def get_script(request: Request, episode_id: str):
    record = _require_episode(request, episode_id)
    content = render_script_text(record)
    filename = f"{_safe_filename(record['title'])}-脚本.txt"
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(filename)}"
        },
    )


@router.get("/episodes/{episode_id}/analysis.md")
async def get_analysis(request: Request, episode_id: str):
    record = _require_episode(request, episode_id)
    content = render_analysis_markdown(record)
    filename = f"{_safe_filename(record['title'])}-解读.md"
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
async def get_illustration_png(request: Request, episode_id: str):
    """生成的信息图（栅格版），用于列表缩略图等场景。"""
    record = _require_episode(request, episode_id)
    stored = record.get("illustration") or {}
    if not stored.get("png_path"):
        raise HTTPException(status_code=404, detail="这一集还没有生成配图")
    return _png_response(Path(stored["png_path"]))


@router.get("/episodes/{episode_id}/illustration.svg")
async def get_illustration_svg(request: Request, episode_id: str):
    """生成的信息图（原始 SVG）。

    直出 SVG 是为了让里面的 SMIL 动画能播放——截图成 PNG 就变死图了。
    内容是模型生成的，已在入库前做过白名单清洗（无 script / 无外链），
    这里再补一层 CSP 兜底。前端务必用 <img>/<object> 引用，不要内联进 HTML。
    """
    record = _require_episode(request, episode_id)
    stored = record.get("illustration") or {}
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
