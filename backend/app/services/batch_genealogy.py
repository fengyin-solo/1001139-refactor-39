"""材料批次谱系：把供应商到场、仓管入库、工程领用到车辆装载串成可回看的批次链。

旧流程靠前置件拼接字段还原批次，回看经常断链；这里把每一次流转落成谱系节点，
节点与库存台账在同一临界区内写入，保证谱系与库存事件逻辑相符：

- 消息重放幂等：每个事件携带 event_id，已受理的事件重放时直接返回首次结果；
- 并发领用版本门闩：工程领用必须携带 expected_version，与台账批次版本不符即拒绝；
- 迁移整批回退：存量无批次材料迁移时任一节点失败，整批节点与台账一并回滚。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import Store, store

MODULE_MATERIAL = "material"
MODULE_PROJECT = "project"
MODULE_VEHICLE = "vehicle"
TABLE_NODE = "_batch_node"
TABLE_EVENT = "_batch_event"
TABLE_ARCHIVE = "_issue_archive"

EVENT_TYPES = ["供应商到场", "仓管入库", "工程领用", "车辆装载", "退料", "换货", "跨工程调拨"]
MIGRATION_EVENT = "迁移归位"

# 各事件允许的前置节点：前置缺失即判定谱系与库存事件不相符，直接拦截
PARENT_RULES: dict[str, list[str]] = {
    "供应商到场": [],
    "仓管入库": ["供应商到场"],
    "工程领用": ["仓管入库", "换货", MIGRATION_EVENT],
    "车辆装载": ["工程领用"],
    "退料": ["工程领用"],
    "换货": ["仓管入库", "退料"],
    "跨工程调拨": ["工程领用"],
}

# 会改动库存台账、需要推进批次版本的事件
LEDGER_EVENTS = {"仓管入库", "工程领用", "退料", "换货", "跨工程调拨", MIGRATION_EVENT}


class GenealogyError(ValueError):
    """谱系校验失败：消息直接回给调用方，不落任何节点。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise GenealogyError(f"字段 {field} 需要整数，当前值「{value}」无法识别") from None


