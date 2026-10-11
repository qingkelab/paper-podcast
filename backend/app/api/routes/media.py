"""单集产物的读取：音频、视频、静帧、封面、论文原图、信息图、脚本与解读下载。

音频和视频**必须支持 Range**（206 + Content-Range，实现见 `shared.py::_ranged_response`），
改成普通 `FileResponse` 会让 Safari 完全不能播。配图另有人工校正（rotate）与删除。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from ...models import Episode, FigureRotateRequest
from ...services.figures import PdfAssetsError, rotate_image_file
from ...services.pipeline import render_analysis_markdown, render_script_text
from .shared import (
    router,
    _IMAGE_CACHE,
    _db,
    _download_filename,
    _landscape_path,
    _png_response,
    _quote,
    _ranged_response,
    _require_episode,
    _resolve_version,
    to_episode,
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


@router.get("/episodes/{episode_id}/video")
async def get_video(
    request: Request,
    episode_id: str,
    lang: str | None = Query(None),
    orientation: str = Query("portrait"),
):
    """视频解读播客（MP4）。`orientation=landscape` 取横版（1920×1080）。

    同样支持 Range：视频拖动进度条比音频更依赖它，而且播放器通常先发一个
    小 range 探测 moov box。
    """
    record = _require_episode(request, episode_id)
    _, version = _resolve_version(request, record, lang)
    landscape = (orientation or "portrait").lower() in ("landscape", "horizontal", "16:9")
    video_path = _landscape_path(version) if landscape else version.get("video_path")
    if not video_path or not Path(video_path).exists():
        raise HTTPException(
            status_code=404,
            detail="这一集还没有横版视频" if landscape else "这一集还没有视频",
        )

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
