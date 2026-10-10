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
    # 画幅：`portrait`（936×1210，默认）/ `landscape`（1920×1080）。老数据没有这个字段
    orientation: Literal["portrait", "landscape"] = "portrait"
    # **封面上的大字标题**（爆款标题，逐语言各一份）。空串表示没有自定义过 ——
    # 视频里的封面会退回显示论文原题。用户可以在单集页改它（改完要重新合成才生效）。
    hook: str = ""
    # **视频第一帧的静帧**，给页面上的 `<video poster>` 用。缺省 null（老视频没有）。
    # 为什么不能用论文首页顶替：那上面**没有烘进标题**，页面上就看不出封面长什么样。
    poster_url: str | None = None
    # **封面标题改过、但还没重新合成**：视频里烘的还是旧标题。
    # 为什么单独一个字段而不是复用 `stale`：`stale` 说的是配图（旋转/删除过论文原图），
    # 提示文案和「该不该慌」都不是一回事。用户改的是一行字，要的是「重新合成一下就生效」。
    title_pending: bool = False
    # **这一份画面是哪条渲染路径画的**：`html`（Chrome 无头，默认）或 `svg`（resvg 保底）。
    # 两条路径同时在（没装 Chrome 就自动降级），所以「同一篇论文两台机器出的画面
    # 不一样」只能靠它分辨。老视频没有这个记录，返回 null。
    renderer: str | None = None


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
    video_landscape: VideoInfo | None = None


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
    video_landscape: VideoInfo | None = None
    audio_url: str | None = None
    audio_duration_sec: float | None = None
    audio_bytes: int | None = None
    created_at: str
    updated_at: str

    # --- V2：归属 / 可见性 / 专辑 -------------------------------------------
    # user_id 故意**不下发**（前端不需要知道，也没法验证别人的 id）。
    # 分享链接的 token；private 时为 null
    share_token: str | None = None
    visibility: Visibility = "private"
    album_id: str | None = None


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
    # V2："open" = 库里还没用户，不需要登录；"auth" = 需要登录。
    # 前端据此决定要不要跳登录页 —— 所以这个接口本身免登录。
    mode: Literal["open", "auth"] = "open"


# --------------------------------------------------------------------------
# V2：账号 / 专辑 / 分享 / 批量
# --------------------------------------------------------------------------

Visibility = Literal["private", "public"]


class User(BaseModel):
    """对外的用户信息。**永远不含 password_hash**。"""

    id: str
    username: str
    display_name: str = ""
    created_at: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    signup_code: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class Album(BaseModel):
    id: str
    title: str
    description: str | None = None
    episode_count: int = 0
    # 用专辑里最新一集的封面当专辑封面；没有单集时为 null
    cover_url: str | None = None
    created_at: str
    updated_at: str


class AlbumDetail(Album):
    """专辑详情：附带里面单集的摘要列表。

    用 `EpisodeListItem` 而不是完整 Episode —— 一个专辑可能装十几期，
    带全量 analysis/script/figures/video 会让响应到 MB 级。
    """

    episodes: list["EpisodeListItem"] = Field(default_factory=list)


class AlbumWriteRequest(BaseModel):
    title: str | None = None
    description: str | None = None


class AlbumAssignRequest(BaseModel):
    episode_ids: list[str] = Field(default_factory=list)


class BatchRequest(BaseModel):
    """批量生成。urls 与 texts 二选一（由 source_type 决定）。"""

    source_type: Literal["url", "text"] = "url"
    urls: list[str] | None = None
    texts: list[str] | None = None
    title: str | None = None
    options: EpisodeOptions = Field(default_factory=EpisodeOptions)


class BatchFailure(BaseModel):
    ref: str
    reason: str


class BatchResult(BaseModel):
    """批量结果。**单项失败不影响其他项** —— failed 里逐条给出原因。"""

    created: list["EpisodeListItem"] = Field(default_factory=list)
    failed: list[BatchFailure] = Field(default_factory=list)
    total: int = 0


class ShareAuthor(BaseModel):
    """公开页只露展示名，不给 username / id。"""

    display_name: str


class ShareView(BaseModel):
    """免登录的公开视图。

    刻意**不是**完整 Episode：不含 id / options / source_ref / raw_text / error /
    figures 的磁盘路径。别人拿到 token 能看内容，但看不到内部结构。
    """

    # 公开分享时是 share token；首页展示「自己的那一期」时为 null
    token: str | None = None
    title: str
    paper_meta: PaperMeta | None = None
    language: Language = "zh"
    languages: list[Language] = Field(default_factory=list)
    versions: dict[str, "ShareVersion"] = Field(default_factory=dict)
    cover_url: str | None = None
    cover_width: int | None = None
    cover_height: int | None = None
    figures: list[Figure] = Field(default_factory=list)
    illustration: Illustration | None = None
    video: VideoInfo | None = None
    video_landscape: VideoInfo | None = None
    audio_url: str | None = None
    audio_duration_sec: float | None = None
    audio_bytes: int | None = None
    script: Script | None = None
    analysis: Analysis | None = None
    author: ShareAuthor | None = None
    created_at: str


class ShareVersion(BaseModel):
    language: Language
    script: Script | None = None
    analysis: Analysis | None = None
    paper_meta: PaperMeta | None = None
    illustration: Illustration | None = None
    audio_url: str | None = None
    audio_duration_sec: float | None = None
    audio_bytes: int | None = None
    video: VideoInfo | None = None
    video_landscape: VideoInfo | None = None


class UsageResponse(BaseModel):
    """这一天的生成配额用了多少。前端拿它显示「今天还能生成几期」。"""

    used: int = 0
    limit: int = 0
    remaining: int | None = None
    # 配额窗口是「最近 24 小时」的滑动窗口，不是自然日 —— 所以给的是重置时刻
    resets_at: str | None = None


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
