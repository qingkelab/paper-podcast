"""给存量集补回图解镜头（**调模型**：1 次草案 + 最多 1 次修复）。

图解原来**没写回 `video.scenes[i].diagram`**，所以「重新合成」会静默丢掉它 ——
存量集的成片里那些图解是当时渲进去的，数据里没有。这条命令重新问一次模型补上。

⚠️ 会改**两份**数据（顶层 `video_json` 与 `versions[lang].video`）：
只改顶层会被下一次重合成冲成 null（踩过一次）。

用法：
    ../.venv/bin/python -m app.maintenance.backfill_diagrams --dry-run
    ../.venv/bin/python -m app.maintenance.backfill_diagrams e3083750
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.services import video as video_module  # noqa: E402
from app.services.llm import LLMClient  # noqa: E402
from app.services.pipeline import Pipeline  # noqa: E402
from app.services.podcast_tts import RoundTiming  # noqa: E402
from app.services.video_core import diagram_shot_to_dict  # noqa: E402

from ._common import connect, find, list_episodes, print_table  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补图解镜头（会调模型）")
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    episodes = list_episodes(ids=args.ids or None)
    if not episodes:
        print("没有可处理的集")
        return 1
    print_table(episodes)
    settings = Settings()
    db = Database(Path(settings.db_path))
    pipeline = Pipeline(settings, db)
    con = connect()

    for episode in episodes:
        record = db.get_episode(episode.id)
        _, version = pipeline.resolve_version(record, "zh")
        segments = (version.get("script") or {}).get("segments") or []
        timings = [
            RoundTiming(index=int(t.get("index", i)), speaker=str(t.get("speaker") or ""),
                        start=float(t.get("start") or 0.0), end=float(t.get("end") or 0.0))
            for i, t in enumerate(version.get("timings") or [])
        ]
        analysis = version.get("analysis") or {}
        figures = record.get("figures") or []
        video, versions = find(episode.id)
        scenes = video.get("scenes") or []
        have = [s["index"] for s in scenes if s.get("diagram")]
        print(f"\n{episode.id}：已有图解的段 {have or '无'}")
        if args.dry_run:
            continue

        pool = {
            asset_id: video_module.ImageAsset(
                id=asset_id, path=Path(path),
                kind="cover" if asset_id == "cover" else "figure",
            )
            for asset_id, path in (video.get("assets") or {}).items()
        }
        report: dict = {}
        shots = video_module.assign_diagram_shots(
            segments=segments,
            timings=timings,
            evidence=video_module.diagram_evidence(analysis, segments, figures),
            llm=LLMClient(settings),
            available_images=[(aid, str(a.caption or "")) for aid, a in pool.items()],
            report=report,
        )
        print("  拿到：", {k: v.title for k, v in shots.items()} or "无")
        if not shots:
            continue
        targets = [video] + [
            payload["video"] for payload in versions.values()
            if isinstance(payload.get("video"), dict)
        ]
        for target in targets:
            rows = target.get("scenes") or []
            for index, shot in shots.items():
                if 0 <= index < len(rows) and not rows[index].get("diagram"):
                    rows[index]["diagram"] = diagram_shot_to_dict(shot)
        con.execute("update episodes set video_json=?, versions_json=? where id=?",
                    (json.dumps(video, ensure_ascii=False),
                     json.dumps(versions, ensure_ascii=False), episode.id))
        con.commit()
        print("  已写回两份数据；现在各段的图解：",
              [s["index"] for s in video.get("scenes") or [] if s.get("diagram")])

    if args.dry_run:
        print("\n--dry-run：没有调模型、没有改数据。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
