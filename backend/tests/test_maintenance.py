"""维护工具（`app.maintenance`）的入口自检。

这一层以前是 `/tmp/xxx.py` 里手抄的脚本，换个 shell 就得重写一遍。收进仓库之后，
最容易坏的其实不是逻辑，而是**入口**：`python -m app.maintenance.xxx` 拼错、
包没打成模块、`sys.path` 没设好 —— 这些都会让「需要时照上面的三步重写」重演。
所以这里用子进程真的跑一次入口，而不是 import 一下就算过。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_index_lists_every_command():
    result = _run("app.maintenance")
    assert result.returncode == 0, result.stderr
    for command in (
        "app.maintenance.rebuild_videos",
        "app.maintenance.reassign_images",
        "app.maintenance.backfill_diagrams",
        "app.maintenance.audit_seams",
    ):
        assert command in result.stdout, f"索引里没有 {command}"
    assert "--dry-run" in result.stdout


def test_rebuild_dry_run_lists_episodes_and_touches_nothing():
    result = _run("app.maintenance.rebuild_videos", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "--dry-run：什么都没动" in result.stdout
    # 列表里应该有表头（有集时才有数据行，空库也不该报错）
    assert "设定" in result.stdout or "没有可处理的集" in result.stdout
