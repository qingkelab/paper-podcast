"""重新跑一次**逐句配图**（会调模型），把新分配写回视频数据，再重合成。

为什么需要它：`rebuild_video` 会**复用** `video.scenes[i].image`（规矩 3：重合成不许调模型）。
所以「配图选得不对」这件事只能靠这条单独跑一次来修 —— 以前是 `/tmp/reassign_images.py`。

成本（实测）：
- 一次逐句选图调用（~4000 token 级）；
- 若模型填了 `generate`，每个生成组**一次调用、约 10 秒 / 3000+ token**（受
  `max_topic_images`(默认 12) 与「同一张原图最多连续 3 段」两条约束）。

用法：
    ../.venv/bin/python -m app.maintenance.reassign_images --dry-run
    ../.venv/bin/python -m app.maintenance.reassign_images e3083750
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.services import video as video_module  # noqa: E402
from app.services.llm import LLMClient  # noqa: E402
from app.services.pipeline import Pipeline, language_plan, video_filename  # noqa: E402
from app.services.podcast_tts import RoundTiming  # noqa: E402

from ._common import list_episodes, print_table  # noqa: E402


def _runs(picks: list[str], timings: list[RoundTiming]) -> list[tuple[str, int, float]]:
    import itertools

    out = []
    for image, group in itertools.groupby(range(len(picks)), key=lambda i: picks[i]):
        idx = list(group)
        secs = sum(timings[i].end - timings[i].start for i in idx if i < len(timings))
        out.append((image, len(idx), round(secs, 1)))
    return sorted(out, key=lambda row: -row[2])


def _report(picks: list[str], timings: list[RoundTiming], tag: str) -> None:
    runs = _runs(picks, timings)
    if runs:
        print(f"  {tag}：最长 {runs[0][0]} {runs[0][1]} 段 / {runs[0][2]} 秒；前 3 {runs[:3]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重新跑一次逐句配图（会调模型）")
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--dry-run", action="store_true", help="只看现状，不调模型")
    args = parser.parse_args(argv)

    episodes = list_episodes(ids=args.ids or None)
    if not episodes:
        print("没有可处理的集")
        return 1
    print_table(episodes)

    settings = Settings()
    db = Database(Path(settings.db_path))
    pipeline = Pipeline(settings, db)
    for episode in episodes:
        record = db.get_episode(episode.id)
        primary, _ = language_plan(record.get("options") or {}, settings)
        lang, version = pipeline.resolve_version(record, "zh")
        segments = (version.get("script") or {}).get("segments") or []
        stored = version.get("video") or {}
        timings = [
            RoundTiming(index=int(t.get("index", i)), speaker=str(t.get("speaker") or ""),
                        start=float(t.get("start") or 0.0), end=float(t.get("end") or 0.0))
            for i, t in enumerate(version.get("timings") or [])
        ]
        print(f"\n{episode.id}：{len(segments)} 段")
        _report([s.get("image") or "" for s in (stored.get("scenes") or [])], timings, "改前")
        if args.dry_run:
            continue

        audio_path = version.get("audio_path")
        if not audio_path or not Path(audio_path).exists():
            print("  跳过：音频已丢失")
            continue
        output_path = settings.video_dir / video_filename(episode.id, lang, primary, "portrait")
        result = video_module.compose_video(
            segments=segments,
            timings=timings,
            audio_path=Path(audio_path),
            audio_duration=float(version.get("audio_duration_sec") or 0.0),
            cover_path=record.get("cover_path"),
            figures=record.get("figures") or [],
            illustration_png=(version.get("illustration") or {}).get("png_path")
            or (record.get("illustration") or {}).get("png_path"),
            work_dir=settings.video_work_dir / episode.id / lang,
            output_path=output_path,
            title=record.get("title") or "论文解读",
            llm=LLMClient(settings),        # ← 给了模型才会重新问一次选图
            analysis=version.get("analysis") or {},
            preset_scenes=None,             # ← 不给 preset，才走「重新问」那条路
            preset_assets=None,
            preset_hook=str(stored.get("hook") or ""),
            language=lang,
            orientation="portrait",
        )
        _report([s.get("image") or "" for s in result.scenes], timings, "改后")
        video = {**result.to_dict(), "url": f"/api/episodes/{episode.id}/video",
                 "orientation": "portrait", "path": str(result.video_path)}
        versions = json.loads(json.dumps(record.get("versions") or {}))
        entry = dict(versions.get(lang) or {})
        entry.update(language=lang, video_path=str(result.video_path), video=video)
        versions[lang] = entry
        db.update_episode(episode.id, video=video, versions=versions,
                          video_path=str(result.video_path))
        topics = [k for k in result.assets if k.startswith("topic")]
        print(f"  已写回；现场生成图 {len(topics)} 张：{topics}")

    if args.dry_run:
        print("\n--dry-run：没有调模型、没有改数据。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
