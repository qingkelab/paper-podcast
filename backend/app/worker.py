"""异步任务队列。

论文解读 + 音频合成耗时 1-3 分钟，不能阻塞 HTTP 请求，所以：
- 接口只负责入库并投递任务 ID，立刻返回；
- 后台 asyncio worker 串行消费（串行是为了避免触发豆包 API 的并发限流）；
- 失败按 settings.max_retries 自动重试，仍失败则标记 failed 并把原因写进 error 字段，
  前端可以点「重新生成」再入队。
"""

from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .db import Database
from .services.pipeline import Pipeline

logger = logging.getLogger(__name__)


class TaskQueue:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.pipeline = Pipeline(settings, db)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._pending: set[str] = set()
        self._running = False

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for index in range(max(self.settings.worker_concurrency, 1)):
            self._workers.append(asyncio.create_task(self._worker(index)))
        logger.info("任务队列已启动（并发 %d）", len(self._workers))

        # 进程重启后，把中断在中间状态的任务重新排队
        await self._requeue_interrupted()

    async def stop(self) -> None:
        self._running = False
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._workers.clear()

    async def _requeue_interrupted(self) -> None:
        """上次进程退出的遗留任务：状态停在中间阶段的一律重新入队。"""
        interrupted = ("parsing", "analyzing", "scripting", "synthesizing")
        for status in interrupted:
            items, _ = self.db.list_episodes(status=status, limit=100)
            for item in items:
                logger.info("恢复中断任务 %s（原状态 %s）", item["id"], status)
                self.db.update_episode(
                    item["id"], status="queued", stage_label="排队中", progress=0, error=None
                )
                await self.submit(item["id"])

    # ---------- 投递 ----------

    async def submit(self, episode_id: str) -> None:
        if episode_id in self._pending:
            return
        self._pending.add(episode_id)
        await self._queue.put(episode_id)
        logger.info("任务 %s 已入队（当前排队 %d）", episode_id, self._queue.qsize())

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # ---------- 消费 ----------

    async def _worker(self, index: int) -> None:
        while self._running:
            try:
                episode_id = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                await self._process_with_retry(episode_id)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("worker %d 处理任务 %s 时崩溃", index, episode_id)
            finally:
                self._pending.discard(episode_id)
                self._queue.task_done()

    async def _process_with_retry(self, episode_id: str) -> None:
        max_attempts = max(self.settings.max_retries, 0) + 1

        for attempt in range(1, max_attempts + 1):
            episode = self.db.get_episode(episode_id)
            if not episode:
                return
            if episode["status"] == "completed":
                return

            if attempt > 1:
                logger.info("任务 %s 第 %d 次重试", episode_id, attempt)
                self.db.update_episode(
                    episode_id,
                    status="queued",
                    stage_label=f"重试中（第 {attempt} 次）",
                    progress=0,
                    error=None,
                )

            ok = await self.pipeline.run(episode_id)
            if ok:
                return

            self.db.update_episode(episode_id, retry_count=attempt)

            # 内容本身的问题（论文解析不了、模型拒绝）重试没意义，直接放弃
            current = self.db.get_episode(episode_id)
            error = (current or {}).get("error") or ""
            if _is_permanent(error):
                logger.info("任务 %s 属于不可重试错误，停止重试", episode_id)
                return

            if attempt < max_attempts:
                await asyncio.sleep(3 * attempt)  # 退避，避免连续撞限流


_PERMANENT_MARKERS = (
    "无法解析",
    "提取不到",
    "已加密",
    "重新上传",
    "过短",
    "鉴权失败",
    "接入点不存在",
    "需要登录",
    "不支持",
    # 余额不足（402）：重试一百次也还是没钱。
    # 实测漏了这条的代价：一次生成被完整重跑 3 遍 —— 每遍都重新下载 PDF、
    # 重新提取配图、重新调模型，最后拿到的还是同一个 402。白烧时间和带宽。
    # 注意**不要**把限流（429）加进来：那是真能靠退避等过去的。
    "余额不足",
    "402",
)


def _is_permanent(error: str) -> bool:
    """判断错误是否属于「重试也不会好」的类别。"""
    return any(marker in error for marker in _PERMANENT_MARKERS)
