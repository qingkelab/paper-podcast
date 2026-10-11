"""HTTP 接口层（包）。字段与 docs/API.md 严格一致。

原来这是一个 2144 行的 `routes.py`，里面压着好几件互不相干的事。现在按
「想改什么就开哪个文件」拆开 —— 每个模块的 docstring 第一行就写着它管什么：

| 想改什么 | 打开 |
|---|---|
| 系统与公开信息（/health /options /usage /showcase） | `system.py` |
| 单集的建（PDF/链接/粘贴/批量）、列表、详情、删除、重试 | `episodes.py` |
| 账号与会话（/auth/*） | `auth.py` |
| 个人专辑（/albums/*） | `albums.py` |
| 分享开关 + 公开视图与公开资源（/share/*） | `share.py` |
| 产物读取：音频/视频/静帧/封面/论文原图/信息图/下载 | `media.py` |
| 封面标题（PATCH /cover）与重新合成视频 | `cover.py` |
| **被两个以上模块共用的东西**（router、依赖、序列化、鉴权归属、配额） | `shared.py` |

⚠️ **下面这几行 import 的顺序不能随便调**：FastAPI 按注册顺序匹配路由，
这一串就是原 `routes.py` 里各模块**第一个接口**出现的先后 —— 换了顺序等于
换了路由匹配优先级。新增模块请插在它「第一个接口」原来所在的位置；
模块内部的接口也按原顺序排列（例如 `episodes.py` 里 `/episodes/batch` 必须先于
`/episodes/{episode_id}` 注册）。

⚠️ 顺带记一笔：按模块 import 会把**同一个模块的接口聚在一起**，所以 `router.routes`
的整体顺序与拆分前那个单文件不同（各模块内部一字未变）。拆分前后核对过两件事：
① 能匹配同一 URL 的模板对只有 `/episodes/batch` 与 `/episodes/{episode_id}`，
两者在同一个模块里、相对顺序原样保留，且方法不重叠；② 同一路径由两个模块分别
注册的只有 `/episodes/{id}/cover`（GET 在 `media.py`、PATCH 在 `cover.py`），
方法不重叠 —— 所以顺序变化不影响任何请求的匹配结果（OpenAPI schema 也逐字节一致）。

`app/main.py` 仍然只写 `from .api.routes import router`，一个字都不用改。
"""

from __future__ import annotations

# 顺序 = 原 routes.py 里各模块第一个接口出现的顺序（改路由优先级，别动）
from . import system  # /health(原第 1 条) /options /usage /showcase
from . import episodes  # POST /episodes(原第 3 条)，含批量与列表/详情/删除/重试
from . import auth  # /auth/*(原第 5 条)
from . import albums  # /albums/*(原第 10 条)
from . import share  # /episodes/{id}/share*(原第 17 条) + /share/* 公开通道
from . import media  # /episodes/{id}/audio(原第 35 条) 及其余产物读取
from . import cover  # /video/rebuild(原第 36 条) + PATCH /cover + 静帧

from .shared import router

__all__ = ["router"]
