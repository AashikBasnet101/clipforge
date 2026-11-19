from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import DATABASE


def timestamp():
    return datetime.now(timezone.utc).timestamp()


def uid():
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    created_at: Mapped[float] = mapped_column(Float, default=timestamp, index=True)


class User(Record, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str]
    admin: Mapped[bool] = mapped_column(Boolean, default=False)


class Session(Base):
    __tablename__ = "sessions"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires: Mapped[float] = mapped_column(Float, index=True)


class Project(Record, Base):
    __tablename__ = "projects"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    discovery: Mapped[dict] = mapped_column(JSON)


class ProjectRecord(Record):
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)


class Source(ProjectRecord, Base):
    __tablename__ = "source_videos"
    filename: Mapped[str]
    kind: Mapped[str]
    url: Mapped[str | None] = mapped_column(Text)
    key: Mapped[str]
    size: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[float | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    mime: Mapped[str | None]
    state: Mapped[str] = mapped_column(default="pending")
    preview_key: Mapped[str | None]


class Transcript(ProjectRecord, Base):
    __tablename__ = "transcriptions"
    language: Mapped[str]
    provider: Mapped[str]


class Segment(Record, Base):
    __tablename__ = "transcript_segments"
    transcript_id: Mapped[str] = mapped_column(ForeignKey("transcriptions.id", ondelete="CASCADE"), index=True)
    start: Mapped[float]
    end: Mapped[float]
    text: Mapped[str] = mapped_column(Text)
    words: Mapped[list] = mapped_column(JSON)


class Candidate(ProjectRecord, Base):
    __tablename__ = "clip_candidates"
    data: Mapped[dict] = mapped_column(JSON)


class Clip(ProjectRecord, Base):
    __tablename__ = "generated_clips"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("clip_candidates.id", ondelete="CASCADE"), index=True)
    settings: Mapped[dict] = mapped_column(JSON)
    key: Mapped[str | None]
    srt_key: Mapped[str | None]
    framing: Mapped[str | None]
    size: Mapped[int] = mapped_column(default=0)


class Job(ProjectRecord, Base):
    __tablename__ = "render_jobs"
    clip_id: Mapped[str | None] = mapped_column(ForeignKey("generated_clips.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str]
    status: Mapped[str] = mapped_column(default="queued", index=True)
    stage: Mapped[str] = mapped_column(default="Queued")
    progress: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[float] = mapped_column(default=timestamp, onupdate=timestamp)
    started_at: Mapped[float | None]
    seconds: Mapped[float | None]
    attempt: Mapped[int] = mapped_column(default=1)
    snapshot: Mapped[dict | None] = mapped_column(JSON)
    dispatched: Mapped[bool] = mapped_column(default=False, index=True)


class Usage(ProjectRecord, Base):
    __tablename__ = "usage_records"
    metric: Mapped[str]
    amount: Mapped[float]
    model: Mapped[str | None]


class Audit(Record, Base):
    __tablename__ = "audit_events"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    action: Mapped[str]


class PublishingAccount(Record, Base):
    __tablename__ = "publishing_accounts"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str]
    status: Mapped[str]


class PublishingJob(ProjectRecord, Base):
    __tablename__ = "publishing_jobs"
    account_id: Mapped[str] = mapped_column(ForeignKey("publishing_accounts.id", ondelete="CASCADE"), index=True)
    clip_id: Mapped[str] = mapped_column(ForeignKey("generated_clips.id", ondelete="CASCADE"), index=True)
    status: Mapped[str]
    scheduled_at: Mapped[float | None]


engine = create_engine(DATABASE, pool_pre_ping=True)
if DATABASE.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def sqlite_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

DB = sessionmaker(engine, expire_on_commit=False)
