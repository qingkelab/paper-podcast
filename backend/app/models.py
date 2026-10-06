"""API 请求 / 响应模型。字段与 docs/API.md 严格一致。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

SourceType = Literal["pdf", "url", "text"]
Level = Literal["intro", "advanced", "expert"]
Status = Literal[
    "queued", "parsing", "analyzing", "scripting", "synthesizing", "completed", "failed"
]


class EpisodeOptions(BaseModel):
    duration_min: int = 5
    level: Level = "intro"
    voice_a: str = ""
    voice_b: str = ""

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


class Illustration(BaseModel):
    """模型生成的信息图。svg_url 保留动画能力，png_url 用于列表缩略图。"""

    png_url: str
    svg_url: str
    width: int
    height: int
    source: Literal["model", "fallback"]


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
    """列表接口：省略 analysis / script 大字段。

    但保留 cover_url —— 列表卡片要显示封面缩略图。
    figures / illustration 也省略，列表用不到。
    """

    analysis: None = None
    script: None = None
    figures: list[Figure] = Field(default_factory=list)
    illustration: None = None
    video: None = None


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


class OptionsResponse(BaseModel):
    durations: list[OptionItem]
    levels: list[OptionItem]
    voices: list[VoiceItem]
