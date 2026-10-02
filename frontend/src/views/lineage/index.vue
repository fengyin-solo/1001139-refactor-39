<template>
  <section class="page lineage-page">
    <header class="page-head">
      <div>
        <h2>批次谱系图</h2>
        <p class="page-desc">
          供应商到场 → 仓管入库 → 工程领用 → 车辆装载，退料 / 换货 / 跨工程调拨新增谱系节点；
          批次结论实时回写库存台账、工程清单与车辆待办。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="replayEvents">重放消息</button>
        <button class="btn" type="button" @click="checkConsistency">一致性自检</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p v-if="message" :class="['lineage-msg', messageOk ? 'ok' : 'error-text']">{{ message }}</p>

    <!-- 筛选条 -->
    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>批次号</span>
        <input v-model="filters.batch_no" placeholder="按批次号查看链路" />
      </label>
      <label class="filter-item">
        <span>材料ID</span>
        <input v-model="filters.material_id" placeholder="材料 id" />
      </label>
      <label class="filter-item">
        <span>工程ID</span>
        <input v-model="filters.project_id" placeholder="工程 id" />
      </label>
      <label class="filter-item">
        <span>车辆ID</span>
        <input v-model="filters.vehicle_id" placeholder="车辆 id" />
      </label>
      <button class="btn" type="submit">查询谱系</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置</button>
    </form>

    <!-- 谱系图 -->
    <div class="graph-card">
      <div class="graph-head">
        <h3>谱系链路（{{ graph.nodes.length }} 节点 / {{ graph.edges.length }} 关系）</h3>
        <div class="legend">
          <span v-for="(label, key) in kindLabels" :key="key" :class="['legend-item', `kind-${key}`]">
            {{ label }}
          </span>
        </div>
      </div>
      <div v-if="layout.length" class="graph-scroll">
        <svg :width="svgWidth" :height="svgHeight" class="lineage-svg">
          <defs>
            <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
              <path d="M0,0 L7,3 L0,6 Z" fill="#94a3b8" />
            </marker>
            <marker id="arrow-replace" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
              <path d="M0,0 L7,3 L0,6 Z" fill="#f97316" />
            </marker>
            <marker id="arrow-archive" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
              <path d="M0,0 L7,3 L0,6 Z" fill="#8b5cf6" />
            </marker>
          </defs>
          <path
            v-for="edge in edgeGeoms"
            :key="edge.id"
            :d="edge.d"
            fill="none"
            :stroke="edgeColor(edge.relation)"
            :stroke-width="edge.relation === 'derived' ? 1.5 : 2.2"
            :stroke-dasharray="edge.relation === 'derived' ? '' : '5 3'"
            :marker-end="`url(#${markerFor(edge.relation)})`"
          />
          <g
            v-for="cell in layout"
            :key="cell.node.id"
            :transform="`translate(${cell.x},${cell.y})`"
            class="node-group"
            @click="selectNode(cell.node)"
          >
            <rect
              width="172" height="76" rx="8"
              :class="['node-rect', `kind-${cell.node.kind}`,
                       { anchor: graph.anchor_node_ids.includes(cell.node.id), active: selectedId === cell.node.id }]"
            />
            <text x="10" y="20" class="node-kind">{{ cell.node.kind_label }}</text>
            <text x="10" y="40" class="node-batch">{{ cell.node.batch_no || '—' }}</text>
            <text x="10" y="58" class="node-summary" :title="cell.node.summary">{{ clip(cell.node.summary) }}</text>
            <text x="162" y="20" class="node-qty">×{{ cell.node.quantity }}</text>
          </g>
        </svg>
      </div>
      <div v-else class="empty-state">当前筛选条件下没有谱系节点</div>
    </div>

    <div v-if="selected" class="detail-card">
      <h3>节点详情</h3>
      <dl class="detail-grid">
        <template v-for="[k, v] in detailRows" :key="k">
          <dt>{{ k }}</dt><dd>{{ v ?? '—' }}</dd>
        </template>
      </dl>
    </div>

    <!-- 三栏：台账 / 工程清单 / 车辆待办 -->
    <div class="writeback-grid">
      <article class="wb-card">
        <h3>库存台账（批次）</h3>
        <table class="mini-table">
          <thead><tr><th>批次</th><th>材料</th><th>余额</th><th>版本</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="b in ledger" :key="b.batch_no">
              <td><a href="#" @click.prevent="focusBatch(b.batch_no)">{{ b.batch_no }}</a></td>
              <td>{{ b.material_name }}</td>
              <td>{{ b.balance }}</td>
              <td>v{{ b.version }}</td>
              <td>{{ b.status }}</td>
            </tr>
          </tbody>
        </table>
      </article>

      <article class="wb-card">
        <h3>工程清单</h3>
        <table class="mini-table">
          <thead><tr><th>工程</th><th>批次</th><th>领用</th><th>已装</th><th>待装</th></tr></thead>
          <tbody>
            <tr v-for="m in projectMaterials" :key="m.id">
              <td>#{{ m.project_id }} {{ m.project_name }}</td>
              <td>{{ m.batch_no }}</td>
              <td>{{ m.received_qty }}</td>
              <td>{{ m.loaded_qty }}</td>
              <td :class="{ warn: m.outstanding_qty > 0 }">{{ m.outstanding_qty }}</td>
            </tr>
          </tbody>
        </table>
      </article>

      <article class="wb-card">
        <h3>车辆待办</h3>
        <table class="mini-table">
          <thead><tr><th>车辆</th><th>批次</th><th>数量</th><th>状态</th><th></th></tr></thead>
          <tbody>
            <tr v-for="t in todos" :key="t.id">
              <td>#{{ t.vehicle_id }} {{ t.vehicle_no }}</td>
              <td>{{ t.batch_no }}</td>
              <td>{{ t.quantity }}</td>
              <td :class="{ warn: t.status === '待装载' }">{{ t.status }}</td>
              <td>
                <button v-if="t.status === '待装载'" class="link" type="button" @click="completeTodo(t.id)">
                  确认装载
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </article>
    </div>

    <!-- 事件操作台 -->
    <div class="action-card">
      <h3>库存事件登记（谱系写入与库存事件同事务提交）</h3>
      <div class="action-tabs">
        <button
          v-for="opt in actionDefs"
          :key="opt.type"
          type="button"
          :class="['tab', { active: actionType === opt.type }]"
          @click="actionType = opt.type"
        >
          {{ opt.label }}
        </button>
      </div>
      <div class="action-form">
        <label v-for="field in currentFields" :key="field.key" class="filter-item">
          <span>{{ field.label }}</span>
          <input v-model="form[field.key]" :placeholder="field.placeholder ?? ''" />
        </label>
        <label class="filter-item">
          <span>event_id</span>
          <input v-model="form.event_id" placeholder="留空自动生成（消息幂等键）" />
        </label>
        <button class="btn primary" type="button" @click="submitEvent">提交事件</button>
      </div>
    </div>

    <!-- 无批次迁移 -->
    <div class="action-card">
      <h3>存量无批次材料迁移归位（整批成功或整批回退）</h3>
      <p class="page-desc">待迁移：{{ unbatched.length }} 份
        <span v-for="m in unbatched" :key="m.id" class="chip">#{{ m.id }} {{ m['材料名称'] }}</span>
      </p>
      <div class="action-form">
        <label class="filter-item">
          <span>材料ID（逗号分隔，留空迁移全部）</span>
          <input v-model="migrationIds" placeholder="如 4,5" />
        </label>
        <label class="filter-item">
          <span>event_id</span>
          <input v-model="migrationEventId" placeholder="迁移事件幂等键" />
        </label>
        <button class="btn primary" type="button" @click="runMigration">执行整批迁移</button>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type NodeKind =
  | 'arrival' | 'inbound' | 'requisition' | 'loading' | 'return'
  | 'exchange_out' | 'exchange_in' | 'transfer_out' | 'transfer_in'
  | 'historical' | 'migration'

