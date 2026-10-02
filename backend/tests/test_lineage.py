"""批次谱系域测试：链路、回写、幂等、版本门闩、迁移回退、消息重放。"""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.seed import SEED_ROWS
from app.store import store


@pytest.fixture()
def client():
    """每个用例一份全新内存仓库，并清空谱系内部表后重新引导。"""
    from app.lineage import service as svc_mod
    saved = copy.deepcopy(store._tables)
    store._tables = {name: [dict(r) for r in rows] for name, rows in SEED_ROWS.items()}
    try:
        svc_mod.bootstrap_demo()
        from app.main import app
        with TestClient(app) as c:
            yield c
    finally:
        store._tables = saved


# --------------------------------------------------------------- 浏览与回写

def test_graph_is_browseable_and_writeback(client: TestClient):
    graph = client.get("/api/lineage/graph").json()
    kinds = {n["kind"] for n in graph["nodes"]}
    assert {"arrival", "inbound", "requisition", "loading", "return", "historical", "migration"} <= kinds
    relations = {e["relation"] for e in graph["edges"]}
    assert "derived" in relations and "archive" in relations

    # 回写库存台账：材料 1 最新批次为 A，余额 60
    material = client.get("/api/material/1").json()
    assert material["批次编号"] == "B-2026-0901-A"
    assert material["批次数量"] == 60.0

    # 回写工程清单与车辆待办
    project = client.get("/api/project/1").json()
    assert "B-2026-0901-A" in project["领用批次"]
    assert project["待装载数量"] == 0
    vehicle = client.get("/api/vehicle/1").json()
    assert vehicle["待装载数量"] == 0  # 装载已确认完成


def test_legacy_list_interface_shape_unchanged(client: TestClient):
    """旧取值接口：字段、分页结构保持不变，新增字段只是叠加。"""
    payload = client.get("/api/material").json()
    assert set(payload) >= {"items", "total", "page", "size"}
    row = next(x for x in payload["items"] if x["id"] == 1)
    for field in ["材料编号", "材料名称", "材料类别", "规格型号", "供应商",
                  "进场日期", "存放地点", "材料状态", "status", "pending"]:
        assert field in row
    # 旧动作接口仍然工作
    r = client.post("/api/material/2/actions", json={"values": {"action": "送检材料"}})
    assert r.json()["ok"] is True
    assert r.json()["entry"]["status"] == "待检测"


# --------------------------------------------------------------- 到场/入库

def test_arrival_then_inbound_creates_batch(client: TestClient):
    r = client.post("/api/lineage/events/arrival", json={
        "event_id": "evt-arrival-x",
        "values": {"material_no": "MATE-0001", "material_name": "沥青",
                   "supplier": "甲供", "batch_no": "B-NEW-1", "quantity": 50},
    })
    assert r.json()["ok"] is True
    # 到场尚未入库，台账无此批次
    assert all(b["batch_no"] != "B-NEW-1" for b in client.get("/api/lineage/ledger").json()["items"])

    r = client.post("/api/lineage/events/inbound", json={
        "event_id": "evt-inbound-x",
        "values": {"batch_no": "B-NEW-1", "location": "3号库", "quantity": 50},
    })
    assert r.status_code == 200
    batch = next(b for b in client.get("/api/lineage/ledger").json()["items"]
                 if b["batch_no"] == "B-NEW-1")
    assert batch["balance"] == 50 and batch["version"] == 1

    # 重复入库被拦
    r = client.post("/api/lineage/events/inbound", json={
        "event_id": "evt-inbound-x2",
        "values": {"batch_no": "B-NEW-1", "location": "3号库"},
    })
    assert r.status_code == 400


def test_arrival_can_register_new_material(client: TestClient):
    r = client.post("/api/lineage/events/arrival", json={
        "event_id": "evt-new-mat",
        "values": {"material_name": "冷补料", "supplier": "乙供",
                   "category": "路面材料", "batch_no": "B-NEW-2", "quantity": 20},
    })
    mid = r.json()["material_id"]
    assert client.get(f"/api/material/{mid}").json()["材料名称"] == "冷补料"


