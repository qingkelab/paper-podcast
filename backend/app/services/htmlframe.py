"""HTML → PNG：用系统里的 Chrome 无头渲染画面。

## 为什么要走这条路

现在画面是「Python 手算坐标 → 拼 SVG → resvg 光栅化」。问题不在能不能画出来，
而在于**排版全得自己算**：文字换行、找不到位置时缩字号、卡片宽度、圆角、
强调行的行内留白…… `video.py` 三千多行里有相当一部分在做浏览器本来就会做的事。
实测一个等效的 1080p 整页（标题 + 配图卡 + 强调行 + 字幕带 + 步骤 + logo）
用 **45 行 HTML/CSS** 就够了，而且中文换行、字号自适应都是浏览器给的。

## 三档（决定「一页要渲染几帧」）

- **A 静态页**：一页只渲染 **1 帧**，静止时长交给 ffmpeg 重复帧。
  实测 936×1210 ≈ 56~90 ms/帧，31 页 ≈ 3 秒。
- **B 内容驱动动画页**：只对**动画窗口**逐帧抓取（例如 2 秒 = 60 帧 ≈ 5 秒/页），
  之后停在末帧。整片逐帧是**不划算**的：250 秒 × 30fps = 7500 帧 ≈ 15 分钟
  + 约 1.1 GB PNG，而现在整条管线只要 40~95 秒。
- **C 时间轴**：concat / 混音 / 时长对齐（视频长度 == 音频长度）仍然归 ffmpeg。

## 实测过的坑（别再试一遍）

- **必须 `--no-sandbox`**：默认沙箱在本机直接报
  `sandbox initialization failed: Operation not permitted`。
- **必须独立的 `--user-data-dir`**：跟正在运行的浏览器共用一个 profile 会失败。
- **`chrome --headless --screenshot` 写完 PNG 之后进程不退出**（看起来像挂死 15 秒）。
  所以这里走 CDP，不用那条 CLI。真要用 CLI 的话只有一招：轮询到文件出现再 kill。
- **CDP 的 `Emulation.setVirtualTimePolicy({policy:"pause", budget})` 会报
  `Can only specify budget for non-Pause policy`**，而且虚拟时间**不推进 CSS 动画**
  （实测进度条一直停在 0px）。动画要用 WAAPI seek，见 `ChromeSession.render_frames`。
- **Playwright 装浏览器在这台机器上超时**，别走它。

## 页面与渲染器的约定

- `window.prepare()`：可选的**异步**钩子，在「加载完 + 字体就绪 + 图片解码完」之后、
  第一次截图之前被 `await`。**字号自适应（量真实字形再定字号）走这里。**
  它必须真的被调用：曾经文档里写了约定、`_READY_SCRIPT` 里却没调，于是
  「字号自适应」和「标签别出画」一次都没执行过 —— 页面照样渲得出来、看着也正常，
  只是所有文字都停在 CSS 的最大字号上（长图注因此被 2 行硬截，而不是缩字号多放几个字）。
  现在有一条**像素级**测试盯着：`prepare()` 把方块搬到右边，截图上就必须在右边。
- `window.seek(seconds)`：可选的 JS 动画 seek 钩子（CSS/WAAPI 动画渲染器自己 seek）。

## 失败要能退回

`backend_name()` 是唯一的口子：Chrome 不在（或 `RENDER_BACKEND=svg`）就返回 `svg`，
调用方据此走回 resvg 那条老路。**不允许**因为「这台机器没装 Chrome」就整个出不了片。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

# 选哪条渲染路径。`svg` / `resvg` / `legacy` 强制走老路；其余值（含未设置）
# 表示「有 Chrome 就用 HTML」。认不出的取值也按自动判定处理 —— 拼错一个环境变量
# 不该让整条出片流程失败。
RENDER_BACKEND_ENV = "RENDER_BACKEND"
_HTML_ALIASES = {"html", "chrome", "chromium", ""}
_SVG_ALIASES = {"svg", "resvg", "legacy"}

# Chrome 的位置。macOS 的 .app 路径排前面（这台开发机的实际情况），
# 然后是 Linux 发行版常见的几个名字。
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
)
CHROME_PATH_ENV = "CHROME_PATH"

# 启动等待：从进程起到 `DevToolsActivePort` 出现。实测约 0.4~1.2 秒，
# 给到 20 秒是留给「第一次冷启动 + 磁盘慢」的情况。
LAUNCH_TIMEOUT_SEC = 20.0
# 单条 CDP 命令的超时（截图是最慢的一条，1080p 约 117ms，留足余量）
COMMAND_TIMEOUT_SEC = 30.0


class HtmlRenderError(RuntimeError):
    """HTML 渲染失败。

    单独一个类型（而不是复用 video.VideoError）是为了让 `video.py` 能**只**捕获
    这一类然后退回 SVG —— 别的异常还是要照常冒出去，不然真 bug 会被静默吞掉。
    """


def chrome_path() -> str | None:
    """找到可用的 Chrome/Chromium 可执行文件；找不到返回 None。"""
    override = (os.environ.get(CHROME_PATH_ENV) or "").strip()
    if override:
        return override if Path(override).exists() else None
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def chrome_available() -> bool:
    return chrome_path() is not None


def backend_name() -> str:
    """当前该用哪条渲染路径：`html` 或 `svg`。

    这是**唯一**的判据来源：环境变量说了算，但说了 `html` 而机器上没有 Chrome 时
    一律降级成 `svg`（并记一条日志）——「缺少 Chrome」不该让用户拿不到视频。
    """
    raw = (os.environ.get(RENDER_BACKEND_ENV) or "").strip().lower()
    if raw in _SVG_ALIASES:
        return "svg"
    if raw not in _HTML_ALIASES:
        logger.info("认不出的 %s=%r，按自动判定处理", RENDER_BACKEND_ENV, raw)
    if chrome_available():
        return "html"
    logger.info("没有找到 Chrome，画面仍然用 SVG + resvg 渲染")
    return "svg"


def html_rendering_enabled() -> bool:
    return backend_name() == "html"


@dataclass(frozen=True)
class Page:
    """一个待渲染的整页：一段 HTML + 它的画布尺寸。

    `html` 里引用图片一律用**绝对 `file://` 路径或 data URI**：页面本身会写成
    临时文件再 `Page.navigate` 过去（`file://` 页面加载 `file://` 图片是允许的，
    实测论文原图 PNG 能正常解码）。用 data URI 内联当然也行，但整页 HTML 会长很多。
    """

    html: str
    width: int
    height: int
    # 透明底。默认 False（不透明白）—— 整页就该是白底；**图层**（图片卡 /
    # 强调行 / 聚光灯）必须开成 True，否则每个图层都带一整块白，叠上去把下面盖住。
    transparent: bool = False


def _png_size(data: bytes) -> tuple[int, int]:
    """从 PNG 的 IHDR 块里读宽高（不引第三方库，测试也用得上）。"""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise HtmlRenderError("截图不是合法的 PNG")
    import struct

    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


class ChromeSession:
    """常驻的无头 Chrome：起一次，渲染很多帧。

    为什么必须常驻：一页起一个进程的话冷启动约 400ms/次；动画页要逐帧抓
    （60 帧）就变成 24 秒纯启动开销。常驻之后剩下的只有真正的渲染时间。

    用 `with ChromeSession() as session: ...` 打开；退出时**一定**要关掉
    （进程 + 一个临时 profile 目录），否则跑几次就攒下一堆僵尸 Chrome。
    """

    def __init__(self, *, executable: str, profile_dir: Path, process: subprocess.Popen, port: int, ws_url: str):
        self.executable = executable
        self.profile_dir = profile_dir
        self.process = process
        self.port = port
        self.ws_url = ws_url
        self._socket = None
        self._message_id = 0
        self._page_url: str | None = None
        self._page_file: Path | None = None

    # ---- 生命周期 ----

    @classmethod
    def start(cls, *, timeout: float = LAUNCH_TIMEOUT_SEC) -> "ChromeSession":
        executable = chrome_path()
        if executable is None:
            raise HtmlRenderError("没有找到 Chrome，无法渲染 HTML")

        profile_dir = Path(tempfile.mkdtemp(prefix="paper-podcast-chrome-"))
        crash_dir = profile_dir / "crash"
        crash_dir.mkdir(exist_ok=True)
        args = [
            executable,
            "--headless=new",
            # 本机不加这个起不来（sandbox initialization failed）。
            "--no-sandbox",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-crash-reporter",
            "--disable-background-networking",
            "--force-device-scale-factor=1",
            f"--crash-dumps-dir={crash_dir}",
            # 必须独立 profile：跟正在运行的浏览器共用一个会直接失败。
            f"--user-data-dir={profile_dir}",
            # 端口交给系统分配，实际端口从 DevToolsActivePort 里读 —— 写死端口
            # 会在「上一次的 Chrome 还没退干净」时撞车。
            "--remote-debugging-port=0",
            "about:blank",
        ]
        logger.debug("启动无头 Chrome：%s", " ".join(args))
        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # 单独一个进程组：Chrome 会 fork 出渲染/GPU 子进程，关的时候要一起收掉
            start_new_session=True,
        )
        try:
            port = _wait_for_port(profile_dir, process, timeout=timeout)
            ws_url = _page_websocket(port)
        except Exception:
            process.kill()
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise

        session = cls(
            executable=executable,
            profile_dir=profile_dir,
            process=process,
            port=port,
            ws_url=ws_url,
        )
        try:
            session._connect()
        except Exception:
            session.close()
            raise
        return session

    def _connect(self) -> None:
        from websockets.sync.client import connect

        self._socket = connect(self.ws_url, max_size=None, open_timeout=COMMAND_TIMEOUT_SEC)
        self._send("Page.enable")
        self._send("Runtime.enable")

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:  # noqa: BLE001 - 关连接失败不该盖住真正的错误
                pass
            self._socket = None
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        # 只 terminate 主进程是不够的：Chrome 的子进程还活着，会继续往 profile 里写东西，
        # 于是 rmtree 半路失败 —— 而它是 `ignore_errors=True`，**静默**留下一个临时目录。
        # 实测过：跑几次测试就攒下一堆 `paper-podcast-chrome-*`。
        self._kill_process_group()
        self._remove_profile()

    def _kill_process_group(self) -> None:
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass  # 进程已经没了 / 权限不够，都不该让 close 抛异常

    def _remove_profile(self) -> None:
        for attempt in range(5):
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            if not self.profile_dir.exists():
                return
            time.sleep(0.1 * (attempt + 1))
        logger.warning("Chrome 的临时 profile 没删干净：%s", self.profile_dir)

    def __enter__(self) -> "ChromeSession":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - 只给日志看
        return f"<ChromeSession port={self.port} alive={self.process.poll() is None}>"

    # ---- CDP ----

    def _send(self, method: str, params: dict | None = None, *, timeout: float = COMMAND_TIMEOUT_SEC):
        if self._socket is None:
            raise HtmlRenderError("Chrome 会话已经关掉了")
        self._message_id += 1
        message_id = self._message_id
        payload = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        self._socket.send(json.dumps(payload))

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HtmlRenderError(f"CDP 命令超时：{method}")
            try:
                raw = self._socket.recv(timeout=remaining)
            except TimeoutError as exc:
                raise HtmlRenderError(f"CDP 命令超时：{method}") from exc
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue  # 事件通知里有二进制/非 JSON 帧，跳过
            if message.get("id") != message_id:
                continue  # 别的事件（loadEventFired 之类），继续等我们那条的回复
            if "error" in message:
                raise HtmlRenderError(f"CDP {method} 失败：{message['error']}")
            return message.get("result") or {}

    def _evaluate(self, expression: str, *, await_promise: bool = False):
        """在页面里跑一段 JS。

        `await_promise=True` 用来等「图片解码 + 字体就绪」这类异步条件 ——
        不等它们就截图会拿到一张字体还是 fallback 的帧。
        """
        result = self._send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        )
        if result.get("exceptionDetails"):
            raise HtmlRenderError(
                f"页面脚本出错：{result['exceptionDetails'].get('text')}"
            )
        return (result.get("result") or {}).get("value")

    # ---- 渲染 ----

    def open_page(self, page: Page, *, work_dir: Path | None = None) -> None:
        """把这一页写进临时文件并导航过去，然后等它真的画完。

        为什么写文件而不是 `Page.setDocumentContent`：后者的文档源是 `about:blank`，
        从那里加载 `file://` 图片会被拦掉（跨源）。写文件再用 `file://` 导航，
        同源，图片和字体都正常。
        """
        target_dir = work_dir or self.profile_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        page_file = target_dir / "page.html"
        page_file.write_text(page.html, encoding="utf-8")
        self._page_file = page_file
        self._page_url = page_file.as_uri()

        self._send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": page.width,
                "height": page.height,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        self._send("Page.navigate", {"url": self._page_url})
        self._apply_background(page.transparent)
        self._evaluate(_READY_SCRIPT, await_promise=True)

    def _apply_background(self, transparent: bool) -> None:
        """要不要透明底。

        默认 Chrome 给的是**不透明白底**，所以「图片卡 / 强调行 / 聚光灯」这几个
        要叠在别的层上面的图层必须显式把它清成透明，否则每个图层都会多带一整块白 ——
        叠上去就是把下面那层整块盖住（而且看不出来是白的还是本来就是白的）。
        """
        if transparent:
            self._send(
                "Emulation.setDefaultBackgroundColorOverride",
                {"color": {"r": 0, "g": 0, "b": 0, "a": 0}},
            )
        else:
            self._send("Emulation.setDefaultBackgroundColorOverride", {})

    def capture(self, output_path: Path) -> Path:
        """截当前画面。"""
        result = self._send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        data = result.get("data")
        if not data:
            raise HtmlRenderError("截图返回空内容")
        raw = base64.b64decode(data)
        if not raw:
            raise HtmlRenderError("截图返回空内容")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(raw)
        return output_path

    def render(self, page: Page, output_path: Path, *, work_dir: Path | None = None) -> Path:
        """渲染**一帧**（Tier A：静态页只需要这一帧）。"""
        self.open_page(page, work_dir=work_dir)
        return self.capture(output_path)

    def render_frames(
        self,
        page: Page,
        times: Sequence[float],
        output_dir: Path,
        *,
        prefix: str = "frame",
        work_dir: Path | None = None,
    ) -> list[Path]:
        """按给定时间点逐帧渲染（Tier B：内容驱动动画）。

        `times` 是**秒**（相对这一页动画的起点），内部换算成毫秒交给 WAAPI。
        做法是把页面上所有动画暂停后 seek 到指定时刻，再截一张 ——
        这样抓帧是**确定性**的，跟机器快慢无关（实测小圆点位置
        60→260→460→660→847 px 与理论值 13.3 px/帧完全一致）。

        页面里如果有 JS 动画（不是 CSS/WAAPI 的），约定同名的 `window.seek(t)`
        （秒）由它自己实现；我们不做别的假设。
        """
        self.open_page(page, work_dir=work_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for index, moment in enumerate(times):
            self.seek(moment)
            paths.append(self.capture(output_dir / f"{prefix}-{index:04d}.png"))
        return paths

    def seek(self, seconds: float) -> int:
        """把页面上的动画 seek 到 `seconds`，并等这一帧真的画出来。

        返回被 seek 的动画数量（调用方可以用它判断「这一页到底有没有动画」）。
        """
        milliseconds = json.dumps(round(seconds * 1000.0, 3))
        count = self._evaluate(
            "(() => {"
            f"  const t = {milliseconds};"
            "  const anims = document.getAnimations ? document.getAnimations() : [];"
            "  anims.forEach(a => { try { a.pause(); a.currentTime = t; } catch (e) {} });"
            "  if (typeof window.seek === 'function') { try { window.seek(t / 1000); } catch (e) {} }"
            "  return anims.length;"
            "})()"
        )
        # 等两帧：一帧让样式生效、一帧让合成器跟上。实测少这一等会截到**上一帧**。
        self._evaluate(
            "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(1))))",
            await_promise=True,
        )
        return int(count or 0)


# 导航之后要等的东西：DOM 完成 + 所有图片解码完 + 字体就绪。
#
# 只等 `readyState === 'complete'` 是不够的：图片可能还没 decode、自定义字体
# 可能还在加载，这时候截图会拿到**字体还是 fallback 的那一版**（中文尤其明显）。
_READY_SCRIPT = """
(async () => {
  if (document.readyState !== 'complete') {
    await new Promise(r => window.addEventListener('load', r, { once: true }));
  }
  await Promise.all(Array.from(document.images).map(img => img.decode().catch(() => {})));
  if (document.fonts && document.fonts.ready) { await document.fonts.ready; }
  // 页面约定的收尾钩子：字号自适应在这里做（必须等字体就绪，否则量出来的宽度是错的）
  if (typeof window.prepare === 'function') { await window.prepare(); }
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  return true;
})()
"""


def _wait_for_port(profile_dir: Path, process: subprocess.Popen, *, timeout: float) -> int:
    """等 Chrome 把实际监听端口写进 `DevToolsActivePort`。"""
    port_file = profile_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise HtmlRenderError(f"Chrome 启动即退出（退出码 {process.returncode}）")
        if port_file.exists():
            try:
                first = port_file.read_text(encoding="utf-8").splitlines()[0].strip()
            except (OSError, IndexError):
                first = ""
            if first.isdigit() and int(first) > 0:
                return int(first)
        time.sleep(0.05)
    raise HtmlRenderError(f"等了 {timeout:.0f} 秒也没等到 Chrome 的调试端口")


def _http_json(url: str, timeout: float = COMMAND_TIMEOUT_SEC):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _page_websocket(port: int, *, timeout: float = COMMAND_TIMEOUT_SEC) -> str:
    """拿到页面目标的 WebSocket 地址。

    直接连**页面**（不是浏览器目标）省掉一层 `sessionId` 转接 —— 我们只需要
    一个标签页。
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            targets = _http_json(f"http://127.0.0.1:{port}/json/list")
        except (urllib.error.URLError, ValueError, OSError) as exc:
            last_error = exc
            time.sleep(0.1)
            continue
        for target in targets or []:
            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                return str(target["webSocketDebuggerUrl"])
        time.sleep(0.1)
    raise HtmlRenderError(f"没有找到可用的页面目标：{last_error}")


def render_page(
    page: Page,
    output_path: Path,
    *,
    work_dir: Path | None = None,
) -> Path:
    """起一个临时会话渲染一页（**只用于一次性调用**，批量请自己开 `ChromeSession`）。

    每次调用都要付一次 Chrome 启动（约 0.4~1.2 秒），所以出片流程里不要用它 ——
    那是 `ChromeSession` 的活。
    """
    with ChromeSession.start() as session:
        return session.render(page, output_path, work_dir=work_dir)