interface GraphNode {
  id: number
  kind: NodeKind
  kind_label: string
  batch_no: string | null
  material_id: number | null
  project_id: number | null
  vehicle_id: number | null
  quantity: number
  summary: string
  event_id: string
  created_at: string
}
interface GraphEdge {
  id: number
  parent_id: number
  child_id: number
  relation: 'derived' | 'replace' | 'transfer' | 'archive'
  from_batch: string | null
  to_batch: string | null
  quantity: number
  project_id: number | null
}

const kindLabels: Record<string, string> = {
  arrival: '到场', inbound: '入库', requisition: '领用', loading: '装载', return: '退料',
  exchange_out: '换货出', exchange_in: '换货入', transfer_out: '调拨出', transfer_in: '调拨入',
  historical: '历史领用', migration: '迁移归位',
}

const ENDPOINT = '/api/lineage'

const graph = ref<{ nodes: GraphNode[]; edges: GraphEdge[]; batches: any[]; anchor_node_ids: number[] }>({
  nodes: [], edges: [], batches: [], anchor_node_ids: [],
})
const ledger = ref<any[]>([])
const projectMaterials = ref<any[]>([])
const todos = ref<any[]>([])
const unbatched = ref<any[]>([])
const message = ref('')
const messageOk = ref(true)
const selectedId = ref<number | null>(null)

