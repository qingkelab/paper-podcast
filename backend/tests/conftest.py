"""测试默认走 **SVG** 渲染路径。

理由有两条，都不是「懒得测 HTML」：

1. **速度**：HTML 路径每出一条片要多起一次 Chrome（实测约 1.9 秒）、多渲染约 90 张图层页。
   而绝大多数测试要验证的是时间轴、文案、复用规则、长度对齐 ——
   跟「用哪个渲染器画出来」无关，不该为此慢一倍。
2. **确定性**：`RENDER_BACKEND` 不固定的话，同一套测试在「装了 Chrome 的机器」和
   「没装的机器」上跑的是两条代码路径。

真正关心 HTML 的测试自己把它打开（`test_htmlpage.py` 里那些），
用 `monkeypatch.setenv` —— 它在这个 autouse fixture 之后执行，所以能覆盖。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _render_with_svg_by_default(monkeypatch):
    monkeypatch.setenv("RENDER_BACKEND", "svg")
