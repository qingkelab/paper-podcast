# 项目架构

本文记录当前系统边界与演进约定。这里是架构地图，不替代接口字段定义；前后端契约仍以 [API.md](API.md) 为准。

## 背景与约束

- 产品把论文转换为双人播客，流程包括导入、解读、脚本、语音、视频和资源分享。
- 当前是单机优先的模块化单体：FastAPI、SQLite、Vue 3；生成任务在进程内串行排队。
- 豆包并发限制、可离线演示、现有数据库兼容和用户数据归属，优先于抽象层或服务拆分。
- 视频渲染是高复杂度领域，遵循 `AGENTS.md` 的分层，不把它并入通用业务服务。

## 领域词汇

| 概念 | 现有表达 | 边界 |
|---|---|---|
| 单集 | `episode` / `episodes` | 用户提交的一篇论文及其生成状态、元数据与产物 |
| 语言版本 | `versions[lang]` | 单集内一种语言的解读、脚本、音频、视频和信息图 |
| 专辑 | `album` | 用户维护的单集集合；删除专辑不删除单集 |
| 分享 | `share_token` / `visibility` | 单集的公开访问能力，不等同于登录用户的所有权 |
| 生成任务 | `TaskQueue` / `Pipeline` | 任务队列管理执行；Pipeline 编排单集生成步骤 |
| 产物 | 音频、视频、封面、配图、信息图 | 数据库存元数据和路径，文件保存在数据目录 |

## 当前模块边界

```text
Vue views/components
        │ HTTP（docs/API.md）
FastAPI routes ── auth / ownership / response mapping
        │
TaskQueue ── Pipeline ── ingest / LLM / TTS / figures / illustration / video
        │                                      │
      Database (SQLite)                 data files / ffmpeg
```

- `api/routes/`：HTTP 校验、状态码、鉴权入口与响应映射。接口模块之间不互相导入；通用且至少两个模块使用的工具放 `shared.py`。
- `worker.py`：排队、恢复中断任务和重试策略。它不负责 HTTP 请求或渲染细节。
- `services/pipeline.py`：单集生成的流程编排；具体领域能力保留在对应服务中。
- `db.py`：SQLite schema、迁移、查询与持久化。API 响应模型仍在 `models.py`，不可把数据库行直接当公开契约。
- `services/video_*.py`、`design.py`、`motion.py`：视频编排、画面渲染、编码和动效按已有单向依赖维护，兼容再导出规则见 `AGENTS.md`。
- `frontend/src/api/`：真实 API 与浏览器 Mock 的边界；视图通过 API 层消费数据，不自行拼接后端内部字段。

## 资源生命周期

`create_app()` 只组装 FastAPI、配置、中间件和路由，不创建运行期外部资源。数据库连接、后台队列与登录限流器在 FastAPI lifespan 中建立；退出时停止 worker 并关闭数据库。这样导入模块不会创建数据库文件，测试和服务进程也使用同一套启动/回收路径。

数据库 schema 演进仍使用 `db.py` 的幂等加列迁移。增加字段时同步更新序列化、`docs/API.md`（若对外可见）和对应数据路径；列表响应不得携带分析、脚本、版本等大字段。

## 演进策略

1. 维持模块化单体，不因「分层完整」而增加 DAO/service/mapper 空壳。
2. 一次迁移一个垂直切片；先让一个真实需求的规则归属清晰，再推广相同模式。
3. 新的持久队列、对象存储或多实例限流，只有在多进程/多实例部署成为当前需求时再替换。替换时保留 `TaskQueue`、存储服务等调用边界，避免把基础设施细节扩散到 routes 和前端。
4. 每次边界调整都按 `AGENTS.md` 的验证命令检查后端、前端类型、纯逻辑和构建；涉及接口时先更新 `docs/API.md`。

## 本次架构调整

将 SQLite 连接从模块构造期移到 ASGI lifespan。变化可回滚到 `create_app()` 中创建数据库的单行实现；无需改数据库 schema、接口契约或前端调用方式。