# ----------------------------------------------------------- 版本门闩/并发

def test_requisition_version_latch(client: TestClient):
    client.post("/api/lineage/events/arrival", json={
        "event_id": "v-arrival",
        "values": {"material_id": 1, "material_name": "x", "supplier": "s",
                   "batch_no": "B-V", "quantity": 100}})
    client.post("/api/lineage/events/inbound", json={
        "event_id": "v-inbound",
        "values": {"batch_no": "B-V", "location": "库", "quantity": 100}})

    # 版本 1 领用成功，版本升到 2
    r = client.post("/api/lineage/events/requisition", json={
        "event_id": "v-req-1",
        "values": {"batch_no": "B-V", "project_id": 1, "quantity": 30,
                   "expected_version": 1}})
    assert r.status_code == 200 and r.json()["version"] == 2

    # 拿着旧版本 1 并发领用 -> 409，且余额未被扣减
    r = client.post("/api/lineage/events/requisition", json={
        "event_id": "v-req-stale",
        "values": {"batch_no": "B-V", "project_id": 2, "quantity": 30,
                   "expected_version": 1}})
    assert r.status_code == 409 and r.json()["code"] == "version_conflict"
    batch = next(b for b in client.get("/api/lineage/ledger").json()["items"]
                 if b["batch_no"] == "B-V")
    assert batch["balance"] == 70 and batch["version"] == 2

    # 新版本 2 领用成功
    r = client.post("/api/lineage/events/requisition", json={
        "event_id": "v-req-2",
        "values": {"batch_no": "B-V", "project_id": 2, "quantity": 20,
                   "expected_version": 2}})
    assert r.status_code == 200


def test_requisition_insufficient_stock(client: TestClient):
    r = client.post("/api/lineage/events/requisition", json={
        "event_id": "short",
        "values": {"batch_no": "B-2026-0902-B", "project_id": 1, "quantity": 9999}})
    assert r.status_code == 409 and r.json()["code"] == "insufficient_stock"


# --------------------------------------------------------------- 装载/待办

def test_loading_creates_vehicle_todo_and_project_outstanding(client: TestClient):
    client.post("/api/lineage/events/arrival", json={
        "event_id": "l-arr",
        "values": {"material_id": 1, "material_name": "x", "supplier": "s",
                   "batch_no": "B-L", "quantity": 50}})
    client.post("/api/lineage/events/inbound", json={
        "event_id": "l-in", "values": {"batch_no": "B-L", "location": "库"}})
    # 用工程 3（引导数据中它只有历史存档，无当期待装载），便于核对新增口径
    client.post("/api/lineage/events/requisition", json={
        "event_id": "l-req",
        "values": {"batch_no": "B-L", "project_id": 3, "quantity": 50}})
    r = client.post("/api/lineage/events/loading", json={
        "event_id": "l-load",
        "values": {"batch_no": "B-L", "project_id": 3,
                   "vehicle_id": 2, "quantity": 20}})
    assert r.status_code == 200
    todo_id = r.json()["todo"]["id"]

    todos = client.get("/api/lineage/vehicle-todos?status=待装载").json()["items"]
    assert any(t["id"] == todo_id for t in todos)
    project = client.get("/api/project/3").json()
    assert project["待装载数量"] == 30

    # 不能超量装载
    r = client.post("/api/lineage/events/loading", json={
        "event_id": "l-load-over",
        "values": {"batch_no": "B-L", "project_id": 3,
                   "vehicle_id": 2, "quantity": 99}})
    assert r.status_code == 409

    client.post("/api/lineage/events/complete-todo", json={
        "event_id": "l-done", "values": {"todo_id": todo_id}})
    assert client.get("/api/vehicle/2").json()["待装载数量"] == 0


# ------------------------------------------------------------------- 退料

def test_return_replenishes_balance(client: TestClient):
    before = next(b for b in client.get("/api/lineage/ledger").json()["items"]
                  if b["batch_no"] == "B-2026-0902-B")["balance"]
    r = client.post("/api/lineage/events/return", json={
        "event_id": "ret-1",
        "values": {"batch_no": "B-2026-0902-B", "project_id": 2, "quantity": 5}})
    assert r.status_code == 200 and r.json()["balance"] == before + 5
    # 退料后出现新的 RETURN 节点且可继续领用
    graph = client.get("/api/lineage/graph", params={"batch_no": "B-2026-0902-B"}).json()
    assert graph["nodes"][-1]["kind"] == "return"