class BatchGenealogyService:
    """批次谱系业务规则：事件校验、幂等受理、版本门闩与回写都收在这里。"""

    def __init__(self, store_obj: Store | None = None) -> None:
        self._store = store_obj or store
        self._lock = threading.Lock()

    # ---------- 可浏览的谱系图 ----------

    def list_nodes(
        self,
        *,
        keyword: str | None = None,
        event_type: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(self._nodes())
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("batch_no", "")) or keyword in str(row.get("material_code", ""))
            ]
        if event_type:
            rows = [row for row in rows if row.get("event_type") == event_type]
        rows.sort(key=lambda row: (str(row.get("occurred_at", "")), str(row.get("node_id", ""))))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def batch_tree(self, batch_no: str) -> dict[str, Any] | None:
        """单批次谱系图：本批次节点 + 跨批次父节点（换货、调拨会跨链），保证回看不断链。"""
        nodes = [node for node in self._nodes() if node.get("batch_no") == batch_no]
        if not nodes:
            return None
        parent_ids = {pid for node in nodes for pid in node.get("parent_ids", [])}
        parents = [
            node
            for node in self._nodes()
            if node.get("node_id") in parent_ids and node.get("batch_no") != batch_no
        ]
        graph_nodes = sorted(
            nodes + parents, key=lambda row: (str(row.get("occurred_at", "")), str(row.get("node_id", "")))
        )
        edges = [
            {"from": pid, "to": node["node_id"]}
            for node in graph_nodes
            for pid in node.get("parent_ids", [])
        ]
        return {"batch_no": batch_no, "nodes": graph_nodes, "edges": edges}

    def list_archive(
        self,
        *,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """历史领用存档：按原批次留存，退料、换货、调拨都不回写这里。"""
        rows = list(self._store.rows(TABLE_ARCHIVE))
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("batch_no", "")) or keyword in str(row.get("material_code", ""))
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def writeback_view(self) -> dict[str, Any]:
        """批次结论回写视图：库存台账、工程清单、车辆待办三处一起看。"""
        ledger = [
            {
                "id": row.get("id"),
                "材料编号": row.get("材料编号"),
                "材料名称": row.get("材料名称"),
                "当前批次": row.get("当前批次"),
                "库存数量": row.get("库存数量"),
                "批次版本": row.get("批次版本"),
                "批次结论": row.get("批次结论"),
            }
            for row in self._store.rows(MODULE_MATERIAL)
        ]
        projects = [
            {
                "id": row.get("id"),
                "工程编号": row.get("工程编号"),
                "工程名称": row.get("工程名称"),
                "材料清单": row.get("材料清单", []),
            }
            for row in self._store.rows(MODULE_PROJECT)
        ]
        vehicles = [
            {
                "id": row.get("id"),
                "车辆编号": row.get("车辆编号"),
                "车牌号": row.get("车牌号"),
                "装载待办": row.get("装载待办", []),
            }
            for row in self._store.rows(MODULE_VEHICLE)
        ]
        return {"ledger": ledger, "projects": projects, "vehicles": vehicles}

    # ---------- 谱系事件：幂等受理 + 版本门闩 ----------

    def append_event(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        event_id = str(values.get("event_id") or "").strip()
        if not event_id:
            return None, "缺少事件标识 event_id，无法保证消息重放幂等"
        with self._lock:
            replayed = self._find_event(event_id)
            if replayed is not None:
                return replayed, f"事件 {event_id} 已受理，重放不再重复写入"
            event_type = str(values.get("event_type") or "").strip()
            if event_type not in EVENT_TYPES:
                return None, f"事件类型「{event_type}」不在谱系事件范围内：{'、'.join(EVENT_TYPES)}"
            try:
                node, message = self._apply(event_type, values)
            except GenealogyError as exc:
                return None, str(exc)
            self._record_event(event_id, message, node_id=node["node_id"])
            return node, message

    # ---------- 存量无批次材料迁移：整批回退 ----------

    def migrate_legacy(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        migration_id = str(values.get("migration_id") or "").strip() or f"MIG-{_now()}"
        default_quantity = _to_int(values.get("default_quantity", 100), "default_quantity")
        if default_quantity < 0:
            return None, "默认库存数量不能为负数"
        with self._lock:
            replayed = self._find_event(migration_id)
            if replayed is not None:
                return replayed, f"迁移 {migration_id} 已受理，重放不再重复写入"
            legacy = [
                row
                for row in self._store.rows(MODULE_MATERIAL)
                if not str(row.get("当前批次") or "").strip()
            ]
            if not legacy:
                return {"migration_id": migration_id, "migrated": 0, "nodes": []}, "没有待迁移的存量材料"
            created: list[dict[str, Any]] = []
            touched: list[tuple[dict[str, Any], dict[str, Any]]] = []
            try:
                for row in legacy:
                    node, changes = self._build_migration(row, migration_id, default_quantity)
                    self._nodes().append(node)
                    created.append(node)
                    touched.append((row, dict(row)))
                    row.update(changes)
            except Exception as exc:  # 任一节点失败：整批节点与台账一并回滚
                for node in created:
                    if node in self._nodes():
                        self._nodes().remove(node)
                for row, snapshot in touched:
                    row.clear()
                    row.update(snapshot)
                return None, f"迁移未成功，已整批回退 {len(created)} 个谱系节点：{exc}"
            summary = {
                "migration_id": migration_id,
                "migrated": len(created),
                "nodes": [node["node_id"] for node in created],
            }
            self._record_event(migration_id, "迁移归位完成", result=summary)
            return summary, f"迁移归位完成：{len(created)} 条存量无批次材料已归位"

    def _build_migration(
        self, row: dict[str, Any], migration_id: str, default_quantity: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """为一条存量材料构造迁移节点与台账变更；抽成独立方法便于校验与回滚。"""
        material_code = str(row.get("材料编号") or "").strip()
        if not material_code:
            raise GenealogyError(f"材料记录 {row.get('id')} 缺少材料编号，无法生成归位批次")
        batch_no = f"{material_code}-LEGACY"
        if any(node.get("batch_no") == batch_no for node in self._nodes()):
            raise GenealogyError(f"归位批次 {batch_no} 已存在，疑似重复迁移")
        quantity = _to_int(row.get("库存数量", default_quantity) or default_quantity, "库存数量")
        version = max(_to_int(row.get("批次版本", 0) or 0, "批次版本"), 1)
        conclusion = f"存量无批次材料迁移归位为批次 {batch_no}，库存 {quantity}"
        node = {
            "node_id": self._next_node_id(),
            "batch_no": batch_no,
            "event_type": MIGRATION_EVENT,
            "material_id": row.get("id"),
            "material_code": material_code,
            "project_id": None,
            "vehicle_id": None,
            "quantity": quantity,
            "parent_ids": [],
            "event_id": migration_id,
            "source_batch_no": None,
            "migration_id": migration_id,
            "occurred_at": _now(),
            "conclusion": conclusion,
        }
        changes = {"当前批次": batch_no, "批次版本": version, "批次结论": conclusion}
        if row.get("库存数量") is None:
            changes["库存数量"] = quantity
        return node, changes

    # ---------- 事件应用：校验通过后节点与台账同事务写入 ----------

    def _apply(self, event_type: str, values: dict[str, Any]) -> tuple[dict[str, Any], str]:
        material = self._require_material(values.get("material_id"))
        batch_no = str(values.get("batch_no") or "").strip()
        if not batch_no:
            raise GenealogyError("缺少批次号 batch_no，谱系节点无法归链")
        quantity = _to_int(values.get("quantity"), "quantity")
        if quantity <= 0:
            raise GenealogyError("数量必须为正整数")
        parent = self._require_parent(event_type, batch_no)

        handler = {
            "供应商到场": self._apply_arrival,
            "仓管入库": self._apply_inbound,
            "工程领用": self._apply_issue,
            "车辆装载": self._apply_loading,
            "退料": self._apply_return,
            "换货": self._apply_exchange,
            "跨工程调拨": self._apply_transfer,
        }[event_type]
        return handler(material, batch_no, quantity, parent, values)

    def _apply_arrival(self, material, batch_no, quantity, parent, values):
        if any(node.get("batch_no") == batch_no for node in self._nodes()):
            raise GenealogyError(f"批次 {batch_no} 已存在谱系节点，不能重复到场")
        conclusion = f"批次 {batch_no} 已由供应商送达 {quantity}，待仓管入库"
        node = self._new_node(values, material, batch_no, "供应商到场", quantity, [], conclusion)
        self._transact(node, [(material, {"批次结论": conclusion})], [])
        return node, f"批次 {batch_no} 供应商到场已登记"

    def _apply_inbound(self, material, batch_no, quantity, parent, values):
        stock = _to_int(material.get("库存数量", 0) or 0, "库存数量") + quantity
        version = _to_int(material.get("批次版本", 0) or 0, "批次版本") + 1
        conclusion = f"批次 {batch_no} 已入库 {quantity}，库存余量 {stock}"
        node = self._new_node(values, material, batch_no, "仓管入库", quantity, [parent["node_id"]], conclusion)
        changes = {"库存数量": stock, "当前批次": batch_no, "批次版本": version, "批次结论": conclusion}
        self._transact(node, [(material, changes)], [])
        return node, f"批次 {batch_no} 仓管入库完成"

    def _apply_issue(self, material, batch_no, quantity, parent, values):
        project = self._require_project(values.get("project_id"))
        if str(material.get("当前批次") or "") != batch_no:
            raise GenealogyError(f"台账当前批次为「{material.get('当前批次') or '无'}」，与领用批次 {batch_no} 不相符")
        expected = values.get("expected_version")
        if expected is None:
            raise GenealogyError("工程领用必须携带版本门闩 expected_version")
        version = _to_int(material.get("批次版本", 0) or 0, "批次版本")
        if _to_int(expected, "expected_version") != version:
            raise GenealogyError(f"版本门闩冲突：台账批次版本 {version} 与提交版本 {expected} 不一致，请刷新后重试")
        stock = _to_int(material.get("库存数量", 0) or 0, "库存数量")
        if quantity > stock:
            raise GenealogyError(f"库存余量 {stock} 不足，无法领用 {quantity}")
        remaining = stock - quantity
        conclusion = f"批次 {batch_no} 已领用 {quantity} 至 {project.get('工程编号')}，库存余量 {remaining}"
        node = self._new_node(
            values, material, batch_no, "工程领用", quantity, [parent["node_id"]], conclusion,
            project_id=project.get("id"),
        )
        ledger = {"库存数量": remaining, "批次版本": version + 1, "批次结论": conclusion}
        manifest = list(project.get("材料清单", [])) + [self._manifest_line(node, project, "工程领用", quantity, conclusion)]
        archive = {
            "archive_id": self._next_archive_id(),
            "node_id": node["node_id"],
            "batch_no": batch_no,
            "material_code": material.get("材料编号"),
            "project_code": project.get("工程编号"),
            "quantity": quantity,
            "issued_at": node["occurred_at"],
            "conclusion": f"历史领用按原批次 {batch_no} 存档，后续退料换货不改写本记录",
        }
        self._transact(node, [(material, ledger), (project, {"材料清单": manifest})], [(TABLE_ARCHIVE, archive)])
        return node, f"批次 {batch_no} 工程领用完成"

    def _apply_loading(self, material, batch_no, quantity, parent, values):
        vehicle = self._require_vehicle(values.get("vehicle_id"))
        outstanding = self._project_outstanding(batch_no)
        if quantity > outstanding:
            raise GenealogyError(f"批次 {batch_no} 在工程侧余量 {outstanding}，不足以装载 {quantity}")
        conclusion = f"批次 {batch_no} 已安排装载 {quantity} 至车辆 {vehicle.get('车辆编号')}"
        node = self._new_node(
            values, material, batch_no, "车辆装载", quantity, [parent["node_id"]], conclusion,
            project_id=parent.get("project_id"), vehicle_id=vehicle.get("id"),
        )
        todos = list(vehicle.get("装载待办", [])) + [{
            "批次号": batch_no,
            "材料编号": material.get("材料编号"),
            "数量": quantity,
            "状态": "待装载",
            "结论": conclusion,
            "时间": node["occurred_at"],
        }]
        self._transact(node, [(vehicle, {"装载待办": todos})], [])
        return node, f"批次 {batch_no} 车辆装载待办已下发"

    def _apply_return(self, material, batch_no, quantity, parent, values):
        outstanding = self._project_outstanding(batch_no)
        if quantity > outstanding:
            raise GenealogyError(f"批次 {batch_no} 在工程侧余量 {outstanding}，不足以退料 {quantity}")
        project = self._require_project(parent.get("project_id"))
        stock = _to_int(material.get("库存数量", 0) or 0, "库存数量") + quantity
        version = _to_int(material.get("批次版本", 0) or 0, "批次版本") + 1
        conclusion = f"批次 {batch_no} 退料 {quantity} 回库，库存余量 {stock}"
        node = self._new_node(
            values, material, batch_no, "退料", quantity, [parent["node_id"]], conclusion,
            project_id=project.get("id"),
        )
        ledger = {"库存数量": stock, "批次版本": version, "批次结论": conclusion}
        manifest = list(project.get("材料清单", [])) + [self._manifest_line(node, project, "退料", quantity, conclusion)]
        self._transact(node, [(material, ledger), (project, {"材料清单": manifest})], [])
        return node, f"批次 {batch_no} 退料回库完成"

    def _apply_exchange(self, material, batch_no, quantity, parent, values):
        if str(material.get("当前批次") or "") != batch_no:
            raise GenealogyError(f"台账当前批次为「{material.get('当前批次') or '无'}」，与换货批次 {batch_no} 不相符")
        new_batch_no = str(values.get("new_batch_no") or "").strip()
        if not new_batch_no:
            raise GenealogyError("换货必须给出新批次号 new_batch_no")
        if any(node.get("batch_no") == new_batch_no for node in self._nodes()):
            raise GenealogyError(f"新批次 {new_batch_no} 已存在谱系节点，不能重复换货")
        version = _to_int(material.get("批次版本", 0) or 0, "批次版本") + 1
        conclusion = f"批次 {batch_no} 已换货为新批次 {new_batch_no}，数量 {quantity}"
        node = self._new_node(
            values, material, new_batch_no, "换货", quantity, [parent["node_id"]], conclusion,
            source_batch_no=batch_no,
        )
        changes = {"当前批次": new_batch_no, "批次版本": version, "批次结论": conclusion}
        self._transact(node, [(material, changes)], [])
        return node, f"批次 {batch_no} 已换货为 {new_batch_no}"

    def _apply_transfer(self, material, batch_no, quantity, parent, values):
        target = self._require_project(values.get("target_project_id"))
        source = self._require_project(parent.get("project_id"))
        if target.get("id") == source.get("id"):
            raise GenealogyError("跨工程调拨的目标工程不能与来源工程相同")
        outstanding = self._project_outstanding(batch_no)
        if quantity > outstanding:
            raise GenealogyError(f"批次 {batch_no} 在工程侧余量 {outstanding}，不足以调拨 {quantity}")
        version = _to_int(material.get("批次版本", 0) or 0, "批次版本") + 1
        conclusion = f"批次 {batch_no} 跨工程调拨 {quantity}：{source.get('工程编号')} → {target.get('工程编号')}"
        node = self._new_node(
            values, material, batch_no, "跨工程调拨", quantity, [parent["node_id"]], conclusion,
            project_id=target.get("id"),
        )
        ledger = {"批次版本": version, "批次结论": conclusion}
        source_manifest = list(source.get("材料清单", [])) + [
            self._manifest_line(node, source, "调拨出", quantity, conclusion)
        ]
        target_manifest = list(target.get("材料清单", [])) + [
            self._manifest_line(node, target, "调拨入", quantity, conclusion)
        ]
        self._transact(
            node,
            [(material, ledger), (source, {"材料清单": source_manifest}), (target, {"材料清单": target_manifest})],
            [],
        )
        return node, f"批次 {batch_no} 跨工程调拨完成"

    # ---------- 内部：校验、事务与幂等日志 ----------

    def _transact(self, node, mutations, appends) -> None:
        """谱系节点与库存事件在同一临界区写入；任一步失败整体回滚，保证两边逻辑相符。"""
        snapshots: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen: set[int] = set()
        for row, _changes in mutations:
            if id(row) not in seen:
                seen.add(id(row))
                snapshots.append((row, dict(row)))
        appended: list[tuple[str, dict[str, Any]]] = []
        try:
            self._nodes().append(node)
            for row, changes in mutations:
                row.update(changes)
            for table, record in appends:
                self._store.rows(table).append(record)
                appended.append((table, record))
        except Exception:
            if node in self._nodes():
                self._nodes().remove(node)
            for row, snapshot in snapshots:
                row.clear()
                row.update(snapshot)
            for table, record in appends:
                if record in self._store.rows(table):
                    self._store.rows(table).remove(record)
            raise

    def _require_parent(self, event_type: str, batch_no: str) -> dict[str, Any] | None:
        allowed = PARENT_RULES[event_type]
        if not allowed:
            return None
        candidates = [
            node
            for node in self._nodes()
            if node.get("batch_no") == batch_no and node.get("event_type") in allowed
        ]
        if not candidates:
            raise GenealogyError(
                f"批次 {batch_no} 缺少前置环节「{'、'.join(allowed)}」，谱系与库存事件不相符，已拦截"
            )
        candidates.sort(key=lambda node: str(node.get("occurred_at", "")))
        return candidates[-1]

    def _project_outstanding(self, batch_no: str) -> int:
        """批次在工程侧的余量：领用 - 退料 - 调拨，用于退料、调拨、装载的上限校验。"""
        issued = returned = transferred = 0
        for node in self._nodes():
            if node.get("batch_no") != batch_no:
                continue
            if node.get("event_type") == "工程领用":
                issued += int(node.get("quantity", 0))
            elif node.get("event_type") == "退料":
                returned += int(node.get("quantity", 0))
            elif node.get("event_type") == "跨工程调拨":
                transferred += int(node.get("quantity", 0))
        return issued - returned - transferred

    def _manifest_line(self, node, project, action, quantity, conclusion) -> dict[str, Any]:
        return {
            "批次号": node["batch_no"],
            "材料编号": node.get("material_code"),
            "动作": action,
            "数量": quantity,
            "结论": conclusion,
            "时间": node["occurred_at"],
        }

    def _new_node(
        self, values, material, batch_no, event_type, quantity, parent_ids, conclusion,
        project_id=None, vehicle_id=None, source_batch_no=None,
    ) -> dict[str, Any]:
        return {
            "node_id": self._next_node_id(),
            "batch_no": batch_no,
            "event_type": event_type,
            "material_id": material.get("id"),
            "material_code": material.get("材料编号"),
            "project_id": project_id if project_id is not None else values.get("project_id"),
            "vehicle_id": vehicle_id if vehicle_id is not None else values.get("vehicle_id"),
            "quantity": quantity,
            "parent_ids": list(parent_ids),
            "event_id": values.get("event_id"),
            "source_batch_no": source_batch_no,
            "migration_id": None,
            "occurred_at": str(values.get("occurred_at") or "").strip() or _now(),
            "conclusion": conclusion,
        }

    def _require_material(self, material_id: Any) -> dict[str, Any]:
        row = self._store.find(MODULE_MATERIAL, _to_int(material_id, "material_id"))
        if row is None:
            raise GenealogyError(f"养护材料 {material_id} 不存在或已归档")
        return row

    def _require_project(self, project_id: Any) -> dict[str, Any]:
        row = self._store.find(MODULE_PROJECT, _to_int(project_id, "project_id"))
        if row is None:
            raise GenealogyError(f"养护工程 {project_id} 不存在或已归档")
        return row

    def _require_vehicle(self, vehicle_id: Any) -> dict[str, Any]:
        row = self._store.find(MODULE_VEHICLE, _to_int(vehicle_id, "vehicle_id"))
        if row is None:
            raise GenealogyError(f"养护车辆 {vehicle_id} 不存在或已归档")
        return row

    def _nodes(self) -> list[dict[str, Any]]:
        return self._store.rows(TABLE_NODE)

    def _next_node_id(self) -> str:
        serial = max(
            (int(str(node.get("node_id", "BN-0")).split("-")[-1]) for node in self._nodes()),
            default=0,
        )
        return f"BN-{serial + 1:06d}"

    def _next_archive_id(self) -> str:
        serial = max(
            (int(str(row.get("archive_id", "ARC-0")).split("-")[-1]) for row in self._store.rows(TABLE_ARCHIVE)),
            default=0,
        )
        return f"ARC-{serial + 1:06d}"

    def _find_event(self, event_id: str) -> dict[str, Any] | None:
        for row in self._store.rows(TABLE_EVENT):
            if row.get("event_id") != event_id:
                continue
            if row.get("result") is not None:
                return row["result"]
            for node in self._nodes():
                if node.get("node_id") == row.get("node_id"):
                    return node
            return None
        return None

    def _record_event(
        self, event_id: str, message: str, node_id: str | None = None, result: dict[str, Any] | None = None
    ) -> None:
        self._store.rows(TABLE_EVENT).append(
            {"event_id": event_id, "node_id": node_id, "result": result, "message": message, "recorded_at": _now()}
        )
