"""谱系接口出入参。所有写接口都接受可选 event_id 作为消息幂等键。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EventPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    event_id: str | None = Field(default=None, description="消息幂等键；不传则由服务端生成")
    remark: str | None = None


class GraphResult(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    batches: list[dict[str, Any]]
    anchor_node_ids: list[int] = []


class ReplayResult(BaseModel):
    replayed: int
    details: list[dict[str, Any]]


class ConsistencyResult(BaseModel):
    ok: bool
    problems: list[str]
    checked_batches: int
    checked_nodes: int
    checked_events: int