# ------------------------------------------------------------------- 换货

def test_exchange_links_new_batch_and_replaces_old(client: TestClient):
    r = client.post("/api/lineage/events/exchange", json={
        "event_id": "ex-1",
        "values": {"old_batch_no": "B-2026-0902-B", "new_batch_no": "B-EX-1",
                   "project_id": 2, "quantity": 20}})
    assert r.status_code == 200
    graph = client.get("/api/lineage/graph", params={"batch_no": "B-EX-1"}).json()
    kinds = [n["kind"] for n in graph["nodes"]]
    assert "exchange_out" in kinds and kinds[-1] == "exchange_in"
    replace_edge = next(e for e in graph["edges"] if e["relation"] == "replace")
    assert replace_edge["from_batch"] == "B-2026-0902-B"
    assert replace_edge["to_batch"] == "B-EX-1"
    # 新批次在工程清单里
    items = client.get("/api/lineage/project-materials", params={"project_id": 2}).json()["items"]
    assert any(m["batch_no"] == "B-EX-1" for m in items)


# --------------------------------------------------------------- 跨工程调拨

def test_transfer_adds_two_nodes_without_losing_chain(client: TestClient):
    r = client.post("/api/lineage/events/transfer", json={
        "event_id": "tr-1",
        "values": {"batch_no": "B-2026-0902-B",
                   "from_project_id": 2, "to_project_id": 1, "quantity": 5}})
    assert r.status_code == 200
    graph = client.get("/api/lineage/graph", params={"batch_no": "B-2026-0902-B"}).json()
    last = [n["kind"] for n in graph["nodes"]][-2:]
    assert last == ["transfer_out", "transfer_in"]
    edge = next(e for e in graph["edges"] if e["relation"] == "transfer")
    assert edge is not None
    target = client.get("/api/lineage/project-materials", params={"project_id": 1}).json()
    assert any(m["batch_no"] == "B-2026-0902-B" for m in target["items"])


# --------------------------------------------------------------- 历史存档

def test_historical_archive_does_not_touch_stock(client: TestClient):
    before = client.get("/api/lineage/ledger").json()
    r = client.post("/api/lineage/events/historical-archive", json={
        "event_id": "h-1",
        "values": {"batch_no": "HIST-OLD", "project_id": 1,
                   "material_id": 1, "quantity": 7}})
    assert r.status_code == 200
    after = client.get("/api/lineage/ledger").json()
    assert before["total"] == after["total"]
    graph = client.get("/api/lineage/graph", params={"batch_no": "HIST-OLD"}).json()
    assert any(n["kind"] == "historical" for n in graph["nodes"])


# ----------------------------------------------------------- 幂等与消息重放

def test_event_id_is_idempotent(client: TestClient):
    payload = {"event_id": "idem-1",
               "values": {"batch_no": "B-2026-0902-B", "project_id": 2, "quantity": 3}}
    r1 = client.post("/api/lineage/events/return", json=payload).json()
    r2 = client.post("/api/lineage/events/return", json=payload).json()
    assert r2["idempotent"] is True
    assert r1["balance"] == r2["balance"]
    nodes = client.get("/api/lineage/graph", params={"batch_no": "B-2026-0902-B"}).json()
    assert sum(1 for n in nodes["nodes"] if n["kind"] == "return"
               and n["event_id"] == "idem-1") == 1


def test_same_event_id_different_type_rejected(client: TestClient):
    body = {"event_id": "dup-type", "values": {"batch_no": "B-2026-0902-B",
                                               "project_id": 2, "quantity": 1}}
    assert client.post("/api/lineage/events/return", json=body).status_code == 200
    assert client.post("/api/lineage/events/requisition", json=body).status_code == 400


