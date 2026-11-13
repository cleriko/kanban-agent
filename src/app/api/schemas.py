from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# The client always sends UUIDs; constraining ids here rejects path-traversal values
# before they can reach a storage key and keeps them inside the DB column width.
_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

# These field names are the contract with the macOS app. See
# Sources/Shared/Core/Networking/DTOs.swift for the other half.


# --- Tasks ------------------------------------------------------------------


class SubtaskDTO(BaseModel):
    id: str
    title: str
    done: bool = False


class TaskDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    notes: str = ""
    status: str
    priority: str
    due: str | None = None
    tags: list[str] = Field(default_factory=list)
    subtasks: list[SubtaskDTO] = Field(default_factory=list)
    position: float = 0
    created: str
    updated: str
    completed: str | None = None
    archived: str | None = None
    source: str = "manual"
    meetingId: str | None = None
    actionItemId: str | None = None
    overdue: bool = False
    dueToday: bool = False


class CreateTaskBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = Field(default=None, pattern=_UUID)
    title: str = Field(min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=100_000)
    status: str | None = None
    priority: str | None = None
    due: str | None = None
    tags: list[str] | None = None
    # Titles, or full objects when a client echoes a task back. Both are accepted so
    # an outbox replay never 422s on a shape difference.
    subtasks: list[str | dict[str, Any]] | None = None
    # Infinity/NaN would poison the sparse ordering maths for every later insert.
    position: float | None = Field(default=None, allow_inf_nan=False)
    source: str | None = None
    meetingId: str | None = None
    actionItemId: str | None = None


class PatchTaskBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=100_000)
    status: str | None = None
    priority: str | None = None
    due: str | None = None
    tags: list[str] | None = None
    archived: bool | None = None
    position: float | None = Field(default=None, allow_inf_nan=False)


class MoveBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str
    index: int | None = None
    position: float | None = Field(default=None, allow_inf_nan=False)


class SummaryDTO(BaseModel):
    open: int
    active: int
    dueToday: int
    overdue: int
    doneToday: int
    byStatus: dict[str, int]
    agenda: list[TaskDTO]
    generatedAt: str


# --- Meetings ---------------------------------------------------------------


class ActionItemDTO(BaseModel):
    id: str
    title: str
    assignee: str | None = None
    dueDate: str | None = None
    confidence: float = 0.0
    state: str = "suggested"
    taskId: str | None = None


class TranscriptSegmentDTO(BaseModel):
    id: str | None = None
    start: float
    end: float
    speaker: str | None = None
    text: str


class TranscriptDTO(BaseModel):
    segments: list[TranscriptSegmentDTO] = Field(default_factory=list)
    language: str | None = None
    duration: float = 0.0


class MeetingDTO(BaseModel):
    id: str
    title: str
    startedAt: str
    duration: float
    status: str
    summary: str | None = None
    keyPoints: list[str] | None = None
    decisions: list[str] | None = None
    questions: list[str] | None = None
    topics: list[str] | None = None
    participants: list[str] | None = None
    actionItems: list[ActionItemDTO] | None = None
    transcript: TranscriptDTO | None = None
    jobId: str | None = None
    error: str | None = None


class CreateMeetingBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = Field(default=None, pattern=_UUID)
    title: str = Field(min_length=1, max_length=500)
    startedAt: str | None = None
    duration: float = Field(default=0.0, ge=0)


# --- Jobs -------------------------------------------------------------------


class JobDTO(BaseModel):
    id: str
    status: str
    stage: str | None = None
    progress: float = 0.0
    error: str | None = None
    meetingId: str | None = None


# --- Agent ------------------------------------------------------------------


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class AgentContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    open: int = 0
    active: int = 0
    dueToday: int = 0
    overdue: int = 0
    doneToday: int = 0
    byStatus: dict[str, int] = Field(default_factory=dict)
    agenda: list[dict[str, Any]] = Field(default_factory=list)
    generatedAt: str = ""


class AgentChatBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str
    history: list[Turn] = Field(default_factory=list)
    context: AgentContext = Field(default_factory=AgentContext)
    clientVersion: str = ""


class AgentActionDTO(BaseModel):
    id: str
    tool: str
    summary: str
    destructive: bool = False
    applied: bool = False


class AgentChatResponse(BaseModel):
    reply: str | None = None
    pending_actions: list[AgentActionDTO] | None = None
    applied_actions: list[AgentActionDTO] | None = None
    tasks_changed: bool | None = None


# --- Misc -------------------------------------------------------------------


class HealthDTO(BaseModel):
    status: str
    version: str
    database: bool
    storage: str
    transcription: str
    llm: str
    # False when WC_API_TOKEN is empty and unauthenticated mode is off: every route
    # will 503 even though the database is fine. Defaults to True for old clients.
    auth: bool = True
    # Present only when something is wrong, with a hint at the likely cause.
    detail: str | None = None


class ErrorBody(BaseModel):
    error: str
    detail: str | None = None
