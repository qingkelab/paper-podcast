"""批量重新合成成片：**不调模型**，只按当前版式重新渲染 + 编码。

用法：

    cd backend
    ../.venv/bin/python -m app.maintenance.rebuild_videos --dry-run      # 只看会动哪些
    ../.venv/bin/python -m app.maintenance.rebuild_videos                # 全量
    ../.venv/bin/python -m app.maintenance.rebuild_videos 100dce22 f7c8af00

为什么要有它：改一次版式（比如「强调行固定回底部」「删掉竖条」），
**存量的成片不会自己变**——配图和文案都烘焙在 MP4 里，必须重新合成一遍。
以前这一步是 `/tmp/rebuild_all.py` 里手抄的，换个 shell 就得重写。

约定：
- 走 `Pipeline.rebuild_video`（复用 `video.scenes` 的配图分配与 `hook`，**不再问模型**）；
- 一集失败不影响其他集，失败的集单独列出来（不静默跳过）；
- 每集结束后核对两件硬指标：帧数 == round(时长×30)、音画差 ≤0.3 秒。
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
from app.services.pipeline import Pipeline  # noqa: E402

from ._common import list_episodes, print_table  # noqa: E402


def _frames(path: Path) -> int | None:
    import subprocess

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_frames", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=False,
    )
    text = result.stdout.strip()
    return int(text) if text.isdigit() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量重新合成成片（不调模型）")
    parser.add_argument("ids", nargs="*", help="只处理这些集（默认全部已完成）")
    parser.add_argument("--dry-run", action="store_true", help="只列表，不动数据")
    parser.add_argument("--orientation", default="portrait", choices=["portrait", "landscape"])
    args = parser.parse_args(argv)

    episodes = list_episodes(ids=args.ids or None)
    if not episodes:
        print("没有可处理的集")
        return 1
    print(f"将重新合成 {len(episodes)} 集（画幅 {args.orientation}）：")
    print_table(episodes)
    if args.dry_run:
        print("\n--dry-run：什么都没动。")
        return 0

    settings = Settings()
    db = Database(Path(settings.db_path))
    pipeline = Pipeline(settings, db)
    failed: list[tuple[str, str]] = []
    checked: list[str] = []
    for index, episode in enumerate(episodes, start=1):
        print(f"\n[{index}/{len(episodes)}] {episode.id} …", flush=True)
        try:
            result = asyncio.run(pipeline.rebuild_video(episode.id, "zh", args.orientation))
        except Exception as exc:  # noqa: BLE001  —— 一集失败不该拖垮整批
            failed.append((episode.id, f"{type(exc).__name__}: {exc}"))
            print(f"  失败：{type(exc).__name__}: {exc}")
            continue
        video = (result or {}).get("video") or {}
        path = Path(video.get("path") or "")
        audio = episode.duration_sec
        frames = _frames(path) if path.exists() else None
        expected = round(float(video.get("duration_sec") or 0) * 30)
        drift = float(video.get("duration_sec") or 0) - audio
        # 帧数**不会**精确等于 `round(时长×30)`：每段是各自四舍五入到整数帧的
        # （`-frames:v int(round(段时长×30))`），27 段累计差个几帧是正常的。
        # 容差取「每段最多半帧」，再加 1 帧兜底；真正的不变量是**时长**（靠 `-shortest`）。
        tolerance = int(len(video.get("scenes") or []) * 0.5) + 1
        frames_ok = frames is not None and abs(frames - expected) <= tolerance
        ok = frames_ok and abs(drift) <= 0.3
        print(
            f"  时长 {video.get('duration_sec')}s / 音频 {audio:.2f}s（差 {drift:+.2f}）"
            f"  帧数 {frames}（期望 {expected}，容差 ±{tolerance}）  {'✓' if ok else '✗ 不达标'}"
        )
        if not ok:
            failed.append((episode.id, f"帧数 {frames} vs {expected}（容差 ±{tolerance}）或音画差 {drift:+.2f}s"))
        checked.append(episode.id)

    print(f"\n完成 {len(checked)} 集，失败 {len(failed)} 集")
    for episode_id, reason in failed:
        print(f"  ✗ {episode_id}: {reason}")
    return 0 if not failed else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