const filters = reactive({ batch_no: '', material_id: '', project_id: '', vehicle_id: '' })

interface ActionField { key: string; label: string; placeholder?: string }

const actionDefs: { type: string; label: string; fields: ActionField[] }[] = [
  { type: 'arrival', label: '到场', fields: [
    { key: 'material_id', label: '材料ID（可空，按名称新建）' },
    { key: 'material_name', label: '材料名称' },
    { key: 'supplier', label: '供应商' },
    { key: 'batch_no', label: '批次号' },
    { key: 'quantity', label: '数量' },
  ]},
  { type: 'inbound', label: '入库', fields: [
    { key: 'batch_no', label: '批次号' }, { key: 'location', label: '存放地点' },
    { key: 'quantity', label: '数量（可空=到场量）' },
  ]},
  { type: 'requisition', label: '领用', fields: [
    { key: 'batch_no', label: '批次号' }, { key: 'project_id', label: '工程ID' },
    { key: 'quantity', label: '数量' }, { key: 'expected_version', label: 'expected_version（版本门闩）' },
  ]},
  { type: 'loading', label: '装载', fields: [
    { key: 'batch_no', label: '批次号' }, { key: 'project_id', label: '工程ID' },
    { key: 'vehicle_id', label: '车辆ID' }, { key: 'quantity', label: '数量' },
  ]},
  { type: 'return_material', label: '退料', fields: [
    { key: 'batch_no', label: '批次号' }, { key: 'project_id', label: '工程ID' },
    { key: 'quantity', label: '数量' },
  ]},
  { type: 'exchange', label: '换货', fields: [
    { key: 'old_batch_no', label: '旧批次号' }, { key: 'new_batch_no', label: '新批次号' },
    { key: 'project_id', label: '工程ID' }, { key: 'quantity', label: '数量' },
  ]},
  { type: 'transfer', label: '跨工程调拨', fields: [
    { key: 'batch_no', label: '批次号' }, { key: 'from_project_id', label: '源工程ID' },
    { key: 'to_project_id', label: '目标工程ID' }, { key: 'quantity', label: '数量' },
  ]},
  { type: 'historical_archive', label: '历史存档', fields: [
    { key: 'batch_no', label: '原批次号' }, { key: 'project_id', label: '工程ID' },
    { key: 'material_id', label: '材料ID（可空）' }, { key: 'quantity', label: '历史数量' },
    { key: 'vehicle_id', label: '车辆ID（可空）' },
  ]},
]

