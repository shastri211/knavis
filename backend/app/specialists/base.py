from dataclasses import asdict, dataclass, field


class SpecialistUnavailable(RuntimeError):
    """The specialist cannot run (not configured, not allowed, input too large). The message is safe to show."""


@dataclass
class OCRPage:
    page: int | None                 # 1-based PDF page, or None for a standalone image
    text: str                        # markdown (tables kept) or plain paragraphs
    provider: str
    regions: list[dict] = field(default_factory=list)   # [{"text", "bbox", "confidence"}] when the provider returns geometry
    confidence: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "OCRPage":
        return cls(**data)


@dataclass
class FigureDescription:
    text: str
    informative: bool                # False for logos, decoration and plain photos: kept in the cache, not indexed
    provider: str
    model: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FigureDescription":
        return cls(**data)


@dataclass
class ASRSegment:
    start_s: float
    end_s: float
    text: str
    speaker: str | None = None


@dataclass
class ASRResult:
    segments: list[ASRSegment]
    text: str
    language: str | None
    duration_s: float | None
    provider: str
    model: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ASRResult":
        return cls(**{**data, "segments": [ASRSegment(**s) for s in data["segments"]]})
