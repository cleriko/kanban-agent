from __future__ import annotations

import logging

from fastapi import APIRouter

from ..services.agent_service import AgentService, _to_dto
from ..services.errors import ServiceError
from .deps import Authenticated, GatewayDep, SessionDep, SettingsDep, StorageDep, http_error
from .schemas import AgentActionDTO, AgentChatBody, AgentChatResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"], dependencies=[Authenticated])


@router.post("/chat", response_model=AgentChatResponse)
async def chat(body: AgentChatBody, session: SessionDep, gateway: GatewayDep,
               storage: StorageDep, settings: SettingsDep) -> AgentChatResponse:
    service = AgentService(session, gateway, storage, settings)
    try:
        return await service.chat(body)
    except ServiceError as error:
        raise http_error(error) from error


@router.post("/actions/{action_id}/execute", response_model=AgentChatResponse)
async def execute_action(action_id: str, session: SessionDep, gateway: GatewayDep,
                         storage: StorageDep, settings: SettingsDep) -> AgentChatResponse:
    """Runs one previously proposed operation. This is the only path by which an
    agent write reaches the database."""
    service = AgentService(session, gateway, storage, settings)
    try:
        return await service.execute(action_id)
    except ServiceError as error:
        raise http_error(error) from error


@router.get("/actions", response_model=list[AgentActionDTO])
async def list_actions(session: SessionDep, gateway: GatewayDep, storage: StorageDep,
                       settings: SettingsDep, history: bool = False,
                       limit: int = 50) -> list[AgentActionDTO]:
    service = AgentService(session, gateway, storage, settings)
    actions = await (service.history(limit) if history else service.pending(limit))
    return [_to_dto(a) for a in actions]
