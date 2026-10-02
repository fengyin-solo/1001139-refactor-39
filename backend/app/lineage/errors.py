"""批次谱系域：错误类型与节点/动作常量。"""
from __future__ import annotations


class LineageError(Exception):
    """谱系业务错误基类；message 会原样回给调用方。"""

    code = "lineage_error"


class NotFoundError(LineageError):
    code = "not_found"


class ValidationError(LineageError):
    code = "validation_error"


class VersionConflictError(LineageError):
    """并发领用：调用方携带的 expected_version 已过期。"""

    code = "version_conflict"


class InsufficientStockError(LineageError):
    code = "insufficient_stock"


# 谱系节点类型（kind）
ARRIVAL = "arrival"        # 供应商到场
INBOUND = "inbound"        # 仓管入库
REQUISITION = "requisition"          # 工程领用
LOADING = "loading"                  # 车辆装载
RETURN = "return"                    # 退料
EXCHANGE_OUT = "exchange_out"        # 换货：旧批次退出
EXCHANGE_IN = "exchange_in"          # 换货：新批次补入
TRANSFER_OUT = "transfer_out"        # 跨工程调拨出
TRANSFER_IN = "transfer_in"          # 跨工程调拨入
HISTORICAL = "historical"            # 历史领用按原批次存档
MIGRATION = "migration"              # 存量无批次材料迁移归位

NODE_KINDS = {
    ARRIVAL, INBOUND, REQUISITION, LOADING, RETURN,
    EXCHANGE_OUT, EXCHANGE_IN, TRANSFER_OUT, TRANSFER_IN,
    HISTORICAL, MIGRATION,
}

NODE_LABELS = {
    ARRIVAL: "到场",
    INBOUND: "入库",
    REQUISITION: "领用",
    LOADING: "装载",
    RETURN: "退料",
    EXCHANGE_OUT: "换货出",
    EXCHANGE_IN: "换货入",
    TRANSFER_OUT: "调拨出",
    TRANSFER_IN: "调拨入",
    HISTORICAL: "历史领用",
    MIGRATION: "迁移归位",
}

# 边（父子关系）类型
EDGE_DERIVED = "derived"      # 普通流转衍生（到场->入库->领用...）
EDGE_REPLACE = "replace"      # 换货：新批次替换旧批次
EDGE_TRANSFER = "transfer"    # 跨工程调拨
EDGE_ARCHIVE = "archive"      # 历史存档 / 迁移归位

# 事件类型 -> 中文动作
EVENT_TYPES = {
    "arrival": "供应商到场",
    "inbound": "仓管入库",
    "requisition": "工程领用",
    "loading": "车辆装载",
    "return_material": "退料",
    "exchange": "换货",
    "transfer": "跨工程调拨",
    "historical_archive": "历史领用存档",
    "migration": "无批次迁移",
    "complete_todo": "完成车辆待办",
}

# 台账批次状态
BATCH_IN_STOCK = "在库"
BATCH_ISSUED = "已领用"
BATCH_LOADED = "已装载"
BATCH_CLOSED = "已结清"
