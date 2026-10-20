"""接缝审计（**只读**）：逐切点量「切点两侧的动对不对得上」。

公式与判据见 `docs/VIDEO_QA.md`「接缝审计」一节；这里只是把它跑成一张表。
不调模型、不改数据 —— 想当闸门之前先把现状量清楚（存量 398 处切点两侧都没在动）。

用法：
    ../.venv/bin/python -m app.maintenance.audit_seams              # 全量
    ../.venv/bin/python -m app.maintenance.audit_seams e3083750    # 只跑一集
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.config import Settings  # noqa: E402
from app.services.seam_audit import audit_seams  # noqa: E402

from ._common import connect, list_episodes  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="接缝审计（只读）")
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--verbose", action="store_true", help="逐个切点打印问题")
    # 只读命令：--dry-run 与正常运行完全一样，接受它是为了四条子命令的用法一致
    parser.add_argument("--dry-run", action="store_true", help="（只读命令，无副作用）")
    args = parser.parse_args(argv)
    if args.dry_run:
        print("（这只命令本来就只读：dry-run 与正常运行相同）\n")

    settings = Settings()
    videos = Path(settings.video_dir)
    con = connect()
    total = {"seams": 0, "settled_before": 0, "rest_after": 0, "dissolve": 0, "axis_flip": 0}
    rows = []
    for episode in list_episodes(ids=args.ids or None):
        video = videos / f"{episode.id}.mp4"
        row = con.execute("select timings_json from episodes where id=?", (episode.id,)).fetchone()
        if not video.exists() or not row or not row[0]:
            rows.append((episode.id, "（缺视频或 timings）", None))
            continue
        starts = sorted(float(t["start"]) for t in json.loads(row[0]))
        report = audit_seams(video, [t for t in starts[1:] if t > 0.7])
        counts = report.counts
        for key in total:
            total[key] += counts[key]
        rows.append((episode.id, report.summary(), counts))
        if args.verbose:
            for seam in report.seams:
                if seam.issues():
                    print(f"  {episode.id} t={seam.at:7.2f}s {seam.kind:<9} "
                          f"{'；'.join(seam.issues()[:2])}")

    print(f"{'集':<12}{'切点':<6}{'退出已静止':<10}{'进入静止':<9}{'叠化':<6}{'方向对不上':<10}摘要")
    for episode_id, summary, counts in rows:
        if counts is None:
            print(f"{episode_id:<12}{summary}")
            continue
        print(f"{episode_id:<12}{counts['seams']:<6}{counts['settled_before']:<10}"
              f"{counts['rest_after']:<9}{counts['dissolve']:<6}{counts['axis_flip']:<10}{summary[:40]}")
    n = max(total["seams"], 1)
    print(f"\n合计 {total['seams']} 处切点：退出时已静止 {total['settled_before']}（{total['settled_before']/n:.0%}）、"
          f"进入从静止开始 {total['rest_after']}（{total['rest_after']/n:.0%}）、"
          f"渐变/叠化 {total['dissolve']}、方向对不上 {total['axis_flip']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