def test_message_replay_is_idempotent(client: TestClient):
    ledger_before = client.get("/api/lineage/ledger").json()
    todos_before = client.get("/api/lineage/vehicle-todos").json()["total"]
    r = client.post("/api/lineage/events/replay")
    assert r.status_code == 200
    assert all(d["idempotent"] for d in r.json()["details"])
    assert client.get("/api/lineage/ledger").json() == ledger_before
    assert client.get("/api/lineage/vehicle-todos").json()["total"] == todos_before
    assert client.get("/api/lineage/consistency").json()["ok"] is True


# ----------------------------------------------------------- 迁移与整批回退

def test_unbatched_list_and_migration(client: TestClient):
    pending = client.get("/api/lineage/migration/unbatched").json()
    # 引导只迁移了材料 3；材料 1/2 已在批次中
    assert {x["id"] for x in pending["items"]} == set()  # 材料 2 在 B-2026-0902-B 已归位

    # 材料 3 已迁移为 MIG-0003
    batch = next(b for b in client.get("/api/lineage/ledger").json()["items"]
                 if b["batch_no"] == "MIG-0003")
    assert batch["migrated"] is True and batch["balance"] == 0
    graph = client.get("/api/lineage/graph", params={"batch_no": "MIG-0003"}).json()
    assert graph["nodes"][0]["kind"] == "migration"


def test_migration_rolls_back_whole_batch_on_failure(client: TestClient):
    # 先造两份无批次材料：登记到场但不入库
    for i, bid in enumerate(("B-MIG-X1", "B-MIG-X2"), start=1):
        client.post("/api/lineage/events/arrival", json={
            "event_id": f"arr-mig-{i}",
            "values": {"material_name": f"待迁移{i}", "supplier": "s",
                       "batch_no": bid, "quantity": 1}})
    pending = {x["id"] for x in client.get("/api/lineage/migration/unbatched").json()["items"]}
    assert len(pending) == 2

    # 目标里混入一个不存在的 id：整批必须回退
    bad_targets = sorted(pending) + [999999]
    r = client.post("/api/lineage/migration/run", json={
        "event_id": "mig-fail", "values": {"material_ids": bad_targets}})
    assert r.status_code == 400
    # 没有任何迁移批次产生
    ledger = client.get("/api/lineage/ledger").json()["items"]
    assert not any(b["batch_no"].startswith("MIG-") and b["material_id"] in pending
                   for b in ledger)
    # 失败事件不落地，原材料仍待迁移
    still = {x["id"] for x in client.get("/api/lineage/migration/unbatched").json()["items"]}
    assert pending <= still

    # 修正后整批成功
    r = client.post("/api/lineage/migration/run", json={
        "event_id": "mig-ok", "values": {"material_ids": sorted(pending)}})
    assert r.status_code == 200 and r.json()["count"] == 2


def test_migration_idempotent(client: TestClient):
    # 再次迁移已无目标，返回 400 且不产生节点
    r = client.post("/api/lineage/migration/run", json={
        "event_id": "mig-empty", "values": {}})
    assert r.status_code == 400


# --------------------------------------------------------------- 事务原子性

def test_exchange_rolls_back_when_new_batch_exists(client: TestClient):
    # B-2026-0901-A 已存在，作为新批次换货应整体失败，旧批次不动
    old = next(b for b in client.get("/api/lineage/ledger").json()["items"]
               if b["batch_no"] == "B-2026-0902-B")
    r = client.post("/api/lineage/events/exchange", json={
        "event_id": "ex-fail",
        "values": {"old_batch_no": "B-2026-0902-B",
                   "new_batch_no": "B-2026-0901-A", "project_id": 2, "quantity": 1}})
    assert r.status_code == 400
    again = next(b for b in client.get("/api/lineage/ledger").json()["items"]
                 if b["batch_no"] == "B-2026-0902-B")
    assert again["version"] == old["version"]
    assert not any(n["kind"] == "exchange_out"
                   for n in client.get("/api/lineage/graph").json()["nodes"])


# --------------------------------------------------------------- 最终一致性

def test_consistency_check_passes_after_all_demo_flows(client: TestClient):
    result = client.get("/api/lineage/consistency").json()
    assert result["ok"] is True, result["problems"]
