from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Credentials(Schema):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class Discovery(Schema):
    count: int = Field(default=5, ge=1, le=30)
    ranges: list[tuple[float, float]] = Field(default_factory=lambda: [(15, 30), (30, 60), (60, 90)], min_length=1, max_length=5)
    language: Literal["auto", "en", "hi", "ne"] = "auto"

    @model_validator(mode="after")
    def ordered_ranges(self):
        if any(not 3 <= low <= high <= 180 for low, high in self.ranges):
            raise ValueError("Duration ranges must be within 3–180 seconds")
        return self


class NewProject(Schema):
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")
    discovery: Discovery = Field(default_factory=Discovery)


class SourceURL(Schema):
    url: str = Field(min_length=12, max_length=4096)
    authorized: Literal[True]


class Word(Schema):
    word: str = Field(min_length=1, max_length=120)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    speaker: str | None = Field(default=None, max_length=100)


class Caption(Schema):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=300, pattern=r"\S")

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("Caption end must follow its start")
        return self


class Scores(Schema):
    hook_strength: int = Field(ge=0, le=100)
    clarity: int = Field(ge=0, le=100)
    emotional_impact: int = Field(ge=0, le=100)
    standalone_value: int = Field(ge=0, le=100)
    overall: int = Field(ge=0, le=100)


class PlatformCopy(Schema):
    caption: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=2000)
    hashtags: list[str] = Field(max_length=15)
    thumbnail_text: str = Field(default="", max_length=80)
    title: str = Field(min_length=1, max_length=150)


class CandidateData(Schema):
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    hook: str = Field(min_length=1, max_length=180, pattern=r"\S")
    title: str = Field(min_length=1, max_length=150, pattern=r"\S")
    summary: str = Field(min_length=1, max_length=500, pattern=r"\S")
    category: str = Field(min_length=1, max_length=60, pattern=r"\S")
    scores: Scores
    reasoning_summary: str = Field(min_length=1, max_length=500, pattern=r"\S")
    platforms: dict[Literal["tiktok", "instagram", "youtube"], PlatformCopy] = Field(default_factory=dict)


class ClipSettings(Schema):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = Field(min_length=1, max_length=150)
    hashtags: str = Field(default="", max_length=500)
    ratio: Literal["9:16", "1:1", "16:9"] = "9:16"
    crop: float = Field(default=.5, ge=0, le=1)
    framing: Literal["manual", "smart", "fit"] = "manual"
    quality: Literal["standard", "high"] = "high"
    fps: Literal[0, 24, 30, 60] = 30
    shorten_pauses: bool = False
    normalize_audio: bool = False
    style: Literal["minimal", "creator", "highlight", "karaoke", "educational", "off"] = "creator"
    font: Literal["Noto Sans", "Noto Sans Devanagari", "DejaVu Sans"] = "Noto Sans"
    font_size: int = Field(default=52, ge=24, le=88)
    text_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9a-fA-F]{6}$")
    highlight_color: str = Field(default="#DFED83", pattern=r"^#[0-9a-fA-F]{6}$")
    stroke: int = Field(default=3, ge=0, le=10)
    shadow: int = Field(default=1, ge=0, le=8)
    position: Literal["top", "middle", "bottom"] = "bottom"
    words_per_line: int = Field(default=5, ge=1, le=12)
    overlay: str = Field(default="", max_length=120)
    captions: list[Caption] | None = Field(default=None, max_length=350)

    @model_validator(mode="after")
    def clip_length(self):
        if not 1 <= self.end - self.start <= 180:
            raise ValueError("Clips must be 1–180 seconds long")
        return self


class SegmentEdit(Caption):
    words: list[Word] | None = Field(default=None, max_length=500)


class SelectCandidate(Schema):
    candidate_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class AccountView(Schema):
    email: EmailStr
    admin: bool


class JobView(Schema):
    id: str
    project_id: str
    clip_id: str | None
    kind: Literal["analyze", "render"]
    status: Literal["queued", "running", "failed", "cancelled", "complete"]
    stage: str
    progress: int
    error: str | None
    attempt: int
    seconds: float | None
    created_at: float
    updated_at: float


class SourceView(Schema):
    filename: str
    kind: Literal["upload", "url"]
    size: int
    duration: float | None
    width: int | None
    height: int | None
    state: str
    preview: bool


class ProjectView(Schema):
    id: str
    name: str
    discovery: Discovery
    created_at: float
    source: SourceView | None
    job: JobView | None
    clip_count: int


class CandidateView(CandidateData):
    id: str


class ClipDraft(Schema):
    id: str
    settings: ClipSettings


class ClipView(ClipDraft):
    candidate_id: str
    framing: str | None
    available: bool


class ProjectDetail(ProjectView):
    jobs: list[JobView]
    candidates: list[CandidateView]
    clips: list[ClipView]


class SegmentView(Caption):
    id: str
    words: list[Word]


class TranscriptView(Schema):
    language: str | None
    provider: str | None
    segments: list[SegmentView]


class AssetLink(Schema):
    url: str
    expires: int
