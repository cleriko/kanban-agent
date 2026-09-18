from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from ..services.errors import ServiceError
from ..services.task_service import TaskService, to_dto
from .deps import Authenticated, SessionDep, http_error
from .schemas import CreateTaskBody, MoveBody, PatchTaskBody, SummaryDTO, TaskDTO

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Authenticated])


@router.get("", response_model=list[TaskDTO])
async def list_tasks(
    session: SessionDep,
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = None,
    tag: str | None = None,
    priority: str | None = None,
    due: str | None = None,
    source: str | None = None,
    meetingId: str | None = None,
    includeArchived: bool = False,
    includeDone: bool = True,
    limit: int | None = None,
) -> list[TaskDTO]:
    try:
        tasks = await TaskService(session).list(
            status=status_filter, query=q, tag=tag, priority=priority, due=due,
            source=source, meeting_id=meetingId,
            include_archived=includeArchived, include_done=includeDone, limit=limit)
    except ServiceError as error:
        raise http_error(error) from error
    return [to_dto(t) for t in tasks]


@router.post("", response_model=TaskDTO, status_code=status.HTTP_201_CREATED)
async def create_task(body: CreateTaskBody, session: SessionDep) -> TaskDTO:
    try:
        return to_dto(await TaskService(session).create(body))
    except ServiceError as error:
        raise http_error(error) from error


@router.get("/{task_id}", response_model=TaskDTO)
async def get_task(task_id: str, session: SessionDep) -> TaskDTO:
    try:
        return to_dto(await TaskService(session).get(task_id))
    except ServiceError as error:
        raise http_error(error) from error


@router.patch("/{task_id}", response_model=TaskDTO)
async def patch_task(task_id: str, body: PatchTaskBody, session: SessionDep) -> TaskDTO:
    try:
        return to_dto(await TaskService(session).patch(task_id, body))
    except ServiceError as error:
        raise http_error(error) from error


@router.delete("/{task_id}", response_model=TaskDTO)
async def delete_task(task_id: str, session: SessionDep, hard: bool = False) -> TaskDTO:
    try:
        return to_dto(await TaskService(session).delete(task_id, hard=hard))
    except ServiceError as error:
        raise http_error(error) from error


@router.post("/{task_id}/move", response_model=TaskDTO)
async def move_task(task_id: str, body: MoveBody, session: SessionDep) -> TaskDTO:
    try:
        return to_dto(await TaskService(session).move(task_id, body))
    except ServiceError as error:
        raise http_error(error) from error


@router.post("/{task_id}/complete", response_model=TaskDTO)
async def complete_task(task_id: str, session: SessionDep) -> TaskDTO:
    try:
        return to_dto(await TaskService(session).complete(task_id))
    except ServiceError as error:
        raise http_error(error) from error


@router.post("/{task_id}/reopen", response_model=TaskDTO)
async def reopen_task(task_id: str, session: SessionDep,
                      status_target: str | None = Query(default=None, alias="status")) -> TaskDTO:
    try:
        return to_dto(await TaskService(session).reopen(task_id, status_target))
    except ServiceError as error:
        raise http_error(error) from error


summary_router = APIRouter(tags=["tasks"], dependencies=[Authenticated])


@summary_router.get("/summary", response_model=SummaryDTO)
async def summary(session: SessionDep) -> SummaryDTO:
    return await TaskService(session).summary()
