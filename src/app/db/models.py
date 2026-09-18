from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON, Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """JSON rather than JSONB so the same models run on SQLite in tests."""

    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# --- Enums ------------------------------------------------------------------


class TaskStatus(str, enum.Enum):
    inbox = "inbox"
    todo = "todo"
    in_progress = "in_progress"
    done = "done"


class TaskPriority(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    urgent = "urgent"


class TaskSource(str, enum.Enum):
    manual = "manual"
    meeting = "meeting"
    agent = "agent"


class MeetingStatus(str, enum.Enum):
    created = "created"
    queued = "queued"
    uploading = "uploading"
    transcribing = "transcribing"
    analyzing = "analyzing"
    completed = "completed"
    failed = "failed"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class ActionItemState(str, enum.Enum):
    suggested = "suggested"
    added = "added"
    dismissed = "dismissed"


# --- Tables -----------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Task(Base):
    __tablename__ = "tasks"

    # The client generates ids so an offline-created task keeps its identity.
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)

    title: Mapped[str] = mapped_column(String(500))
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus, native_enum=False),
                                               default=TaskStatus.inbox, index=True)
    priority: Mapped[TaskPriority] = mapped_column(Enum(TaskPriority, native_enum=False),
                                                   default=TaskPriority.medium)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, index=True)
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    subtasks: Mapped[list[Any]] = mapped_column(JSON, default=list)
    position: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    source: Mapped[TaskSource] = mapped_column(Enum(TaskSource, native_enum=False),
                                               default=TaskSource.manual)
    meeting_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="SET NULL"), default=None, index=True)
    action_item_id: Mapped[str | None] = mapped_column(String(36), default=None, index=True)

    __table_args__ = (Index("ix_tasks_status_position", "status", "position"),)


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)

    title: Mapped[str] = mapped_column(String(500))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[MeetingStatus] = mapped_column(Enum(MeetingStatus, native_enum=False),
                                                  default=MeetingStatus.created, index=True)

    # Object storage key, never the audio itself.
    audio_key: Mapped[str | None] = mapped_column(String(500), default=None)
    audio_bytes: Mapped[int] = mapped_column(Integer, default=0)
    audio_content_type: Mapped[str | None] = mapped_column(String(100), default=None)

    summary: Mapped[str] = mapped_column(Text, default="")
    key_points: Mapped[list[Any]] = mapped_column(JSON, default=list)
    decisions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    questions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    topics: Mapped[list[Any]] = mapped_column(JSON, default=list)
    participants: Mapped[list[Any]] = mapped_column(JSON, default=list)

    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    transcript: Mapped[MeetingTranscript | None] = relationship(
        back_populates="meeting", uselist=False, cascade="all, delete-orphan", lazy="selectin")
    action_items: Mapped[list[MeetingActionItem]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", lazy="selectin",
        order_by="MeetingActionItem.position")
    meeting_decisions: Mapped[list[MeetingDecision]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", lazy="selectin",
        order_by="MeetingDecision.position")


class MeetingTranscript(Base):
    """One row per meeting. Segments are JSON: they are always read whole, and a
    row-per-segment table would mean thousands of inserts per meeting for no gain."""

    __tablename__ = "meeting_transcripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), unique=True, index=True)

    language: Mapped[str | None] = mapped_column(String(16), default=None)
    provider: Mapped[str | None] = mapped_column(String(64), default=None)
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    text: Mapped[str] = mapped_column(Text, default="")
    segments: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    meeting: Mapped[Meeting] = relationship(back_populates="transcript")


class MeetingActionItem(Base):
    __tablename__ = "meeting_action_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), index=True)

    title: Mapped[str] = mapped_column(String(500))
    assignee: Mapped[str | None] = mapped_column(String(200), default=None)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    state: Mapped[ActionItemState] = mapped_column(Enum(ActionItemState, native_enum=False),
                                                   default=ActionItemState.suggested)
    task_id: Mapped[str | None] = mapped_column(String(36), default=None)
    position: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    meeting: Mapped[Meeting] = relationship(back_populates="action_items")


class MeetingDecision(Base):
    __tablename__ = "meeting_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    meeting_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    position: Mapped[float] = mapped_column(Float, default=0.0)

    meeting: Mapped[Meeting] = relationship(back_populates="meeting_decisions")


class AgentAction(Base):
    """Every operation the agent proposes, stored before it can run.

    This is what makes agent behaviour auditable and undoable: the operation is
    validated and persisted first, then executed only when the client confirms.
    """

    __tablename__ = "agent_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)

    tool: Mapped[str] = mapped_column(String(64), index=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(String(500), default="")
    destructive: Mapped[bool] = mapped_column(Boolean, default=False)

    executed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # Snapshot of the affected row before execution, so an undo can be added later
    # without changing any call site.
    undo_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    meeting_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("meetings.id", ondelete="CASCADE"), default=None, index=True)

    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus, native_enum=False),
                                              default=JobStatus.queued, index=True)
    stage: Mapped[str | None] = mapped_column(String(64), default=None)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Set when a worker claims the job; lets an abandoned job be reclaimed.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    locked_by: Mapped[str | None] = mapped_column(String(100), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (Index("ix_jobs_claim", "status", "locked_at", "created_at"),)
