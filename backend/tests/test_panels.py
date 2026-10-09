"""多子图切分：几何必须来自像素，不能来自模型的猜测。

实测背景（为什么这个文件存在）：让模型直接给「这一段在讲图里的哪一块」的坐标
是行不通的 —— 它看不到图。所以分工改成「几何由像素定、语义（第几个子图）由模型定」。
这个文件盯着「几何」这一半。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.panels import (
    _split_by_marks,
    detect_panels,
    panel_count_from_caption,
    panel_labels,
)


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


class TestPanelLabels:
    """子图标注 → 「这一块讲什么」。模型靠它挑得动「第几个子图」。"""

    def test_splits_caption_into_lettered_parts(self):
        caption = (
            "Figure 2: Spatial structure. (a) Key rows are ranked by readout impact. "
            "(b) A representative state exhibits outliers. (c) Channel RMS relative to the median."
        )
        labels = panel_labels(caption)
        assert [letter for letter, _ in labels] == ["a", "b", "c"]
        assert labels[0][1] == "Key rows are ranked by readout impact"
        assert labels[2][1].startswith("Channel RMS")

    def test_needs_at_least_two(self):
        assert panel_labels("Figure 1: only (a) one panel") == []

    def test_last_part_runs_to_the_end(self):
        labels = panel_labels("(a) first (b) second part has no trailing mark")
        assert labels[-1][1] == "second part has no trailing mark"


class TestPanelMarks:
    """图上的 `(a)(b)(c)` 标注位置能把搜索范围收窄到「两标注之间那条缝」。

    实测这一条是必需的：Figure 1 是三个子图横排，但每两块之间夹着下一块的纵轴刻度，
    中间带里没有一列是空的，纯投影切不出 3 块；给了标注位置就一找一个准。
    """

    def _three_columns_with_busy_gaps(self, path: Path) -> Path:
        """三块子图，缝里塞满「刻度文字」（模拟真实论文图）。

        每块是一条竖直色带，缝里有零散的小点 —— 那些点让「严格空白」判据失效，
        但缝的墨量仍然远低于子图本体。
        """
        import pymupdf

        size = 600
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, size, size), False)
        pix.set_rect(pix.irect, (255, 255, 255))
        for index in range(3):
            x0 = 60 + index * 180
            pix.set_rect(pymupdf.IRect(x0, 60, x0 + 120, 540), (30, 60, 120))
        # 缝里的小点：不构成「空白」，但比子图淡得多
        for index in range(2):
            x = 60 + index * 180 + 130
            for y in range(70, 540, 60):
                pix.set_rect(pymupdf.IRect(x, y, x + 3, y + 4), (200, 200, 200))
        pix.save(str(path))
        return path

    def test_marks_give_the_boundaries(self, tmp_path):
        image = self._three_columns_with_busy_gaps(tmp_path / "three.png")
        # 标注在每块子图左下角
        marks = [(0.10, 0.90), (0.40, 0.90), (0.70, 0.90)]

        panels = detect_panels(image, expected=3, marks=marks)
        assert len(panels) == 3
        for panel in panels:
            assert panel.w < 0.35
        assert panels[0].x < panels[1].x < panels[2].x

    def test_marks_are_ignored_when_count_disagrees(self, tmp_path):
        """标注个数和图注说的个数对不上 → 不按标注切（宁可退回纯几何）。"""
        image = self._three_columns_with_busy_gaps(tmp_path / "three2.png")
        panels = detect_panels(image, expected=3, marks=[(0.1, 0.9), (0.4, 0.9)])
        assert len(panels) != 2

    def test_grid_layout_marks_are_not_used(self, tmp_path):
        """2×2 网格里 (c) 会回到第一列，按顺序映射会指错块 → 这条路直接放弃。"""
        image = grid_png(tmp_path / "grid.png", 2, 2)
        marks = [(0.1, 0.1), (0.6, 0.1), (0.1, 0.6), (0.6, 0.6)]
        panels = detect_panels(image, expected=4, marks=marks)
        assert len(panels) == 4  # 走的是纯几何那条路

    def test_panels_are_trimmed_to_content(self, tmp_path):
        """带大片白边的图要被收到内容上，否则聚光灯等于没压暗多少。"""
        import pymupdf

        path = tmp_path / "margin.png"
        size = 600
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, size, size), False)
        pix.set_rect(pix.irect, (255, 255, 255))
        pix.set_rect(pymupdf.IRect(100, 100, 250, 500), (30, 60, 120))
        pix.set_rect(pymupdf.IRect(350, 100, 500, 500), (30, 60, 120))
        pix.save(str(path))

        panels = detect_panels(path, expected=2)
        assert len(panels) == 2
        # 左边那块的内容从 100/600=0.167 才开始，收过之后应该贴上去
        assert panels[0].x < 0.2, panels[0]
        assert panels[1].x > 0.55, panels[1]


class TestSplitByMarks:
    """直接盯几何：边界要落在「两个标注之间那条缝」上，而不是等分。

    这块专门造一组**宽度悬殊**的子图：等分（每块 1/3）会切进内容中间，
    而正确的边界在 0.575 / 0.725 附近。这样测试才真的在测「按标注切」。
    """

    def _grid(self, *, cols: int = 120, rows: int = 60):
        grid = [[False] * cols for _ in range(rows)]
        for x0, x1 in ((6, 66), (72, 84), (90, 114)):
            for row in grid:
                for col in range(x0, x1):
                    row[col] = True
        return grid, cols, rows

    def test_cuts_at_the_gap_between_marks_not_evenly(self):
        grid, cols, rows = self._grid()
        marks = [(0.05, 0.9), (0.60, 0.9), (0.75, 0.9)]
        boxes = _split_by_marks(grid, cols, rows, marks, 3)
        assert len(boxes) == 3
        xs = [(box[0] / cols, box[2] / cols) for box in boxes]
        # 缝隙在 66~72（=0.55~0.60）和 84~90（=0.70~0.75）
        assert 0.54 <= xs[0][1] <= 0.62, xs
        assert 0.69 <= xs[1][1] <= 0.77, xs
        # 等分的话边界会在 0.33 / 0.67，明显不是这个答案
        assert abs(xs[0][1] - 1 / 3) > 0.1

    def test_refuses_when_marks_are_not_in_one_row_or_column(self):
        grid, cols, rows = self._grid()
        marks = [(0.05, 0.1), (0.60, 0.1), (0.05, 0.6), (0.60, 0.6)]
        assert _split_by_marks(grid, cols, rows, marks, 4) == []

    def test_refuses_when_no_gap_between_marks(self):
        """两块之间全是墨（真的粘在一起）→ 不给切，别硬分。"""
        grid = [[True] * 120 for _ in range(60)]
        assert _split_by_marks(grid, 120, 60, [(0.05, 0.9), (0.95, 0.9)], 2) == []

    def test_column_layout_splits_horizontally(self):
        grid = [[False] * 60 for _ in range(120)]
        for col in range(60):
            for row in list(range(6, 66)) + list(range(72, 84)):
                grid[row][col] = True
        boxes = _split_by_marks(grid, 60, 120, [(0.1, 0.05), (0.1, 0.60)], 2)
        assert len(boxes) == 2
        assert boxes[0][3] / 120 <= 0.62
        assert boxes[1][1] / 120 >= 0.54
