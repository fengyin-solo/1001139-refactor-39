"""批次谱系核心服务。

一张表一条链路：

    供应商到场 ─derived→ 仓管入库 ─derived→ 工程领用 ─derived→ 车辆装载
                                  │
                                  ├─ 退料（回到台账，余额回补）
                                  ├─ 换货：exchange_out ─replace→ exchange_in（新批次）
                                  └─ 跨工程调拨：transfer_out ─transfer→ transfer_in（目标工程）

设计要点：

* 谱系写入与库存事件同生共死——所有动作先写事件日志，再在同一个 store 事务里
  落节点、边、批次台账与回写，任何一步抛错整批回滚（见 :class:`app.store.Store`）。
* 事件带 ``event_id`` 幂等键：重复投递/重放直接返回首次结果，绝不重复扣减。
* 批次台账行带 ``version`` 门闩：领用/装载必须携带读取时的版本号，并发下后到的
  请求拿到 :class:`VersionConflictError`，由调用方重读后重试。
* 批次结论同步回写三处：库存台账（``material`` 旧表 + ``_lineage_batches``）、
  工程清单（``project`` 旧表 + ``_lineage_project_materials``）、
  车辆待办（``vehicle`` 旧表 + ``_lineage_vehicle_todos``）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.lineage.errors import (
    ARRIVAL,
    BATCH_CLOSED,
    BATCH_IN_STOCK,
    BATCH_ISSUED,
    BATCH_LOADED,
    EDGE_ARCHIVE,
    EDGE_DERIVED,
    EDGE_REPLACE,
    EDGE_TRANSFER,
    EVENT_TYPES,
    EXCHANGE_IN,
    EXCHANGE_OUT,
    HISTORICAL,
    INBOUND,
    LOADING,
    MIGRATION,
    NODE_KINDS,
    NODE_LABELS,
    REQUISITION,
    RETURN,
    TRANSFER_IN,
    TRANSFER_OUT,
    InsufficientStockError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from app.store import store

# ---- 内部表名 ---------------------------------------------------------------

T_NODES = "_lineage_nodes"
T_EDGES = "_lineage_edges"
T_BATCHES = "_lineage_batches"
T_PROJECT_MATERIALS = "_lineage_project_materials"
T_VEHICLE_TODOS = "_lineage_vehicle_todos"
T_EVENTS = "_lineage_events"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _require(values: dict[str, Any], *fields: str) -> list[Any]:
    missing = [name for name in fields if values.get(name) in (None, "")]
    if missing:
        raise ValidationError(f"缺少必填参数：{'、'.join(missing)}")
    return [values[name] for name in fields]


def _as_int(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"参数 {name} 必须是正整数") from None
    if result <= 0:
        raise ValidationError(f"参数 {name} 必须是正整数")
    return result


class LineageService:
    # ------------------------------------------------------------------ 基础

    def _add_node(self, *, kind: str, batch_no: str | None, material_id: int | None,
                  quantity: float, event_id: str, summary: str,
                  project_id: int | None = None, vehicle_id: int | None = None,
                  extra: dict[str, Any] | None = None) -> dict[str, Any]:
        if kind not in NODE_KINDS:
            raise ValidationError(f"未知谱系节点类型：{kind}")
        node = {
            "id": store.next_id(T_NODES),
            "kind": kind,
            "kind_label": NODE_LABELS[kind],
            "batch_no": batch_no,
            "material_id": material_id,
            "project_id": project_id,
            "vehicle_id": vehicle_id,
            "quantity": quantity,
            "summary": summary,
            "event_id": event_id,
            "created_at": _now(),
        }
        if extra:
            node.update(extra)
        store.rows(T_NODES).append(node)
        return node

    def _add_edge(self, *, parent_id: int, child_id: int, relation: str,
                  from_batch: str | None = None, to_batch: str | None = None,
                  quantity: float = 0, project_id: int | None = None) -> dict[str, Any]:
        edge = {
            "id": store.next_id(T_EDGES),
            "parent_id": parent_id,
            "child_id": child_id,
            "relation": relation,
            "from_batch": from_batch,
            "to_batch": to_batch,
            "quantity": quantity,
            "project_id": project_id,
            "created_at": _now(),
        }
        store.rows(T_EDGES).append(edge)
        return edge

    def _latest_node(self, batch_no: str, *, kinds: set[str] | None = None) -> dict[str, Any] | None:
        result = None
        for node in store.rows(T_NODES):
            if node.get("batch_no") != batch_no:
                continue
            if kinds and node.get("kind") not in kinds:
                continue
            if result is None or node["id"] > result["id"]:
                result = node
        return result

    def _get_batch(self, batch_no: str) -> dict[str, Any]:
        for row in store.rows(T_BATCHES):
            if row["batch_no"] == batch_no:
                return row
        raise NotFoundError(f"批次 {batch_no} 不存在")

    def _find_project(self, project_id: int) -> dict[str, Any]:
        row = store.find("project", project_id)
        if row is None:
            raise NotFoundError(f"养护工程 {project_id} 不存在")
        return row

    def _find_vehicle(self, vehicle_id: int) -> dict[str, Any]:
        row = store.find("vehicle", vehicle_id)
        if row is None:
            raise NotFoundError(f"养护车辆 {vehicle_id} 不存在")
        return row

    def _find_material(self, material_id: int) -> dict[str, Any]:
        row = store.find("material", material_id)
        if row is None:
            raise NotFoundError(f"养护材料 {material_id} 不存在")
        return row

    # ------------------------------------------------------------ 事件/幂等

    def _record_event(self, *, event_id: str, event_type: str,
                      payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(T_EVENTS)
        for row in rows:
            if row["event_id"] == event_id:
                # 幂等命中：类型必须一致，防止同一键串用在不同动作上
                if row["event_type"] != event_type:
                    raise ValidationError(
                        f"事件 {event_id} 已用于「{EVENT_TYPES.get(row['event_type'], row['event_type'])}」，"
                        f"不能再作为「{EVENT_TYPES.get(event_type, event_type)}」重放"
                    )
                return row["result"]
        row = {
            "id": store.next_id(T_EVENTS),
            "event_id": event_id,
            "event_type": event_type,
            "payload": payload,
            "result": result,
            "replayed": False,
            "created_at": _now(),
        }
        rows.append(row)
        return result

    def _run_once(self, *, event_id: str, event_type: str,
                  payload: dict[str, Any], producer) -> dict[str, Any]:
        """事件包裹：先查重，再在事务里执行业务并落日志；失败整批回滚。"""
        for row in store.rows(T_EVENTS):
            if row["event_id"] == event_id:
                if row["event_type"] != event_type:
                    raise ValidationError(
                        f"事件 {event_id} 已用于「{EVENT_TYPES.get(row['event_type'], row['event_type'])}」，"
                        f"不能再作为「{EVENT_TYPES.get(event_type, event_type)}」重放"
                    )
                return dict(row["result"], idempotent=True)

        with store.transaction():
            result = producer(event_id)
            self._record_event(event_id=event_id, event_type=event_type,
                               payload=payload, result=result)
        return dict(result, idempotent=False)

    # ------------------------------------------------------------ 回写三处

    def _writeback_material(self, material_id: int | None) -> None:
        """批次结论回写库存台账（material 旧行）。"""
        if material_id is None:
            return
        row = self._find_material(material_id)
        batches = [b for b in store.rows(T_BATCHES) if b["material_id"] == material_id]
        if batches:
            total_balance = sum(float(b["balance"]) for b in batches)
            latest = max(batches, key=lambda b: b["id"])
            row["批次编号"] = latest["batch_no"]
            row["批次数量"] = total_balance
            active = [b for b in batches if float(b["balance"]) > 0]
            if active:
                # 有可用余额：在库批次优先，避免被已装载批次覆盖
                statuses = {b["status"] for b in active}
                row["材料状态"] = BATCH_IN_STOCK if BATCH_IN_STOCK in statuses else next(
                    (b["status"] for b in active if b["status"] != BATCH_CLOSED),
                    active[0]["status"],
                )
                row["status"] = "在库"
            else:
                row["材料状态"] = BATCH_CLOSED
                if row.get("status") == "在库":
                    row["status"] = "已领用"

    def _writeback_project(self, project_id: int) -> None:
        """批次结论回写工程清单（project 旧行 + 清单表）。"""
        items = [m for m in store.rows(T_PROJECT_MATERIALS)
                 if m["project_id"] == project_id]
        row = self._find_project(project_id)
        row["领用材料数"] = len({m["material_id"] for m in items})
        row["领用批次"] = sorted({m["batch_no"] for m in items})
        row["待装载数量"] = sum(float(m["outstanding_qty"]) for m in items)
        pending = sum(1 for m in items if float(m["outstanding_qty"]) > 0)
        row["pending"] = pending > 0

    def _refresh_project_item(self, project_id: int, batch_no: str,
                              material_id: int, delta_outstanding: float) -> None:
        items = store.rows(T_PROJECT_MATERIALS)
        item = next((m for m in items
                     if m["project_id"] == project_id and m["batch_no"] == batch_no
                     and m["material_id"] == material_id), None)
        if item is None:
            item = {
                "id": store.next_id(T_PROJECT_MATERIALS),
                "project_id": project_id,
                "material_id": material_id,
                "batch_no": batch_no,
                "received_qty": 0.0,
                "loaded_qty": 0.0,
                "returned_qty": 0.0,
                "outstanding_qty": 0.0,
                "updated_at": _now(),
            }
            items.append(item)
        item["outstanding_qty"] = float(item["outstanding_qty"]) + delta_outstanding
        item["updated_at"] = _now()
        if delta_outstanding > 0:
            item["received_qty"] = float(item["received_qty"]) + delta_outstanding
        self._writeback_project(project_id)

    def _writeback_vehicle(self, vehicle_id: int | None) -> None:
        if vehicle_id is None:
            return
        row = self._find_vehicle(vehicle_id)
        todos = [t for t in store.rows(T_VEHICLE_TODOS)
                 if t["vehicle_id"] == vehicle_id and t["status"] == "待装载"]
        row["待装载批次"] = [t["batch_no"] for t in todos]
        row["待装载数量"] = sum(float(t["quantity"]) - float(t["loaded_qty"]) for t in todos)
        row["pending"] = bool(todos)

    # ------------------------------------------------------------ 到场/入库

    def arrival(self, values: dict[str, Any], event_id: str) -> dict[str, Any]:
        def produce(eid: str) -> dict[str, Any]:
            material_id = values.get("material_id")
            material_no = values.get("material_no")
            material_name, supplier = _require(values, "material_name", "supplier")
            quantity = _as_int(values.get("quantity"), "quantity")
            batch_no = str(values.get("batch_no") or "").strip()
            if not batch_no:
                raise ValidationError("缺少必填参数：batch_no")
            if any(b["batch_no"] == batch_no for b in store.rows(T_BATCHES)):
                raise ValidationError(f"批次 {batch_no} 已存在，不能重复到场")

            # material_id 与 material_no 二选一定位材料，都没有就登记新材料
            material_row = None
            if material_id is not None:
                material_row = self._find_material(int(material_id))
            elif material_no:
                material_row = next(
                    (r for r in store.rows("material") if r.get("材料编号") == material_no),
                    None,
                )
            if material_row is None:
                material_row = {
                    "id": store.next_id("material"),
                    "材料编号": material_no or f"MATE-{store.next_id('material'):04d}",
                    "材料名称": material_name,
                    "材料类别": values.get("category") or "未分类",
                    "规格型号": values.get("spec") or "",
                    "供应商": supplier,
                    "进场日期": values.get("arrival_date") or _now()[:10],
                    "存放地点": values.get("location") or "",
                    "材料状态": "待入库",
                    "status": "在库",
                    "pending": True,
                    "abnormal": False,
                }
                store.rows("material").append(material_row)
            mid = int(material_row["id"])

            node = self._add_node(
                kind=ARRIVAL, batch_no=batch_no, material_id=mid, quantity=quantity,
                event_id=eid, project_id=None, vehicle_id=None,
                summary=f"{material_row['材料名称']} 由 {supplier} 到场 {quantity}",
            )
            return {"node": node, "batch_no": batch_no, "material_id": mid,
                    "balance": 0, "message": "到场已登记，等待仓管入库"}

        return self._run_once(event_id=event_id, event_type="arrival",
                              payload=values, producer=produce)

    def inbound(self, values: dict[str, Any], event_id: str) -> dict[str, Any]:
        def produce(eid: str) -> dict[str, Any]:
            batch_no, location = _require(values, "batch_no", "location")
            batch_no = str(batch_no)
            quantity = values.get("quantity")
            arrival = self._latest_node(batch_no, kinds={ARRIVAL})
            if arrival is None:
                raise NotFoundError(f"批次 {batch_no} 没有到场记录，不能直接入库")
            if self._latest_node(batch_no, kinds={INBOUND, EXCHANGE_IN, MIGRATION}) is not None:
                raise ValidationError(f"批次 {batch_no} 已入库，不能重复入库")
            qty = _as_int(quantity, "quantity") if quantity is not None else int(arrival["quantity"])

            material_id = arrival["material_id"]
            node = self._add_node(
                kind=INBOUND, batch_no=batch_no, material_id=material_id, quantity=qty,
                event_id=eid, summary=f"仓管入库至 {location}，数量 {qty}",
            )
            self._add_edge(parent_id=arrival["id"], child_id=node["id"],
                           relation=EDGE_DERIVED, from_batch=batch_no,
                           to_batch=batch_no, quantity=qty)

            material = self._find_material(material_id)
            batch_row = {
                "id": store.next_id(T_BATCHES),
                "batch_no": batch_no,
                "material_id": material_id,
                "material_no": material.get("材料编号"),
                "material_name": material.get("材料名称"),
                "supplier": material.get("供应商"),
                "location": location,
                "arrival_event": arrival["event_id"],
                "received_qty": float(qty),
                "issued_qty": 0.0,
                "loaded_qty": 0.0,
                "returned_qty": 0.0,
                "balance": float(qty),
                "status": BATCH_IN_STOCK,
                "version": 1,
                "updated_at": _now(),
            }
            store.rows(T_BATCHES).append(batch_row)
            material["存放地点"] = location
            self._writeback_material(material_id)
            return {"node": node, "batch_no": batch_no, "material_id": material_id,
                    "balance": batch_row["balance"], "version": 1,
                    "message": f"批次 {batch_no} 已入库 {qty}"}

        return self._run_once(event_id=event_id, event_type="inbound",
                              payload=values, producer=produce)

    # ---------------------------------------------------------------- 领用

    def requisition(self, values: dict[str, Any], event_id: str) -> dict[str, Any]:
        def produce(eid: str) -> dict[str, Any]:
            batch_no, project_id = _require(values, "batch_no", "project_id")
            batch_no, project_id = str(batch_no), int(project_id)
            quantity = _as_int(values.get("quantity"), "quantity")
            expected_version = values.get("expected_version")
            self._find_project(project_id)

            batch = self._get_batch(batch_no)
            if expected_version is not None and int(expected_version) != int(batch["version"]):
                raise VersionConflictError(
                    f"批次 {batch_no} 已被其他领用更新（当前版本 {batch['version']}），"
                    "请刷新台账后重试"
                )
            if float(batch["balance"]) < quantity:
                raise InsufficientStockError(
                    f"批次 {batch_no} 可用余额 {batch['balance']}，不足领用 {quantity}"
                )

            parent = self._latest_node(
                batch_no, kinds={INBOUND, RETURN, EXCHANGE_IN, TRANSFER_IN, REQUISITION})
            node = self._add_node(
                kind=REQUISITION, batch_no=batch_no, material_id=batch["material_id"],
                quantity=quantity, event_id=eid, project_id=project_id,
                summary=f"工程 {project_id} 领用 {quantity}",
            )
            self._add_edge(parent_id=parent["id"] if parent else node["id"],
                           child_id=node["id"], relation=EDGE_DERIVED,
                           from_batch=batch_no, quantity=quantity, project_id=project_id)

            batch["issued_qty"] = float(batch["issued_qty"]) + quantity
            batch["balance"] = float(batch["balance"]) - quantity
            batch["version"] = int(batch["version"]) + 1
            batch["status"] = BATCH_ISSUED if batch["balance"] > 0 else BATCH_CLOSED
            batch["updated_at"] = _now()

            self._refresh_project_item(project_id, batch_no, batch["material_id"], quantity)
            self._writeback_material(batch["material_id"])
            return {"node": node, "batch_no": batch_no,
                    "material_id": batch["material_id"], "project_id": project_id,
                    "balance": batch["balance"], "version": batch["version"],
                    "message": f"批次 {batch_no} 向工程 {project_id} 领用 {quantity}"}

        return self._run_once(event_id=event_id, event_type="requisition",
                              payload=values, producer=produce)

    # ------------------------------------------------------------ 车辆装载

    def loading(self, values: dict[str, Any], event_id: str) -> dict[str, Any]:
        def produce(eid: str) -> dict[str, Any]:
            batch_no, project_id, vehicle_id = _require(
                values, "batch_no", "project_id", "vehicle_id")
            batch_no, project_id, vehicle_id = str(batch_no), int(project_id), int(vehicle_id)
            quantity = _as_int(values.get("quantity"), "quantity")
            self._find_vehicle(vehicle_id)

            req_node = self._latest_node(
                batch_no,
                kinds={REQUISITION, TRANSFER_IN, LOADING, RETURN},
            )
            if req_node is None or req_node.get("kind") not in (
                    REQUISITION, TRANSFER_IN, RETURN, LOADING):
                raise ValidationError(f"批次 {batch_no} 尚未被工程 {project_id} 领用，无法装载")
            if int(req_node.get("project_id") or 0) != project_id:
                raise ValidationError(
                    f"批次 {batch_no} 当前归属工程 {req_node.get('project_id')}，"
                    f"与目标工程 {project_id} 不符"
                )

            item = next((m for m in store.rows(T_PROJECT_MATERIALS)
                         if m["project_id"] == project_id and m["batch_no"] == batch_no), None)
            outstanding = float(item["outstanding_qty"]) if item else 0.0
            if outstanding < quantity:
                raise InsufficientStockError(
                    f"工程 {project_id} 批次 {batch_no} 待装载仅余 {outstanding}"
                )

            batch = self._get_batch(batch_no)
            node = self._add_node(
                kind=LOADING, batch_no=batch_no, material_id=batch["material_id"],
                quantity=quantity, event_id=eid, project_id=project_id, vehicle_id=vehicle_id,
                summary=f"车辆 {vehicle_id} 装载 {quantity}",
            )
            self._add_edge(parent_id=req_node["id"], child_id=node["id"],
                           relation=EDGE_DERIVED, from_batch=batch_no,
                           quantity=quantity, project_id=project_id)

            batch["loaded_qty"] = float(batch["loaded_qty"]) + quantity
            batch["version"] = int(batch["version"]) + 1
            batch["status"] = BATCH_LOADED if batch["balance"] > 0 else BATCH_CLOSED
            batch["updated_at"] = _now()

            item["loaded_qty"] = float(item["loaded_qty"]) + quantity
            item["outstanding_qty"] = float(item["outstanding_qty"]) - quantity
            item["updated_at"] = _now()

            todo = {
                "id": store.next_id(T_VEHICLE_TODOS),
                "vehicle_id": vehicle_id,
                "project_id": project_id,
                "batch_no": batch_no,
                "material_id": batch["material_id"],
                "quantity": quantity,
                "loaded_qty": 0.0,
                "status": "待装载",
                "node_id": node["id"],
                "event_id": eid,
                "created_at": _now(),
            }
            store.rows(T_VEHICLE_TODOS).append(todo)
            self._writeback_project(project_id)
            self._writeback_material(batch["material_id"])
            self._writeback_vehicle(vehicle_id)
            return {"node": node, "todo": todo, "batch_no": batch_no,
                    "material_id": batch["material_id"], "project_id": project_id,
                    "vehicle_id": vehicle_id, "version": batch["version"],
                    "message": f"批次 {batch_no} 已生成车辆 {vehicle_id} 装载待办"}

        return self._run_once(event_id=event_id, event_type="loading",
                              payload=values, producer=produce)

    def complete_todo(self, values: dict[str, Any], event_id: str) -> dict[str, Any]:
        def produce(eid: str) -> dict[str, Any]:
            todo_id, = _require(values, "todo_id")
            todo = next((t for t in store.rows(T_VEHICLE_TODOS)
                         if int(t["id"]) == int(todo_id)), None)
            if todo is None:
                raise NotFoundError(f"车辆待办 {todo_id} 不存在")
            if todo["status"] == "已完成":
                return {"todo": todo, "message": f"待办 {todo_id} 已完成",
                        "vehicle_id": todo["vehicle_id"]}
            todo["status"] = "已完成"
            todo["loaded_qty"] = todo["quantity"]
            todo["completed_at"] = _now()
            todo["complete_event"] = eid
            self._writeback_vehicle(todo["vehicle_id"])
            return {"todo": todo, "vehicle_id": todo["vehicle_id"],
                    "message": f"车辆待办 {todo_id} 已确认装载完成"}

        return self._run_once(event_id=event_id, event_type="complete_todo",
                              payload=values, producer=produce)

    # ---------------------------------------------------------------- 退料

    def return_material(self, values: dict[str, Any], event_id: str) -> dict[str, Any]:
        def produce(eid: str) -> dict[str, Any]:
            batch_no, project_id = _require(values, "batch_no", "project_id")
            batch_no, project_id = str(batch_no), int(project_id)
            quantity = _as_int(values.get("quantity"), "quantity")
            batch = self._get_batch(batch_no)

            item = next((m for m in store.rows(T_PROJECT_MATERIALS)
                         if m["project_id"] == project_id and m["batch_no"] == batch_no), None)
            held = float(item["received_qty"]) - float(item["returned_qty"]) if item else 0.0
            if held < quantity:
                raise InsufficientStockError(
                    f"工程 {project_id} 持有批次 {batch_no} 仅 {held}，不足退料 {quantity}"
                )

            parent = self._latest_node(batch_no, kinds={REQUISITION, LOADING, TRANSFER_IN})
            node = self._add_node(
                kind=RETURN, batch_no=batch_no, material_id=batch["material_id"],
                quantity=quantity, event_id=eid, project_id=project_id,
                summary=f"工程 {project_id} 退料 {quantity}",
            )
            self._add_edge(parent_id=parent["id"] if parent else node["id"],
                           child_id=node["id"], relation=EDGE_DERIVED,
                           from_batch=batch_no, quantity=quantity, project_id=project_id)

            batch["returned_qty"] = float(batch["returned_qty"]) + quantity
            batch["balance"] = float(batch["balance"]) + quantity
            batch["version"] = int(batch["version"]) + 1
            batch["status"] = BATCH_IN_STOCK
            batch["updated_at"] = _now()

            item["returned_qty"] = float(item["returned_qty"]) + quantity
            item["outstanding_qty"] = max(0.0, float(item["outstanding_qty"]) - quantity)
            item["updated_at"] = _now()

            self._writeback_project(project_id)
            self._writeback_material(batch["material_id"])
            return {"node": node, "batch_no": batch_no, "material_id": batch["material_id"],
                    "project_id": project_id, "balance": batch["balance"],
                    "version": batch["version"],
                    "message": f"批次 {batch_no} 退料 {quantity}，余额回补至 {batch['balance']}"}

        return self._run_once(event_id=event_id, event_type="return_material",
                              payload=values, producer=produce)

    # ---------------------------------------------------------------- 换货

    def exchange(self, values: dict[str, Any], event_id: str) -> dict[str, Any]:
        def produce(eid: str) -> dict[str, Any]:
            old_batch, new_batch, project_id = _require(
                values, "old_batch_no", "new_batch_no", "project_id")
            old_batch, new_batch, project_id = str(old_batch), str(new_batch), int(project_id)
            quantity = _as_int(values.get("quantity"), "quantity")
            if old_batch == new_batch:
                raise ValidationError("换货的新旧批次不能相同")
            if any(b["batch_no"] == new_batch for b in store.rows(T_BATCHES)):
                raise ValidationError(f"新批次 {new_batch} 已存在，应走领用而不是换货")

            old = self._get_batch(old_batch)
            item = next((m for m in store.rows(T_PROJECT_MATERIALS)
                         if m["project_id"] == project_id and m["batch_no"] == old_batch), None)
            held = float(item["received_qty"]) - float(item["returned_qty"]) if item else 0.0
            if held < quantity:
                raise InsufficientStockError(
                    f"工程 {project_id} 持有旧批次 {old_batch} 仅 {held}，不足换货 {quantity}"
                )

            # 旧批次退出
            out_parent = self._latest_node(old_batch, kinds={REQUISITION, LOADING})
            out_node = self._add_node(
                kind=EXCHANGE_OUT, batch_no=old_batch, material_id=old["material_id"],
                quantity=quantity, event_id=eid, project_id=project_id,
                summary=f"工程 {project_id} 换货退出旧批次 {quantity}",
            )
            self._add_edge(parent_id=out_parent["id"] if out_parent else out_node["id"],
                           child_id=out_node["id"], relation=EDGE_REPLACE,
                           from_batch=old_batch, quantity=quantity, project_id=project_id)
            old["version"] = int(old["version"]) + 1
            old["status"] = BATCH_CLOSED
            old["updated_at"] = _now()
            item["returned_qty"] = float(item["returned_qty"]) + quantity
            item["outstanding_qty"] = max(0.0, float(item["outstanding_qty"]) - quantity)
            item["updated_at"] = _now()

            # 新批次补入（沿用同一材料目录，开新批次行）
            in_node = self._add_node(
                kind=EXCHANGE_IN, batch_no=new_batch, material_id=old["material_id"],
                quantity=quantity, event_id=eid, project_id=project_id,
                summary=f"换货新批次 {new_batch} 补入 {quantity}",
            )
            self._add_edge(parent_id=out_node["id"], child_id=in_node["id"],
                           relation=EDGE_REPLACE, from_batch=old_batch,
                           to_batch=new_batch, quantity=quantity, project_id=project_id)
            new_row = {
                "id": store.next_id(T_BATCHES),
                "batch_no": new_batch,
                "material_id": old["material_id"],
                "material_no": old.get("material_no"),
                "material_name": old.get("material_name"),
                "supplier": old.get("supplier"),
                "location": old.get("location"),
                "arrival_event": eid,
                "received_qty": float(quantity),
                "issued_qty": float(quantity),
                "loaded_qty": 0.0,
                "returned_qty": 0.0,
                "balance": 0.0,
                "status": BATCH_ISSUED,
                "version": 1,
                "updated_at": _now(),
                "replaces_batch": old_batch,
            }
            store.rows(T_BATCHES).append(new_row)
            self._refresh_project_item(project_id, new_batch, old["material_id"], quantity)
            self._writeback_material(old["material_id"])
            return {"nodes": [out_node, in_node], "old_batch_no": old_batch,
                    "new_batch_no": new_batch, "material_id": old["material_id"],
                    "project_id": project_id,
                    "message": f"换货完成：{old_batch} → {new_batch}（{quantity}）"}

        return self._run_once(event_id=event_id, event_type="exchange",
                              payload=values, producer=produce)

    # ----------------------------------------------------------- 跨工程调拨

    def transfer(self, values: dict[str, Any], event_id: str) -> dict[str, Any]:
        def produce(eid: str) -> dict[str, Any]:
            batch_no, from_project, to_project = _require(
                values, "batch_no", "from_project_id", "to_project_id")
            batch_no, from_project, to_project = str(batch_no), int(from_project), int(to_project)
            quantity = _as_int(values.get("quantity"), "quantity")
            if from_project == to_project:
                raise ValidationError("调拨的源工程与目标工程不能相同")
            self._find_project(from_project)
            self._find_project(to_project)

            batch = self._get_batch(batch_no)
            src = next((m for m in store.rows(T_PROJECT_MATERIALS)
                        if m["project_id"] == from_project and m["batch_no"] == batch_no), None)
            held = float(src["received_qty"]) - float(src["returned_qty"]) if src else 0.0
            if held < quantity:
                raise InsufficientStockError(
                    f"源工程 {from_project} 持有批次 {batch_no} 仅 {held}，不足调拨 {quantity}"
                )

            out_parent = self._latest_node(batch_no, kinds={REQUISITION, LOADING, TRANSFER_IN})
            out_node = self._add_node(
                kind=TRANSFER_OUT, batch_no=batch_no, material_id=batch["material_id"],
                quantity=quantity, event_id=eid, project_id=from_project,
                summary=f"工程 {from_project} 调出至工程 {to_project}：{quantity}",
                extra={"to_project_id": to_project},
            )
            self._add_edge(parent_id=out_parent["id"] if out_parent else out_node["id"],
                           child_id=out_node["id"], relation=EDGE_TRANSFER,
                           from_batch=batch_no, quantity=quantity, project_id=from_project)
            src["returned_qty"] = float(src["returned_qty"]) + quantity
            src["outstanding_qty"] = max(0.0, float(src["outstanding_qty"]) - quantity)
            src["updated_at"] = _now()

            in_node = self._add_node(
                kind=TRANSFER_IN, batch_no=batch_no, material_id=batch["material_id"],
                quantity=quantity, event_id=eid, project_id=to_project,
                summary=f"工程 {to_project} 接收调拨 {quantity}",
                extra={"from_project_id": from_project},
            )
            self._add_edge(parent_id=out_node["id"], child_id=in_node["id"],
                           relation=EDGE_TRANSFER, from_batch=batch_no,
                           to_batch=batch_no, quantity=quantity, project_id=to_project)
            self._refresh_project_item(to_project, batch_no, batch["material_id"], quantity)

            batch["version"] = int(batch["version"]) + 1
            batch["status"] = BATCH_ISSUED
            batch["updated_at"] = _now()
            self._writeback_project(from_project)
            self._writeback_material(batch["material_id"])
            return {"nodes": [out_node, in_node], "batch_no": batch_no,
                    "material_id": batch["material_id"],
                    "from_project_id": from_project, "to_project_id": to_project,
                    "version": batch["version"],
                    "message": f"批次 {batch_no} 由工程 {from_project} 调拨至 {to_project}"}

        return self._run_once(event_id=event_id, event_type="transfer",
                              payload=values, producer=produce)

    # ------------------------------------------------------- 历史领用存档

    def historical_archive(self, values: dict[str, Any], event_id: str) -> dict[str, Any]:
        """历史领用按原批次存档：只补谱系节点，不改当期库存余额。"""
        def produce(eid: str) -> dict[str, Any]:
            batch_no, project_id, quantity = _require(
                values, "batch_no", "project_id", "quantity")
            batch_no, project_id = str(batch_no), int(project_id)
            quantity = _as_int(quantity, "quantity")
            material_id = values.get("material_id")
            material_id = int(material_id) if material_id is not None else None
            self._find_project(project_id)

            node = self._add_node(
                kind=HISTORICAL, batch_no=batch_no, material_id=material_id,
                quantity=quantity, event_id=eid, project_id=project_id,
                vehicle_id=values.get("vehicle_id"),
                summary=f"历史领用存档：工程 {project_id} 原批次 {batch_no} {quantity}",
                extra={"archived_at": _now()},
            )
            # 存档边：把原批次节点挂到该材料最早的现存批次根上（没有则自环省略）
            root = None
            if material_id is not None:
                for n in store.rows(T_NODES):
                    if n.get("material_id") == material_id and n["id"] != node["id"]:
                        if root is None or n["id"] < root["id"]:
                            root = n
            if root is not None:
                self._add_edge(parent_id=root["id"], child_id=node["id"],
                               relation=EDGE_ARCHIVE, from_batch=root.get("batch_no"),
                               to_batch=batch_no, quantity=quantity, project_id=project_id)
            # 历史领用只补谱系节点，不生成当期工程清单行、不动当期库存
            if material_id is not None:
                self._writeback_material(material_id)
            return {"node": node, "batch_no": batch_no, "project_id": project_id,
                    "message": f"历史领用已按原批次 {batch_no} 存档（不影响当期库存）"}

        return self._run_once(event_id=event_id, event_type="historical_archive",
                              payload=values, producer=produce)

    # ----------------------------------------------------------- 无批次迁移

    def unbatched_materials(self) -> list[dict[str, Any]]:
        """存量无批次材料：从未在任何批次台账行里出现过的材料。"""
        batched_ids = {int(b["material_id"]) for b in store.rows(T_BATCHES)}
        return [dict(r) for r in store.rows("material") if int(r["id"]) not in batched_ids]

    def migrate_unbatched(self, values: dict[str, Any], event_id: str) -> dict[str, Any]:
        """存量无批次材料迁移归位：整批一个事件、一个事务，任一失败回退全部节点。

        每份材料开一个 ``MIG-`` 批次（migration 节点 + 台账行，余额 0，状态已结清），
        历史数量仅用于谱系可追溯，不回冲当期库存。
        """
        targets = values.get("material_ids")
        default_qty = _as_int(values.get("default_quantity") or 1, "default_quantity")

        def produce(eid: str) -> dict[str, Any]:
            candidates = self.unbatched_materials()
            if targets is not None:
                wanted = {int(x) for x in targets}
                candidates = [r for r in candidates if int(r["id"]) in wanted]
                found = {int(r["id"]) for r in candidates}
                missing = sorted(wanted - found)
                if missing:
                    raise ValidationError(
                        f"材料 {('、'.join(map(str, missing)))} 不存在或已归属批次，"
                        "整批迁移已回退"
                    )
            if not candidates:
                raise ValidationError("没有需要迁移的无批次材料")

            created = []
            for row in candidates:
                mid = int(row["id"])
                batch_no = f"MIG-{mid:04d}"
                if any(b["batch_no"] == batch_no for b in store.rows(T_BATCHES)):
                    raise ValidationError(f"迁移批次 {batch_no} 已存在，整批迁移回退")
                qty = default_qty
                node = self._add_node(
                    kind=MIGRATION, batch_no=batch_no, material_id=mid, quantity=qty,
                    event_id=eid,
                    summary=f"存量无批次材料归位：{row.get('材料名称', mid)}",
                    extra={"material_no": row.get("材料编号")},
                )
                # 归位边挂到该材料此前最早的谱系节点（若已在其他批次出现过）
                root = next((n for n in sorted(store.rows(T_NODES), key=lambda x: x["id"])
                             if n.get("material_id") == mid and n["id"] != node["id"]), None)
                if root is not None:
                    self._add_edge(parent_id=root["id"], child_id=node["id"],
                                   relation=EDGE_ARCHIVE, from_batch=root.get("batch_no"),
                                   to_batch=batch_no, quantity=qty)
                store.rows(T_BATCHES).append({
                    "id": store.next_id(T_BATCHES),
                    "batch_no": batch_no,
                    "material_id": mid,
                    "material_no": row.get("材料编号"),
                    "material_name": row.get("材料名称"),
                    "supplier": row.get("供应商"),
                    "location": row.get("存放地点"),
                    "arrival_event": eid,
                    "received_qty": 0.0,
                    "issued_qty": 0.0,
                    "loaded_qty": 0.0,
                    "returned_qty": 0.0,
                    "balance": 0.0,
                    "status": BATCH_CLOSED,
                    "version": 1,
                    "updated_at": _now(),
                    "migrated": True,
                    "historical_qty": float(qty),
                })
                created.append({"node_id": node["id"], "batch_no": batch_no,
                                "material_id": mid})
                self._writeback_material(mid)
            return {"migrated": created, "count": len(created),
                    "message": f"已归位 {len(created)} 份无批次材料"}

        return self._run_once(event_id=event_id, event_type="migration",
                              payload=values or {}, producer=produce)

    # --------------------------------------------------------------- 查询

    def graph(self, *, batch_no: str | None = None, material_id: int | None = None,
              project_id: int | None = None, vehicle_id: int | None = None) -> dict[str, Any]:
        nodes = store.rows(T_NODES)
        edges = store.rows(T_EDGES)

        # 1) 批次维度（含历史存档/迁移等「只有节点」的批次）
        batch_set = {n["batch_no"] for n in nodes if n.get("batch_no")}
        batch_set |= {b["batch_no"] for b in store.rows(T_BATCHES)}
        if batch_no is not None:
            batch_set &= {str(batch_no)}
        if material_id is not None:
            mat_batches = {n["batch_no"] for n in nodes
                           if n.get("material_id") == int(material_id)}
            mat_batches |= {b["batch_no"] for b in store.rows(T_BATCHES)
                            if int(b["material_id"]) == int(material_id)}
            batch_set &= mat_batches

        # 2) 工程/车辆维度：只聚焦锚点节点所在批次
        anchor_ids: set[int] | None = None
        if project_id is not None:
            batches = {n["batch_no"] for n in nodes
                       if n.get("project_id") == int(project_id)}
            batch_set &= batches
            anchor_ids = {n["id"] for n in nodes
                          if n.get("project_id") == int(project_id)}
        if vehicle_id is not None:
            batches = {n["batch_no"] for n in nodes
                       if n.get("vehicle_id") == int(vehicle_id)}
            batch_set &= batches
            v_ids = {n["id"] for n in nodes if n.get("vehicle_id") == int(vehicle_id)}
            anchor_ids = v_ids if anchor_ids is None else anchor_ids | v_ids

        # 3) 沿跨批次边（换货 replace）把对端节点也带出来，保证回看不断链
        visible_ids = {n["id"] for n in nodes if n.get("batch_no") in batch_set}
        cross_ids: set[int] = set()
        by_id = {n["id"]: n for n in nodes}
        for e in edges:
            if e["relation"] == EDGE_REPLACE and (
                    e["parent_id"] in visible_ids or e["child_id"] in visible_ids):
                cross_ids.add(e["parent_id"])
                cross_ids.add(e["child_id"])
        visible_ids |= cross_ids

        visible = [dict(n) for n in nodes if n["id"] in visible_ids]
        visible_edges = [dict(e) for e in edges
                         if e["parent_id"] in visible_ids and e["child_id"] in visible_ids]
        final_batches = {n["batch_no"] for n in visible if n.get("batch_no")}
        batch_rows = [dict(b) for b in store.rows(T_BATCHES)
                      if b["batch_no"] in final_batches]
        return {"nodes": visible, "edges": visible_edges, "batches": batch_rows,
                "anchor_node_ids": sorted(anchor_ids or [])}

    def list_batches(self, *, material_id: int | None = None,
                     project_id: int | None = None) -> list[dict[str, Any]]:
        batches = [dict(b) for b in store.rows(T_BATCHES)]
        if material_id is not None:
            batches = [b for b in batches if int(b["material_id"]) == int(material_id)]
        if project_id is not None:
            linked = {m["batch_no"] for m in store.rows(T_PROJECT_MATERIALS)
                      if int(m["project_id"]) == int(project_id)}
            batches = [b for b in batches if b["batch_no"] in linked]
        return batches

    def ledger(self) -> list[dict[str, Any]]:
        return [dict(b) for b in store.rows(T_BATCHES)]

    def project_materials(self, project_id: int | None = None) -> list[dict[str, Any]]:
        rows = [dict(m) for m in store.rows(T_PROJECT_MATERIALS)]
        if project_id is not None:
            rows = [m for m in rows if int(m["project_id"]) == int(project_id)]
        for m in rows:
            material = store.find("material", int(m["material_id"]))
            project = store.find("project", int(m["project_id"]))
            m["material_name"] = material.get("材料名称") if material else None
            m["material_no"] = material.get("材料编号") if material else None
            m["project_name"] = project.get("工程名称") if project else None
        return rows

    def vehicle_todos(self, vehicle_id: int | None = None,
                      status: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(t) for t in store.rows(T_VEHICLE_TODOS)]
        if vehicle_id is not None:
            rows = [t for t in rows if int(t["vehicle_id"]) == int(vehicle_id)]
        if status:
            rows = [t for t in rows if t["status"] == status]
        for t in rows:
            vehicle = store.find("vehicle", int(t["vehicle_id"]))
            t["vehicle_no"] = vehicle.get("车牌号") if vehicle else None
        return rows

    def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(e) for e in store.rows(T_EVENTS)]
        if event_type:
            rows = [e for e in rows if e["event_type"] == event_type]
        return rows

    def replay_events(self, *, since_id: int = 0) -> dict[str, Any]:
        """消息重放：把事件日志按序再投递一遍。

        处理器天然按 event_id 幂等——已落库的事件直接返回首次结果并标记 replayed，
        因此重放不会重复扣减库存或重复生成节点。
        """
        replayed = []
        for row in sorted(store.rows(T_EVENTS), key=lambda r: r["id"]):
            if int(row["id"]) < since_id:
                continue
            handler = EVENT_HANDLERS.get(row["event_type"])
            if handler is None:
                continue
            result = handler(self, row["payload"], row["event_id"])
            row["replayed"] = True
            replayed.append({"event_id": row["event_id"],
                             "event_type": row["event_type"],
                             "idempotent": result.get("idempotent")})
        return {"replayed": len(replayed), "details": replayed}

    def verify_consistency(self) -> dict[str, Any]:
        """谱系与库存事件逻辑相符性自检。"""
        problems: list[str] = []

        # 1. 台账余额 = 入库 - 领用 + 退料（装载占用不扣余额，换货/调拨发生在已领用部分）
        nodes = store.rows(T_NODES)
        for b in store.rows(T_BATCHES):
            expected_balance = (
                float(b["received_qty"]) + float(b["returned_qty"]) - float(b["issued_qty"])
            )
            if abs(expected_balance - float(b["balance"])) > 1e-9:
                problems.append(
                    f"批次 {b['batch_no']} 余额不一致：台账 {b['balance']}，"
                    f"事件汇总应为 {expected_balance}"
                )

            # 2. 每个台账批次必须能在谱系里找到归依节点
            kinds = {n["kind"] for n in nodes if n.get("batch_no") == b["batch_no"]}
            if not (kinds & {INBOUND, EXCHANGE_IN, MIGRATION}):
                problems.append(f"批次 {b['batch_no']} 缺少入库/换货入/迁移归位节点")

        # 3. 每条边的父子节点都存在
        node_ids = {n["id"] for n in nodes}
        for e in store.rows(T_EDGES):
            if e["parent_id"] not in node_ids or e["child_id"] not in node_ids:
                problems.append(f"边 {e['id']} 引用了不存在的节点（断链）")

        # 4. 工程清单汇总额 == 当期领用/装载/退料事件数量（HISTORICAL 仅存档不进清单）
        for m in store.rows(T_PROJECT_MATERIALS):
            req = sum(float(n["quantity"]) for n in nodes
                      if n.get("batch_no") == m["batch_no"]
                      and n.get("project_id") == m["project_id"]
                      and n.get("kind") in {REQUISITION, TRANSFER_IN, EXCHANGE_IN})
            ret = sum(float(n["quantity"]) for n in nodes
                      if n.get("batch_no") == m["batch_no"]
                      and n.get("project_id") == m["project_id"]
                      and n.get("kind") in {RETURN, EXCHANGE_OUT, TRANSFER_OUT})
            if abs(req - float(m["received_qty"])) > 1e-9:
                problems.append(
                    f"工程 {m['project_id']} 批次 {m['batch_no']} 领用汇总 "
                    f"{m['received_qty']} 与谱系事件 {req} 不符"
                )
            if abs(ret - float(m["returned_qty"])) > 1e-9:
                problems.append(
                    f"工程 {m['project_id']} 批次 {m['batch_no']} 退出汇总 "
                    f"{m['returned_qty']} 与谱系事件 {ret} 不符"
                )

        # 5. 回写字段与清单一致
        for p in store.rows("project"):
            items = [m for m in store.rows(T_PROJECT_MATERIALS)
                     if m["project_id"] == p["id"]]
            if items:
                outstanding = sum(float(m["outstanding_qty"]) for m in items)
                if abs(float(p.get("待装载数量", 0)) - outstanding) > 1e-9:
                    problems.append(
                        f"工程 {p['id']} 回写待装载数量 {p.get('待装载数量')} 与清单 {outstanding} 不符"
                    )

        # 6. 事件幂等：event_id 唯一
        ids = [e["event_id"] for e in store.rows(T_EVENTS)]
        if len(ids) != len(set(ids)):
            problems.append("事件日志存在重复 event_id，幂等键被破坏")

        return {"ok": not problems, "problems": problems,
                "checked_batches": len(store.rows(T_BATCHES)),
                "checked_nodes": len(nodes),
                "checked_events": len(store.rows(T_EVENTS))}


# 事件类型 -> 处理器，供消息重放按日志重新投递
EVENT_HANDLERS = {
    "arrival": LineageService.arrival,
    "inbound": LineageService.inbound,
    "requisition": LineageService.requisition,
    "loading": LineageService.loading,
    "return_material": LineageService.return_material,
    "exchange": LineageService.exchange,
    "transfer": LineageService.transfer,
    "historical_archive": LineageService.historical_archive,
    "migration": LineageService.migrate_unbatched,
    "complete_todo": LineageService.complete_todo,
}


def bootstrap_demo() -> dict[str, Any]:
    """引导一条可浏览的示例谱系（幂等）。

    * B-2026-0901-A：到场→入库→领用(工程1)→装载(车辆1)→确认完成；
    * B-2026-0902-B：到场→入库→领用(工程2)→退料；
    * 历史领用按原批次 HIST-2026-08 存档（不动当期库存）；
    * 种子材料 3 做一次无批次迁移归位；材料 2 保持无批次，便于页面演示迁移。
    """
    svc = lineage_service
    if any(n.get("kind") == ARRIVAL for n in svc.graph()["nodes"]):
        return {"ok": True, "bootstrapped": False, "message": "示例谱系已存在，跳过引导"}

    eid = "seed"
    with store.transaction():
        svc.arrival({
            "material_id": 1, "batch_no": "B-2026-0901-A",
            "material_name": "养护材料样例1", "supplier": "养护材料样例1",
            "quantity": 100,
        }, f"{eid}-arrival-a")
        svc.inbound({"batch_no": "B-2026-0901-A", "location": "1号库房", "quantity": 100},
                    f"{eid}-inbound-a")
        svc.requisition({"batch_no": "B-2026-0901-A", "project_id": 1,
                         "quantity": 40, "expected_version": 1}, f"{eid}-req-a")
        svc.loading({"batch_no": "B-2026-0901-A", "project_id": 1,
                     "vehicle_id": 1, "quantity": 40}, f"{eid}-load-a")
        todo = next(t for t in store.rows(T_VEHICLE_TODOS) if t["status"] == "待装载")
        svc.complete_todo({"todo_id": todo["id"]}, f"{eid}-todo-a")

        svc.arrival({
            "material_id": 2, "batch_no": "B-2026-0902-B",
            "material_name": "养护材料样例2", "supplier": "养护材料样例2",
            "quantity": 60,
        }, f"{eid}-arrival-b")
        svc.inbound({"batch_no": "B-2026-0902-B", "location": "2号库房", "quantity": 60},
                    f"{eid}-inbound-b")
        svc.requisition({"batch_no": "B-2026-0902-B", "project_id": 2,
                         "quantity": 30, "expected_version": 1}, f"{eid}-req-b")
        svc.return_material({"batch_no": "B-2026-0902-B", "project_id": 2,
                             "quantity": 10}, f"{eid}-return-b")

        svc.historical_archive({
            "batch_no": "HIST-2026-08", "project_id": 3, "material_id": 1,
            "quantity": 25, "vehicle_id": 2,
        }, f"{eid}-historical")

        svc.migrate_unbatched({"material_ids": [3]}, f"{eid}-migration")

    return {"ok": True, "bootstrapped": True, "message": "示例谱系已引导"}


lineage_service = LineageService()
