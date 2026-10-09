"""多子图切分：几何必须来自像素，不能来自模型的猜测。

实测背景（为什么这个文件存在）：让模型直接给「这一段在讲图里的哪一块」的坐标
是行不通的 —— 它看不到图。所以分工改成「几何由像素定、语义（第几个子图）由模型定」。
这个文件盯着「几何」这一半。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.panels import detect_panels, panel_count_from_caption


def grid_png(path: Path, cols: int, rows: int, *, size: int = 600, gap: int = 40) -> Path:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, size, size), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    cell_w = (size - gap * (cols + 1)) // cols
    cell_h = (size - gap * (rows + 1)) // rows
    for row in range(rows):
        for col in range(cols):
            x0 = gap + col * (cell_w + gap)
            y0 = gap + row * (cell_h + gap)
            pix.set_rect(pymupdf.IRect(x0, y0, x0 + cell_w, y0 + cell_h), (30, 60, 120))
    pix.save(str(path))
    return path


def solid_png(path: Path, *, size: int = 600, color=(30, 30, 30)) -> Path:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, size, size), False)
    pix.set_rect(pix.irect, color)
    pix.save(str(path))
    return path


class TestPanelCountFromCaption:
    """图注里的 `(a) (b) (c)` 就是「这张图有几个子图」——这是文字活，读得准。"""

    def test_counts_letters(self):
        assert panel_count_from_caption("Figure 2: Spatial structure. (a) key rows (b) value columns (c) both") == 3

    def test_counts_digits(self):
        assert panel_count_from_caption("(1) architecture (2) results") == 2

    def test_none_when_single_panel(self):
        assert panel_count_from_caption("Figure 1: overview of the model") is None
        assert panel_count_from_caption("Figure 1: only (a) one panel") is None
        assert panel_count_from_caption("") is None

    def test_caps_at_six(self):
        caption = " ".join(f"({letter}) part" for letter in "abcdefgh")
        assert panel_count_from_caption(caption) == 6


class TestDetectPanels:
    def test_splits_a_two_by_two_grid(self, tmp_path):
        panels = detect_panels(grid_png(tmp_path / "grid.png", 2, 2))
        assert len(panels) == 4, [p for p in panels]
        # 从左到右、从上到下
        assert panels[0].x < panels[1].x
        assert panels[0].y < panels[2].y
        for panel in panels:
            # 2×2 里每块约 1/4，留出沟的余量
            assert 0.15 < panel.area < 0.35, f"块大小不合理：{panel}"

    def test_splits_a_side_by_side_pair(self, tmp_path):
        panels = detect_panels(grid_png(tmp_path / "two.png", 2, 1))
        assert len(panels) == 2
        assert panels[0].x + panels[0].w <= panels[1].x + 0.02

    def test_single_panel_image_is_not_split(self, tmp_path):
        assert detect_panels(grid_png(tmp_path / "one.png", 1, 1)) == []

    def test_dense_image_is_not_split(self, tmp_path):
        # 整张都是墨：没有任何沟，就不该硬切
        assert detect_panels(solid_png(tmp_path / "solid.png")) == []

    def test_expected_count_drives_the_split(self, tmp_path):
        """图注说有 2 块时，按「最深的沟」切得出来（实测真实论文图也是这条路径有效）。"""
        panels = detect_panels(grid_png(tmp_path / "pair.png", 2, 1), expected=2)
        assert len(panels) == 2

    def test_missing_file_is_harmless(self, tmp_path):
        assert detect_panels(tmp_path / "nope.png") == []
