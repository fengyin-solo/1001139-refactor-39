"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

谱系域新增的内部表（批次、节点、边、事件日志等）统一放在 ``lineage_`` 命名空间下，
不进入 :meth:`Store.module_names`，因此运营概览与旧模块列表的口径保持不变。
所有写操作经 :meth:`Store.transaction` 暂存，业务校验失败时整批回滚。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 谱系域内部表前缀：下划线开头的表不进运营概览
INTERNAL_PREFIX = "_lineage_"

# 全局写锁：库存事件串行提交，配合行级 version 构成并发领用的版本门闩
_WRITE_LOCK = threading.RLock()


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if not name.startswith(INTERNAL_PREFIX))

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """事务上下文：进入时对受影响表打快照，异常时整批回滚。

        内存仓库没有真正的 WAL，这里用「提交前快照 + 异常还原」实现迁移、换货等
        多节点写入的原子性；全局 RLock 保证并发领用不会同时读到同一余额。
        """
        with _WRITE_LOCK:
            snapshot = copy.deepcopy(self._tables)
            try:
                yield
            except Exception:
                self._tables = snapshot
                raise


store = Store()
