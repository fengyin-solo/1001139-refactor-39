"""批次谱系接口。

写接口（到场/入库/领用/装载/退料/换货/调拨/存档/迁移）全部：

* 接受可选 ``event_id``，不传则按「类型-UUID」生成，消息重放时携带同一键即可幂等；
* 谱系节点、批次台账、工程清单、车辆待办在同一事务提交，失败整批回滚；
* 领用/装载支持 ``expected_version`` 版本门闩，冲突返回 409。
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.lineage.errors import (
    InsufficientStockError,
    LineageError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from app.lineage.schemas import ConsistencyResult, EventPayload, GraphResult, ReplayResult
from app.lineage.service import EVENT_TYPES, bootstrap_demo, lineage_service

router = APIRouter(prefix="/api/lineage", tags=["批次谱系"])

_STATUS = {
    NotFoundError: 404,
    ValidationError: 400,
    VersionConflictError: 409,
    InsufficientStockError: 409,
}


def _event_id(event_type: str, payload: EventPayload) -> str:
    return payload.event_id or f"{event_type}-{uuid.uuid4().hex}"


def _dispatch(event_type: str, payload: EventPayload) -> Any:
    handler = getattr(lineage_service, {
        "arrival": "arrival",
        "inbound": "inbound",
        "requisition": "requisition",
        "loading": "loading",
        "return_material": "return_material",
        "exchange": "exchange",
        "transfer": "transfer",
        "historical_archive": "historical_archive",
        "migration": "migrate_unbatched",
        "complete_todo": "complete_todo",
    }[event_type])
    return handler(payload.values, _event_id(event_type, payload))


def _error(exc: LineageError) -> JSONResponse:
    status = _STATUS.get(type(exc), 400)
    return JSONResponse(status_code=status,
                        content={"ok": False, "code": exc.code, "message": str(exc)})


# -------------------------------------------------------------- 写：库存事件

@router.post("/events/arrival")
def arrival(payload: EventPayload) -> Any:
    """供应商到场登记：材料可按 material_id/material_no 关联，也可现场建档。"""
    try:
        result = _dispatch("arrival", payload)
    except LineageError as exc:
        return _error(exc)
    return {"ok": True, **result}


@router.post("/events/inbound")
def inbound(payload: EventPayload) -> Any:
    """仓管入库：建立批次台账行（含 version 门闩初值）。"""
    try:
        result = _dispatch("inbound", payload)
    except LineageError as exc:
        return _error(exc)
    return {"ok": True, **result}


@router.post("/events/requisition")
def requisition(payload: EventPayload) -> Any:
    """工程领用：携带 expected_version 时启用版本门闩，余额不足返回 409。"""
    try:
        result = _dispatch("requisition", payload)
    except LineageError as exc:
        return _error(exc)
    return {"ok": True, **result}


@router.post("/events/loading")
def loading(payload: EventPayload) -> Any:
    """车辆装载：结论回写工程清单并生成车辆待办。"""
    try:
        result = _dispatch("loading", payload)
    except LineageError as exc:
        return _error(exc)
    return {"ok": True, **result}


@router.post("/events/return")
def return_material(payload: EventPayload) -> Any:
    """退料：新增谱系节点，批次余额回补、清单待装载核减。"""
    try:
        result = _dispatch("return_material", payload)
    except LineageError as exc:
        return _error(exc)
    return {"ok": True, **result}


@router.post("/events/exchange")
def exchange(payload: EventPayload) -> Any:
    """换货：旧批次 exchange_out 与新批次 exchange_in 在同一事务成对落库。"""
    try:
        result = _dispatch("exchange", payload)
    except LineageError as exc:
        return _error(exc)
    return {"ok": True, **result}


@router.post("/events/transfer")
def transfer(payload: EventPayload) -> Any:
    """跨工程调拨：源工程 transfer_out、目标工程 transfer_in，同批次不断链。"""
    try:
        result = _dispatch("transfer", payload)
    except LineageError as exc:
        return _error(exc)
    return {"ok": True, **result}


@router.post("/events/historical-archive")
def historical_archive(payload: EventPayload) -> Any:
    """历史领用按原批次存档：只补谱系节点，不影响当期库存。"""
    try:
        result = _dispatch("historical_archive", payload)
    except LineageError as exc:
        return _error(exc)
    return {"ok": True, **result}


@router.post("/events/complete-todo")
def complete_todo(payload: EventPayload) -> Any:
    """确认车辆装载完成（关闭车辆待办）。"""
    try:
        result = _dispatch("complete_todo", payload)
    except LineageError as exc:
        return _error(exc)
    return {"ok": True, **result}


# ----------------------------------------------------------- 写：存量迁移

@router.post("/bootstrap")
def bootstrap() -> Any:
    """引导一条可浏览的示例谱系（幂等，可重复调用）。"""
    return bootstrap_demo()


@router.get("/migration/unbatched")
def unbatched() -> dict[str, Any]:
    """列出尚未归属任何批次的存量材料。"""
    items = lineage_service.unbatched_materials()
    return {"total": len(items), "items": items}


@router.post("/migration/run")
def migrate(payload: EventPayload) -> Any:
    """存量无批次材料迁移归位：整批成功或整批回退。"""
    try:
        result = _dispatch("migration", payload)
    except LineageError as exc:
        return _error(exc)
    return {"ok": True, **result}


# ---------------------------------------------------------------- 读：谱系

@router.get("/graph", response_model=GraphResult)
def graph(
    batch_no: str | None = Query(default=None, description="按批次号查看完整链路"),
    material_id: int | None = Query(default=None, description="按材料聚合其全部批次"),
    project_id: int | None = Query(default=None, description="按工程聚焦相关链路"),
    vehicle_id: int | None = Query(default=None, description="按车辆聚焦装载链路"),
) -> GraphResult:
    """浏览批次谱系图：节点 + 衍生/替换/调拨/存档边。"""
    data = lineage_service.graph(batch_no=batch_no, material_id=material_id,
                                 project_id=project_id, vehicle_id=vehicle_id)
    return GraphResult(**data)


@router.get("/batches")
def batches(material_id: int | None = None, project_id: int | None = None) -> dict[str, Any]:
    """批次台账列表（结论回写库存台账后的批次视图）。"""
    items = lineage_service.list_batches(material_id=material_id, project_id=project_id)
    return {"total": len(items), "items": items}


@router.get("/ledger")
def ledger() -> dict[str, Any]:
    """库存台账（批次粒度）。"""
    items = lineage_service.ledger()
    return {"total": len(items), "items": items}


@router.get("/project-materials")
def project_materials(project_id: int | None = None) -> dict[str, Any]:
    """工程清单（批次领用/装载/退料结论）。"""
    items = lineage_service.project_materials(project_id)
    return {"total": len(items), "items": items}


@router.get("/vehicle-todos")
def vehicle_todos(vehicle_id: int | None = None, status: str | None = None) -> dict[str, Any]:
    """车辆待办。"""
    items = lineage_service.vehicle_todos(vehicle_id=vehicle_id, status=status)
    return {"total": len(items), "items": items}


# -------------------------------------------------------- 消息重放与一致性

@router.get("/events")
def list_events(event_type: str | None = None) -> dict[str, Any]:
    """事件日志：消息重放的依据。"""
    items = lineage_service.events(event_type)
    return {"total": len(items), "event_types": EVENT_TYPES, "items": items}


@router.post("/events/replay", response_model=ReplayResult)
def replay_events(since_id: int = 0) -> ReplayResult:
    """按事件日志顺序重放：event_id 去重保证不重复扣减。"""
    data = lineage_service.replay_events(since_id=since_id)
    return ReplayResult(**data)


@router.get("/consistency", response_model=ConsistencyResult)
def consistency() -> ConsistencyResult:
    """谱系写入与库存事件逻辑相符性自检。"""
    return ConsistencyResult(**lineage_service.verify_consistency())
