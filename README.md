# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   ├── app/lineage/          批次谱系域（节点/边/事件/台账）
│   ├── app/store.py          内存数据仓库、事务快照与示例数据
│   └── tests/                谱系域 pytest 用例
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 批次谱系图（`/lineage`）

养护材料按「供应商到场 → 仓管入库 → 工程领用 → 车辆装载」串成可浏览的批次谱系，
退料、换货、跨工程调拨以新节点续链（边关系分别为 `derived` / `replace` /
`transfer`），历史领用按原批次以 `historical` 节点存档。实现集中在
`backend/app/lineage/`：

- **谱系存储**：`_lineage_nodes`（节点）+ `_lineage_edges`（父子边）。
- **结论回写三处**：批次台账 `_lineage_batches`（同步在 `material` 行叠加
  `批次编号/批次数量/材料状态`）、工程清单 `_lineage_project_materials`
  （同步在 `project` 行叠加 `领用批次/待装载数量`）、车辆待办
  `_lineage_vehicle_todos`（同步在 `vehicle` 行叠加 `待装载批次/待装载数量`）。
  旧取值接口字段与分页结构不变，新字段只做叠加。
- **谱系写入与库存事件同事务**：`Store.transaction()` 提交前快照，任何一步抛错
  整批回滚；换货/迁移不会留下半个批次。
- **消息重放幂等**：写接口都接受 `event_id` 幂等键（`POST /api/lineage/events/*`），
  事件落 `_lineage_events` 日志；`POST /api/lineage/events/replay` 按序重放，
  重复键直接返回首次结果，不重复扣减。
- **并发领用版本门闩**：批次行带 `version`，领用携带 `expected_version`，过期返回
  `409 version_conflict`；全局写锁保证内存仓库下的串行提交与不超卖。
- **存量无批次迁移**：`GET /api/lineage/migration/unbatched` 查看待迁移材料，
  `POST /api/lineage/migration/run` 整批归位为 `MIG-xxxx` 批次（`migration` 节点），
  任一目标失败整批回退。
- **自检**：`GET /api/lineage/consistency` 校验台账余额与事件汇总、边完整性、
  工程清单汇总、回写字段及事件幂等键。

服务启动时会幂等引导一条示例谱系（`bootstrap_demo()`），可用
`POST /api/lineage/bootstrap` 重复触发。测试：`cd backend && python3 -m pytest`。
