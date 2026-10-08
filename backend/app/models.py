"""API 请求 / 响应模型。字段与 docs/API.md 严格一致。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

SourceType = Literal["pdf", "url", "text"]
Level = Literal["intro", "advanced", "expert"]
Language = Literal["zh", "en"]
Status = Literal[
    "queued", "parsing", "analyzing", "scripting", "synthesizing", "completed", "failed"
]


class EpisodeOptions(BaseModel):
    duration_min: int = 5
    level: Level = "intro"
    voice_a: str = ""
    voice_b: str = ""
    # 主语言：详情页默认展示、`?lang=` 缺省时取用的那一版
    language: Language = "zh"
    # 这一集实际产出哪些语言版本（含主语言）。双语会让 TTS 成本与耗时翻倍，
    # 所以由服务端配置决定，不由前端逐集勾选。
    languages: list[Language] = Field(default_factory=list)

    @field_validator("duration_min")
    @classmethod
    def _check_duration(cls, v: int) -> int:
        if v not in (3, 5, 10):
            raise ValueError("duration_min 只支持 3 / 5 / 10")
        return v


class PaperMeta(BaseModel):
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    abstract: str | None = None
    year: int | None = None
    venue: str | None = None
    arxiv_id: str | None = None
    keywords: list[str] = Field(default_factory=list)


class Analysis(BaseModel):
    background: str = ""
    innovations: list[str] = Field(default_factory=list)
    method: str = ""
    experiments: str = ""
    conclusion: str = ""
    limitations: list[str] = Field(default_factory=list)
    value: str = ""
    future: list[str] = Field(default_factory=list)


class ScriptSegment(BaseModel):
    speaker: Literal["A", "B"]
    text: str
    round: int = 0
    # "intro" / "outro" 表示这是社区品牌话术（不是论文正文），
    # 视频层据此把片尾渲染成品牌卡片
    brand: Literal["intro", "outro"] | None = None


class Script(BaseModel):
    segments: list[ScriptSegment] = Field(default_factory=list)
    word_count: int = 0
    est_duration_sec: int = 0


class Figure(BaseModel):
    """从论文 PDF 里提取的一张图或表。"""

    id: str
    kind: Literal["figure", "table"]
    label: str          # "Figure 1"
    caption: str = ""
    page: int = 1
    url: str
    width: int
    height: int


class VideoInfo(BaseModel):
    """合成好的视频解读播客。"""

    url: str
    duration_sec: float | None = None
    scene_count: int | None = None
    bytes: int | None = None
    # 配图在视频生成之后被改过（人工校正 / 重新提取）时为 true，
    # 此时视频里还是旧画面，前端应提示可以重新合成
    stale: bool = False


class Illustration(BaseModel):
    """模型生成的信息图。svg_url 保留动画能力，png_url 用于列表缩略图。"""

    png_url: str
    svg_url: str
    width: int
    height: int
    source: Literal["model", "fallback"]


class EpisodeVersion(BaseModel):
    """一个语言版本的全部产物。

    同一集的封面/论文配图是跨语言共用的（都来自同一份 PDF），
    所以只有需要「用文字表达」的东西才逐语言各存一份：
    解读、脚本、信息图、音频、视频。
    """

    language: Language
    paper_meta: PaperMeta | None = None
    analysis: Analysis | None = None
    script: Script | None = None
    illustration: Illustration | None = None
    audio_url: str | None = None
    audio_duration_sec: float | None = None
    audio_bytes: int | None = None
    video: VideoInfo | None = None


class Episode(BaseModel):
    id: str
    title: str
    source_type: SourceType
    source_ref: str | None = None
    status: Status
    stage_label: str = ""
    progress: int = 0
    error: str | None = None
    options: EpisodeOptions
    # 主语言 + 已产出的语言列表。`paper_meta`/`analysis`/`script`/`audio_*`/`video`
    # 这些顶层字段**镜像主语言那一版**，老前端不改也能用；
    # 要取另一语言请读 `versions[lang]`。
    language: Language = "zh"
    languages: list[Language] = Field(default_factory=list)
    versions: dict[str, EpisodeVersion] = Field(default_factory=dict)
    paper_meta: PaperMeta | None = None
    analysis: Analysis | None = None
    script: Script | None = None
    cover_url: str | None = None
    cover_width: int | None = None
    cover_height: int | None = None
    figures: list[Figure] = Field(default_factory=list)
    illustration: Illustration | None = None
    video: VideoInfo | None = None
    audio_url: str | None = None
    audio_duration_sec: float | None = None
    audio_bytes: int | None = None
    created_at: str
    updated_at: str


class EpisodeListItem(Episode):
    """列表接口：省略 analysis / script / versions 大字段。

    但保留 cover_url —— 列表卡片要显示封面缩略图。
    figures / illustration 也省略，列表用不到。
    """

    analysis: None = None
    script: None = None
    figures: list[Figure] = Field(default_factory=list)
    illustration: None = None
    video: None = None
    versions: dict[str, EpisodeVersion] = Field(default_factory=dict)


class EpisodeList(BaseModel):
    items: list[EpisodeListItem]
    total: int


class TextImportRequest(BaseModel):
    source_type: Literal["url", "text"]
    url: str | None = None
    text: str | None = None
    title: str | None = None
    options: EpisodeOptions = Field(default_factory=EpisodeOptions)


class FigureRotateRequest(BaseModel):
    """人工校正配图方向。"""

    direction: Literal["cw", "ccw"]


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    modes: dict[str, str]


class OptionItem(BaseModel):
    value: str | int
    label: str


class VoiceItem(BaseModel):
    id: str
    label: str
    gender: str
    pair: str
    # 这个音色属于哪一种语言版本。前端按当前语言过滤音色列表 ——
    # 拿中文音色去念英文虽然也能出声，但口音很明显。
    language: Language = "zh"


class OptionsResponse(BaseModel):
    durations: list[OptionItem]
    levels: list[OptionItem]
    voices: list[VoiceItem]
    # 服务端**支持**的语言（新建时选要产出哪些版本）。
    # 注意区分：Episode.languages 是「这一集实际产出了哪些」。
    languages: list[OptionItem] = Field(default_factory=list)
