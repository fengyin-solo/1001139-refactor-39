"""批次谱系测试：全链路回写、重放幂等、版本门闩、并发领用、迁移回退与旧接口兼容。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.batch_genealogy import BatchGenealogyService
from app.store import Store


def make_service() -> tuple[BatchGenealogyService, Store]:
    fresh = Store()
    return BatchGenealogyService(fresh), fresh


def seed_event(svc: BatchGenealogyService, **overrides):
    values = {
        "event_id": "EVT-TEST-1",
        "event_type": "供应商到场",
        "material_id": 3,
        "batch_no": "MATE-0003-B01",
        "quantity": 50,
    }
    values.update(overrides)
    return svc.append_event(values)


def test_full_chain_writes_back_to_ledger_project_vehicle():
    svc, fresh = make_service()
    node, msg = seed_event(svc)
    assert node is not None, msg
    node, msg = seed_event(svc, event_id="EVT-TEST-2", event_type="仓管入库")
    assert node is not None, msg
    node, msg = seed_event(
        svc, event_id="EVT-TEST-3", event_type="工程领用", quantity=20, project_id=2, expected_version=1
    )
    assert node is not None, msg
    node, msg = seed_event(svc, event_id="EVT-TEST-4", event_type="车辆装载", quantity=20, vehicle_id=2)
    assert node is not None, msg

    material = fresh.find("material", 3)
    assert material["当前批次"] == "MATE-0003-B01"
    assert material["库存数量"] == 30
    assert material["批次版本"] == 2
    assert "已领用 20 至 PROJ-0002" in material["批次结论"]

    project = fresh.find("project", 2)
    assert project["材料清单"][-1]["批次号"] == "MATE-0003-B01"
    assert project["材料清单"][-1]["动作"] == "工程领用"

    vehicle = fresh.find("vehicle", 2)
    assert vehicle["装载待办"][-1]["批次号"] == "MATE-0003-B01"
    assert vehicle["装载待办"][-1]["状态"] == "待装载"

    tree = svc.batch_tree("MATE-0003-B01")
    assert tree is not None
    assert [n["event_type"] for n in tree["nodes"]] == ["供应商到场", "仓管入库", "工程领用", "车辆装载"]
    assert len(tree["edges"]) == 3


def test_replay_same_event_id_is_idempotent():
    svc, fresh = make_service()
    seed_event(svc)
    seed_event(svc, event_id="EVT-TEST-2", event_type="仓管入库")
    first, _ = seed_event(
        svc, event_id="EVT-REPLAY", event_type="工程领用", quantity=10, project_id=1, expected_version=1
    )
    assert first is not None
    stock_after_first = fresh.find("material", 3)["库存数量"]
    nodes_after_first = len(fresh.rows("_batch_node"))

    replayed, message = seed_event(
        svc, event_id="EVT-REPLAY", event_type="工程领用", quantity=10, project_id=1, expected_version=1
    )
    assert replayed is not None
    assert replayed["node_id"] == first["node_id"]
    assert "重放" in message
    assert fresh.find("material", 3)["库存数量"] == stock_after_first
    assert len(fresh.rows("_batch_node")) == nodes_after_first


def test_issue_requires_version_latch():
    svc, fresh = make_service()
    seed_event(svc)
    seed_event(svc, event_id="EVT-TEST-2", event_type="仓管入库")
    node, message = seed_event(
        svc, event_id="EVT-STALE", event_type="工程领用", quantity=10, project_id=1, expected_version=99
    )
    assert node is None
    assert "版本门闩冲突" in message
    assert fresh.find("material", 3)["库存数量"] == 50
    assert not any(n["event_id"] == "EVT-STALE" for n in fresh.rows("_batch_node"))


def test_concurrent_issue_serialized_by_version_latch():
    svc, fresh = make_service()
    # 材料 2 种子数据：批次 MATE-0002-B01，库存 80，版本 1
    results: list[tuple[dict | None, str]] = []
    barrier = threading.Barrier(2)

    def issue(event_id: str) -> None:
        barrier.wait()
        results.append(
            svc.append_event({
                "event_id": event_id,
                "event_type": "工程领用",
                "material_id": 2,
                "batch_no": "MATE-0002-B01",
                "quantity": 50,
                "project_id": 1,
                "expected_version": 1,
            })
        )

    threads = [threading.Thread(target=issue, args=(f"EVT-RACE-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    succeeded = [node for node, _ in results if node is not None]
    assert len(succeeded) == 1  # 一把版本门闩只放过去一个，另一个必须刷新版本重来
    assert fresh.find("material", 2)["库存数量"] == 30
    assert fresh.find("material", 2)["批次版本"] == 2


def test_issue_beyond_stock_rejected_without_side_effects():
    svc, fresh = make_service()
    seed_event(svc)
    seed_event(svc, event_id="EVT-TEST-2", event_type="仓管入库")
    node, message = seed_event(
        svc, event_id="EVT-OVER", event_type="工程领用", quantity=999, project_id=1, expected_version=1
    )
    assert node is None
    assert "不足" in message
    material = fresh.find("material", 3)
    assert material["库存数量"] == 50
    assert material["批次版本"] == 1
    assert len(fresh.rows("_batch_node")) == 8  # 6 个种子节点 + 到场 + 入库，失败事件不留痕


def test_missing_parent_event_rejected():
    svc, fresh = make_service()
    node, message = seed_event(svc, event_type="仓管入库")  # 没有先到场就入库
    assert node is None
    assert "缺少前置环节" in message
    assert len(fresh.rows("_batch_node")) == 6


def test_return_exchange_transfer_add_nodes_and_keep_archive():
    svc, fresh = make_service()
    # 材料 2：领用 30 到工程 1
    node, msg = svc.append_event({
        "event_id": "EVT-ISSUE", "event_type": "工程领用", "material_id": 2,
        "batch_no": "MATE-0002-B01", "quantity": 30, "project_id": 1, "expected_version": 1,
    })
    assert node is not None, msg
    issue_node_id = node["node_id"]

    node, msg = svc.append_event({
        "event_id": "EVT-RETURN", "event_type": "退料", "material_id": 2,
        "batch_no": "MATE-0002-B01", "quantity": 10,
    })
    assert node is not None, msg
    assert node["parent_ids"] == [issue_node_id]
    assert fresh.find("material", 2)["库存数量"] == 60
    assert fresh.find("project", 1)["材料清单"][-1]["动作"] == "退料"

    node, msg = svc.append_event({
        "event_id": "EVT-TRANSFER", "event_type": "跨工程调拨", "material_id": 2,
        "batch_no": "MATE-0002-B01", "quantity": 5, "target_project_id": 3,
    })
    assert node is not None, msg
    assert fresh.find("project", 1)["材料清单"][-1]["动作"] == "调拨出"
    assert fresh.find("project", 3)["材料清单"][-1]["动作"] == "调拨入"
    assert fresh.find("material", 2)["库存数量"] == 60  # 调拨不动库存

    node, msg = svc.append_event({
        "event_id": "EVT-EXCHANGE", "event_type": "换货", "material_id": 2,
        "batch_no": "MATE-0002-B01", "quantity": 60, "new_batch_no": "MATE-0002-B02",
    })
    assert node is not None, msg
    assert node["source_batch_no"] == "MATE-0002-B01"
    assert fresh.find("material", 2)["当前批次"] == "MATE-0002-B02"
    # 换货后的新批次谱系图能跨链回看到旧批次节点，不断链
    tree = svc.batch_tree("MATE-0002-B02")
    assert tree is not None and any(n["batch_no"] == "MATE-0002-B01" for n in tree["nodes"])

    # 历史领用仍按原批次存档，退料换货调拨都不改写
    archive = [row for row in fresh.rows("_issue_archive") if row["node_id"] == issue_node_id]
    assert len(archive) == 1
    assert archive[0]["batch_no"] == "MATE-0002-B01"
    assert archive[0]["quantity"] == 30


def test_migration_assigns_batches_and_is_idempotent():
    svc, fresh = make_service()
    assert fresh.find("material", 3).get("当前批次") is None
    summary, msg = svc.migrate_legacy({"migration_id": "MIG-TEST", "default_quantity": 100})
    assert summary is not None, msg
    assert summary["migrated"] == 1
    material = fresh.find("material", 3)
    assert material["当前批次"] == "MATE-0003-LEGACY"
    assert material["库存数量"] == 100
    assert material["批次版本"] == 1
    migration_nodes = [n for n in fresh.rows("_batch_node") if n["event_type"] == "迁移归位"]
    assert len(migration_nodes) == 1

    replayed, msg = svc.migrate_legacy({"migration_id": "MIG-TEST", "default_quantity": 100})
    assert replayed == summary
    assert "重放" in msg
    assert len([n for n in fresh.rows("_batch_node") if n["event_type"] == "迁移归位"]) == 1


def test_migration_rolls_back_whole_batch_on_failure(monkeypatch):
    svc, fresh = make_service()
    # 制造三条存量无批次材料，第二个节点构造时注入失败
    for material_id in (1, 2):
        row = fresh.find("material", material_id)
        for field in ("当前批次", "库存数量", "批次版本", "批次结论"):
            row.pop(field, None)
    before_nodes = len(fresh.rows("_batch_node"))
    before_row1 = dict(fresh.find("material", 1))

    original = BatchGenealogyService._build_migration
    calls = {"count": 0}

    def flaky(self, row, migration_id, default_quantity):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("模拟迁移中途失败")
        return original(self, row, migration_id, default_quantity)

    monkeypatch.setattr(BatchGenealogyService, "_build_migration", flaky)
    summary, msg = svc.migrate_legacy({"migration_id": "MIG-FAIL"})
    assert summary is None
    assert "整批回退" in msg
    assert len(fresh.rows("_batch_node")) == before_nodes  # 整批节点已回退
    assert fresh.find("material", 1) == before_row1  # 台账一并回滚
    assert fresh.find("material", 3).get("当前批次") is None


def test_old_material_interfaces_stay_compatible():
    client = TestClient(app)
    page = client.get("/api/material").json()
    assert page["total"] >= 3
    first = page["items"][0]
    for field in ("材料编号", "材料名称", "材料类别", "status"):
        assert field in first

    detail = client.get("/api/material/1").json()
    assert detail["材料编号"] == "MATE-0001"

    action = client.post("/api/material/1/actions", json={"values": {"action": "送检材料"}}).json()
    assert action["ok"] is True
    assert action["entry"]["status"] == "待检测"

    overview = client.get("/api/overview").json()
    assert len(overview["modules"]) == 18  # 内部谱系表不进业务模块清单
    assert client.get("/api/health").json()["modules"] == 18


def test_genealogy_api_browse_and_writeback():
    client = TestClient(app)
    nodes = client.get("/api/batch-genealogy/nodes", params={"keyword": "MATE-0001"}).json()
    assert nodes["total"] == 4

    tree = client.get("/api/batch-genealogy/batches/MATE-0001-B01").json()
    assert len(tree["nodes"]) == 4
    assert client.get("/api/batch-genealogy/batches/NO-SUCH-BATCH").status_code == 404

    archive = client.get("/api/batch-genealogy/archive").json()
    assert archive["total"] >= 1
    assert archive["items"][0]["batch_no"] == "MATE-0001-B01"

    writeback = client.get("/api/batch-genealogy/writeback").json()
    assert writeback["ledger"][0]["当前批次"] == "MATE-0001-B01"
    assert writeback["projects"][0]["材料清单"]
    assert writeback["vehicles"][0]["装载待办"]

    missing_id = client.post("/api/batch-genealogy/events", json={"values": {"event_type": "供应商到场"}}).json()
    assert missing_id["ok"] is False
    assert "event_id" in missing_id["message"]