const actionType = ref<string>('arrival')
const form = reactive<Record<string, string>>({})
const migrationIds = ref('')
const migrationEventId = ref('')

const currentFields = computed(() => actionDefs.find(a => a.type === actionType.value)?.fields ?? [])

const stats = computed(() => [
  { label: '批次总数', value: ledger.value.length },
  { label: '在库余额合计', value: ledger.value.reduce((s, b) => s + Number(b.balance || 0), 0) },
  { label: '待装载批次', value: todos.value.filter(t => t.status === '待装载').length },
  { label: '无批次待迁移', value: unbatched.value.length },
])

const selected = computed(() => graph.value.nodes.find(n => n.id === selectedId.value) ?? null)
const detailRows = computed(() => {
  const n = selected.value
  if (!n) return []
  return [
    ['类型', n.kind_label], ['批次号', n.batch_no], ['数量', n.quantity],
    ['材料ID', n.material_id], ['工程ID', n.project_id], ['车辆ID', n.vehicle_id],
    ['事件ID', n.event_id], ['时间', n.created_at], ['说明', n.summary],
  ]
})

// ------------------------------------------------ 图谱布局：按批次分列，列内按时间排

const NODE_W = 172
const NODE_H = 76
const COL_GAP = 70
const ROW_GAP = 26

const layout = computed(() => {
  const byBatch = new Map<string, GraphNode[]>()
  for (const n of [...graph.value.nodes].sort((a, b) => a.id - b.id)) {
    const key = n.batch_no || `#${n.id}`
    if (!byBatch.has(key)) byBatch.set(key, [])
    byBatch.get(key)!.push(n)
  }
  // 批次列顺序：取每列最早节点 id
  const cols = [...byBatch.entries()]
    .map(([batch, ns]) => ({ batch, ns, first: Math.min(...ns.map(n => n.id)) }))
    .sort((a, b) => a.first - b.first)
  const cells: { node: GraphNode; x: number; y: number }[] = []
  cols.forEach((col, ci) => {
    col.ns.forEach((node, ri) => {
      cells.push({ node, x: ci * (NODE_W + COL_GAP) + 12, y: ri * (NODE_H + ROW_GAP) + 12 })
    })
  })
  return cells
})

const svgWidth = computed(() => {
  const cols = new Set(layout.value.map(c => c.x)).size
  return Math.max(cols * (NODE_W + COL_GAP) + 24, 300)
})
const maxRow = computed(() => Math.max(0, ...layout.value.map(c => c.y)))
const svgHeight = computed(() => maxRow.value + NODE_H + 24)

