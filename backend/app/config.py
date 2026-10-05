"""应用配置。

所有密钥只从环境变量 / .env 读取，绝不硬编码，也绝不下发给前端。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> backend/ -> 项目根
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 基础 ----
    app_name: str = "论文解读 AI 播客"
    version: str = "0.1.0"
    host: str = "127.0.0.1"
    port: int = 8000

    # 允许的前端来源，逗号分隔；"*" 表示不限制（仅本地开发用）
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    # ---- 存储 ----
    data_dir: Path = PROJECT_ROOT / "data"
    database_path: Path | None = None  # 默认为 data_dir / "app.db"

    # ---- 文本解读 / 脚本生成的大模型提供方 ----
    # "auto" = 谁配了密钥就用谁（deepseek 优先）；也可显式指定 "deepseek"/"doubao"
    llm_provider: str = "auto"

    # DeepSeek（OpenAI 兼容接口）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_sec: float = 180.0

    # ---- 豆包方舟大模型（文本解读 / 脚本生成）----
    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    # 可以是接入点 ID（ep-xxxx）或模型 ID（如 doubao-seed-1-6-250615）
    ark_model: str = ""
    ark_timeout_sec: float = 120.0

    # ---- 豆包语音播客（音频合成）----
    # API Key 鉴权（新账号推荐）
    doubao_api_key: str = ""
    # 或 AK/SK 鉴权（老账号）
    doubao_app_id: str = ""
    doubao_app_key: str = "aGjiRDfUWi"  # 播客接口的固定默认值
    doubao_access_key: str = ""

    podcast_ws_url: str = "wss://openspeech.bytedance.com/api/v3/sami/podcasttts"
    podcast_resource_id: str = "volc.service_type.10050"
    podcast_timeout_sec: float = 600.0

    # ---- 默认音色 ----
    default_voice_a: str = "zh_male_dayixiansheng_v2_saturn_bigtts"
    default_voice_b: str = "zh_female_mizaitongxue_v2_saturn_bigtts"

    # 播客合成语速。范围 -50(0.5x) ~ 100(2.0x)，0 为音色标准语速。
    # 这几个音色在 0 档实测约 350 字/分钟，对中文听众偏快（人声叙述通常 240~280），
    # 觉得赶的话调到 -20 ~ -30 会舒服很多。字数预算会随语速自动联动。
    podcast_speech_rate: int = 0

    # ---- 行为开关 ----
    # 强制走 Mock（即使有密钥）。留空表示按密钥是否齐全自动判断。
    force_mock: bool = False
    # worker 并发处理的单集数量（串行处理，避免触发 API 限流）
    worker_concurrency: int = 1
    # 单篇论文送入模型的文本上限（字符）
    max_paper_chars: int = 20000
    # 失败自动重试次数
    max_retries: int = 2

    # 是否在音频合成后继续合成视频解读播客（需要系统安装 ffmpeg）
    enable_video: bool = True

    # 视频里「没有对应论文原图」的段落，最多现场生成几张专门配图。
    # 每张一次模型调用（实测约 10 秒 / 3000+ token），调大要留意成本与耗时。
    max_topic_images: int = 4

    # ---------- 派生属性 ----------

    @property
    def db_path(self) -> Path:
        return self.database_path or (self.data_dir / "app.db")

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def cover_dir(self) -> Path:
        return self.data_dir / "covers"

    @property
    def figure_dir(self) -> Path:
        return self.data_dir / "figures"

    @property
    def illustration_dir(self) -> Path:
        return self.data_dir / "illustrations"

    @property
    def video_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def video_work_dir(self) -> Path:
        return self.data_dir / "video-frames"

    @property
    def cors_origin_list(self) -> list[str]:
        raw = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return raw or ["http://127.0.0.1:5173"]

    @property
    def llm_mode(self) -> str:
        """文本解读走哪个提供方：'deepseek' | 'doubao' | 'mock'。"""
        if self.force_mock:
            return "mock"

        provider = (self.llm_provider or "auto").strip().lower()
        if provider == "deepseek":
            return "deepseek" if self.deepseek_api_key else "mock"
        if provider == "doubao":
            return "doubao" if (self.ark_api_key and self.ark_model) else "mock"

        # auto：DeepSeek 优先，其次方舟
        if self.deepseek_api_key:
            return "deepseek"
        if self.ark_api_key and self.ark_model:
            return "doubao"
        return "mock"

    @property
    def llm_credentials(self) -> tuple[str, str, str, float]:
        """返回当前提供方的 (base_url, api_key, model, timeout)。"""
        if self.llm_mode == "deepseek":
            return (
                self.deepseek_base_url,
                self.deepseek_api_key,
                self.deepseek_model,
                self.deepseek_timeout_sec,
            )
        if self.llm_mode == "doubao":
            return (
                self.ark_base_url,
                self.ark_api_key,
                self.ark_model,
                self.ark_timeout_sec,
            )
        return ("", "", "", self.deepseek_timeout_sec)

    @property
    def tts_mode(self) -> str:
        """音频合成走真实接口还是 Mock。"""
        if self.force_mock:
            return "mock"
        has_auth = bool(self.doubao_api_key) or bool(
            self.doubao_access_key and self.doubao_app_id
        )
        return "doubao" if has_auth else "mock"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.audio_dir,
            self.upload_dir,
            self.cover_dir,
            self.figure_dir,
            self.illustration_dir,
            self.video_dir,
            self.video_work_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
