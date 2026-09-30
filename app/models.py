from pydantic import BaseModel, Field


class MentionSpan(BaseModel):
    raw: str
    title: str
    author: str | None = None
    confidence: float = Field(ge=0, le=1)
    work_id: str | None = None


class Classification(BaseModel):
    tags: dict[str, float] = Field(default_factory=dict)
    sentiment: float = Field(ge=-1, le=1)
    recommendation_strength: float = Field(ge=0, le=1)


class RunMetrics(BaseModel):
    threads_discovered: int = 0
    threads_fetched: int = 0
    threads_skipped: int = 0
    threads_reused_raw: int = 0
    threads_remaining: int = 0
    comments_fetched: int = 0
    new_comments: int = 0
    mentions_extracted: int = 0
    books_resolved: int = 0
    unresolved_mentions: int = 0
    metadata_lookups: int = 0
    classifications_performed: int = 0
    failures: int = 0
    runtime_seconds: float = 0
