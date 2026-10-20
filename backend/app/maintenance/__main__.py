"""`python -m app.maintenance`：列出可用的维护动作。"""

from __future__ import annotations

COMMANDS = (
    ("rebuild_videos", "批量重新合成成片（**不调模型**，纯渲染；应用当前版式）"),
    ("reassign_images", "重新跑一次逐句配图（**调模型**：1 次选图 + 最多 N 张生成图）"),
    ("backfill_diagrams", "给存量集补回图解镜头（**调模型**：1 次草案 + 最多 1 次修复）"),
    ("audit_seams", "接缝审计（**只读**：逐切点量两侧的动对不对得上）"),
)


def main() -> int:
    print("paper-podcast 维护工具\n")
    for name, note in COMMANDS:
        print(f"  python -m app.maintenance.{name}\n      {note}")
    print("\n每个子命令都支持 --dry-run（只说要做什么，不动数据）。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
