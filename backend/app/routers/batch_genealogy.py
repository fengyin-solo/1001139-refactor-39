"""材料批次谱系接口：浏览批次谱系图、追加谱系事件、迁移存量无批次材料。

旧的养护材料取值接口（/api/material）保持不变；谱系结论会回写到库存台账、
工程清单与车辆待办，通过 /writeback 一处看全。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.batch_genealogy import EVENT_TYPES, BatchGenealogyService

router = APIRouter(prefix="/api/batch-genealogy", tags=["材料批次谱系"])

service = BatchGenealogyService()


@router.get("/nodes", response_model=PageResult[dict])
def list_nodes(
    keyword: str | None = Query(default=None, description="按批次号或材料编号检索"),
    event_type: str | None = Query(default=None, description="供应商到场、仓管入库、工程领用、车辆装载、退料、换货、跨工程调拨"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按批次与事件类型浏览谱系节点；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_nodes(keyword=keyword, event_type=event_type, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/batches/{batch_no}", response_model=dict)
def batch_tree(batch_no: str) -> dict:
    """读取单个批次的谱系图：本批次节点、跨批次父节点与有向边，回看不断链。"""
    tree = service.batch_tree(batch_no)
    if tree is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_no} 没有谱系节点")
    return tree


@router.post("/events", response_model=ActionResult)
def append_event(payload: EntryPayload) -> ActionResult:
    """追加谱系事件：携带 event_id 保证重放幂等；工程领用必须带版本门闩 expected_version。"""
    node, message = service.append_event(payload.values)
    if node is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=node)


@router.get("/archive", response_model=PageResult[dict])
def list_archive(
    keyword: str | None = Query(default=None, description="按批次号或材料编号检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """历史领用存档：按原批次留存，退料、换货、跨工程调拨都不回写这里。"""
    items, total = service.list_archive(keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/migrate", response_model=ActionResult)
def migrate_legacy(payload: EntryPayload) -> ActionResult:
    """存量无批次材料迁移归位：任一节点失败时整批节点与台账一并回滚。"""
    summary, message = service.migrate_legacy(payload.values)
    if summary is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=summary)


@router.get("/writeback", response_model=dict)
def writeback_view() -> dict[str, Any]:
    """批次结论回写视图：库存台账、各工程材料清单、各车辆装载待办。"""
    return service.writeback_view()


@router.get("/meta", response_model=dict)
def meta() -> dict[str, Any]:
    """谱系事件类型清单，前端表单按此渲染。"""
    return {"event_types": EVENT_TYPES}
