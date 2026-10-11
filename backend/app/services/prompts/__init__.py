"""Prompt 工程：论文解读 + 双人播客脚本 + 视频文案（封面标题 / 强调行 / 逐段选图）。

**这是一个包，不是一个大文件**：按「一次只改一件事」切开，改文案不用读全文件 ——

| 模块 | 管什么 |
|---|---|
| `units` | 语速、字数预算、时长模型、输出语言规则（所有 prompt 的长度口径都从这里取） |
| `analysis` | 第一步：论文解读 |
| `script` | 第二步：双人脚本（难度档、费曼技巧、作者视角） |
| `cover` | 封面爆款标题 + 清洗（`normalize_hook`） |
| `points` | 画面强调行 + 子图聚光灯选择 + 本地兜底 |
| `assign` | 逐段选图 + 配图理由 + 启发式兜底 |
| `title` | 从正文认标题 |
| `repair` | 长度修补（扩写 / 压缩） |

设计原则（从前的文件头注释，保留在下面）：

1. **两段式生成**。先出结构化解读（JSON），再基于解读写脚本。分两步的原因：
   一次性输出「解读+脚本」时模型会把注意力放在文风上，解读深度明显下降。
2. **强制 JSON**。解读与脚本都用 JSON，前端才能渲染成卡片、避免解析自由文本被变体搞崩。
3. **反 AI 腔**。负面清单显式禁止「首先…其次…」「综上所述」这类书面语。
4. **长度可控**。字数预算按实测语速反推（`units.py`），并写进 prompt。
5. **视频文案也在这里**（`cover` / `points` / `assign`）。它们原来是 `video.py` 里的
   一大段，搬过来是因为「改文案」和「改渲染」根本是两件事 ——
   前者天天改、后者一个月动一次，混在一个 3600 行的文件里每次都得读全部。
"""

from __future__ import annotations

from .analysis import *          # noqa: F401,F403
from .assign import *            # noqa: F401,F403
from .cover import *             # noqa: F401,F403
from .points import *            # noqa: F401,F403
from .repair import *            # noqa: F401,F403
from .script import *            # noqa: F401,F403
from .title import *             # noqa: F401,F403
from .units import *             # noqa: F401,F403