const edgeGeoms = computed(() => {
  const pos = new Map(layout.value.map(c => [c.node.id, c]))
  return graph.value.edges.map(e => {
    const p = pos.get(e.parent_id)
    const c = pos.get(e.child_id)
    if (!p || !c) return { id: e.id, relation: e.relation, d: '' }
    const x1 = p.x + NODE_W, y1 = p.y + NODE_H / 2
    const x2 = c.x, y2 = c.y + NODE_H / 2
    const mx = (x1 + x2) / 2
    return { id: e.id, relation: e.relation, d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 4},${y2}` }
  }).filter(x => x.d)
})

function edgeColor(relation: string) {
  return relation === 'replace' ? '#f97316' : relation === 'archive' ? '#8b5cf6'
    : relation === 'transfer' ? '#0ea5e9' : '#94a3b8'
}
function markerFor(relation: string) {
  return relation === 'replace' ? 'arrow-replace' : relation === 'archive' ? 'arrow-archive' : 'arrow'
}
function clip(text: string) {
  return text && text.length > 14 ? `${text.slice(0, 13)}…` : text
}
function selectNode(n: GraphNode) {
  selectedId.value = n.id
}
function focusBatch(batchNo: string) {
  filters.batch_no = batchNo
  void reload()
}

// ------------------------------------------------------------- 数据加载

async function getJson<T>(path: string): Promise<T> {
  const r = await request(path)
  if (!r.ok) throw new Error(`接口返回 ${r.status}`)
  return (await r.json()) as T
}

function setMsg(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

async function reload() {
  const qs = new URLSearchParams()
  Object.entries(filters).forEach(([k, v]) => { if (v.trim()) qs.set(k, v.trim()) })
  try {
    const [g, l, p, t, u] = await Promise.all([
      getJson<any>(`${ENDPOINT}/graph?${qs.toString()}`),
      getJson<any>(`${ENDPOINT}/ledger`),
      getJson<any>(`${ENDPOINT}/project-materials`),
      getJson<any>(`${ENDPOINT}/vehicle-todos`),
      getJson<any>(`${ENDPOINT}/migration/unbatched`),
    ])
    graph.value = g
    ledger.value = l.items
    projectMaterials.value = p.items
    todos.value = t.items
    unbatched.value = u.items
    selectedId.value = null
  } catch (error) {
    setMsg(error instanceof Error ? error.message : '谱系数据读取失败', false)
  }
}

function resetFilters() {
  filters.batch_no = filters.material_id = filters.project_id = filters.vehicle_id = ''
  void reload()
}

// ------------------------------------------------------------- 事件提交

async function submitEvent() {
  const values: Record<string, unknown> = {}
  for (const field of currentFields.value) {
    const raw = form[field.key]
    if (raw === undefined || raw === '') continue
    values[field.key] = ['material_id', 'project_id', 'vehicle_id', 'quantity', 'expected_version']
      .includes(field.key) ? Number(raw) : raw
  }
  const body: Record<string, unknown> = { values }
  if (form.event_id) body.event_id = form.event_id
  try {
    const r = await request(`${ENDPOINT}/events/${actionPath(actionType.value)}`, {
      method: 'POST', body: JSON.stringify(body),
    })
    const data = await r.json()
    if (!r.ok || data.ok === false) {
      setMsg(data.message ? `[${data.code ?? r.status}] ${data.message}` : '事件提交失败', false)
      return
    }
    setMsg(`${data.message ?? '事件已提交'}${data.idempotent ? '（幂等命中，未重复记账）' : ''}`)
    Object.keys(form).forEach(k => delete form[k])
    await reload()
  } catch (error) {
    setMsg(error instanceof Error ? error.message : '事件提交失败', false)
  }
}

function actionPath(type: string) {
  return { return_material: 'return', historical_archive: 'historical-archive' }[type] ?? type
}

async function completeTodo(todoId: number) {
  const r = await request(`${ENDPOINT}/events/complete-todo`, {
    method: 'POST', body: JSON.stringify({ values: { todo_id: todoId } }),
  })
  const data = await r.json()
  setMsg(data.message ?? '待办已完成', r.ok)
  await reload()
}

async function runMigration() {
  const values: Record<string, unknown> = {}
  if (migrationIds.value.trim()) {
    values.material_ids = migrationIds.value.split(',').map(s => Number(s.trim())).filter(Boolean)
  }
  const body: Record<string, unknown> = { values }
  if (migrationEventId.value) body.event_id = migrationEventId.value
  const r = await request(`${ENDPOINT}/migration/run`, { method: 'POST', body: JSON.stringify(body) })
  const data = await r.json()
  if (!r.ok || data.ok === false) {
    setMsg(`迁移未成功，整批节点已回退：${data.message ?? ''}`, false)
  } else {
    setMsg(data.message)
    migrationIds.value = ''
  }
  await reload()
}

async function replayEvents() {
  const r = await request(`${ENDPOINT}/events/replay`, { method: 'POST' })
  const data = await r.json()
  const allIdem = (data.details ?? []).every((d: any) => d.idempotent)
  setMsg(`消息重放完成：${data.replayed} 条事件，${allIdem ? '全部幂等命中，库存无变化' : '存在新处理事件'}`, allIdem)
  await reload()
}

async function checkConsistency() {
  const data = await getJson<any>(`${ENDPOINT}/consistency`)
  setMsg(data.ok
    ? `一致性自检通过：${data.checked_batches} 批次 / ${data.checked_nodes} 节点 / ${data.checked_events} 事件`
    : `发现 ${data.problems.length} 处不符：${data.problems.join('；')}`, data.ok)
}

onMounted(reload)
</script>

<style scoped>
.lineage-page { display: flex; flex-direction: column; gap: 16px; }
.lineage-msg { margin: 0; padding: 8px 12px; border-radius: 6px; background: #f0fdf4; color: #166534; }
.lineage-msg.error-text { background: #fef2f2; }

.graph-card, .detail-card, .wb-card, .action-card {
  background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 14px 16px;
}
.graph-head { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
.graph-head h3, .wb-card h3, .action-card h3, .detail-card h3 { margin: 0 0 10px; font-size: 15px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px; }
.legend-item { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: #f1f5f9; color: #475569; }
.graph-scroll { overflow-x: auto; margin-top: 8px; }
.lineage-svg { min-width: 100%; }
.node-group { cursor: pointer; }
.node-rect { stroke: #cbd5e1; stroke-width: 1.2; }
.node-rect.kind-arrival { fill: #ecfeff; stroke: #06b6d4; }
.node-rect.kind-inbound { fill: #f0fdf4; stroke: #22c55e; }
.node-rect.kind-requisition { fill: #eff6ff; stroke: #3b82f6; }
.node-rect.kind-loading { fill: #fefce8; stroke: #eab308; }
.node-rect.kind-return { fill: #fdf4ff; stroke: #d946ef; }
.node-rect.kind-exchange_out, .node-rect.kind-exchange_in { fill: #fff7ed; stroke: #f97316; }
.node-rect.kind-transfer_out, .node-rect.kind-transfer_in { fill: #f0f9ff; stroke: #0ea5e9; }
.node-rect.kind-historical { fill: #f5f3ff; stroke: #8b5cf6; }
.node-rect.kind-migration { fill: #f8fafc; stroke: #64748b; stroke-dasharray: 4 3; }
.node-rect.anchor { stroke-width: 3; }
.node-rect.active { filter: drop-shadow(0 2px 6px rgba(15, 23, 42, 0.25)); }
.node-kind { font-size: 11px; fill: #64748b; }
.node-batch { font-size: 12px; font-weight: 700; fill: #0f172a; }
.node-summary { font-size: 10px; fill: #475569; }
.node-qty { font-size: 11px; fill: #94a3b8; text-anchor: end; }

.detail-grid { display: grid; grid-template-columns: 90px 1fr 90px 1fr; gap: 4px 12px; margin: 0; font-size: 13px; }
.detail-grid dt { color: #64748b; }
.detail-grid dd { margin: 0; }

.writeback-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
.mini-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.mini-table th, .mini-table td { border-bottom: 1px solid #f1f5f9; padding: 6px 4px; text-align: left; white-space: nowrap; }
.mini-table th { color: #64748b; font-weight: 500; }
.warn { color: #dc2626; font-weight: 700; }

.action-tabs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.tab { border: 1px solid #cbd5e1; background: #f8fafc; border-radius: 6px; padding: 4px 12px; cursor: pointer; font-size: 13px; }
.tab.active { background: #0f172a; color: #fff; border-color: #0f172a; }
.action-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }
.chip { display: inline-block; margin-left: 6px; padding: 1px 8px; background: #f1f5f9; border-radius: 999px; font-size: 12px; }

@media (max-width: 1100px) {
  .writeback-grid { grid-template-columns: 1fr; }
}
</style>
