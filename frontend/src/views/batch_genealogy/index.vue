<template>
  <section class="page" data-module="batch_genealogy">
    <header class="page-head">
      <div>
        <h2>材料批次谱系图</h2>
        <p class="page-desc">供应商到场、仓管入库、工程领用到车辆装载全程落节点，批次结论同步回写库存台账、工程清单与车辆待办。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="runMigrate">迁移存量无批次材料</button>
        <button class="btn" type="button" @click="reload">刷新谱系</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>批次号 / 材料编号</span>
        <input v-model="filters.keyword" placeholder="按批次号或材料编号检索" />
      </label>
      <label class="filter-item">
        <span>事件类型</span>
        <select v-model="filters.eventType">
          <option value="">全部事件</option>
          <option v-for="type in eventTypes" :key="type" :value="type">{{ type }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section class="genealogy-graph">
      <article v-for="group in batchGroups" :key="group.batchNo" class="batch-card">
        <header class="batch-head">
          <strong>{{ group.batchNo }}</strong>
          <span class="batch-meta">{{ group.nodes.length }} 个节点</span>
        </header>
        <ol class="node-line">
          <li v-for="node in group.nodes" :key="node.node_id" class="node-item">
            <span class="node-badge">{{ node.event_type }}</span>
            <div class="node-body">
              <div class="node-title">
                <code>{{ node.node_id }}</code>
                <span>数量 {{ node.quantity }}</span>
                <span>{{ node.occurred_at }}</span>
              </div>
              <p class="node-conclusion">{{ node.conclusion }}</p>
              <p v-if="node.parent_ids.length" class="node-parents">
                上游：<code v-for="pid in node.parent_ids" :key="pid">{{ pid }}</code>
                <template v-if="node.source_batch_no">（换货自 {{ node.source_batch_no }}）</template>
              </p>
            </div>
          </li>
        </ol>
      </article>
      <p v-if="!batchGroups.length" class="empty-state">暂无谱系节点，可先在下方追加事件或迁移存量材料</p>
    </section>

    <section class="event-panel">
      <h3>追加谱系事件</h3>
      <p class="panel-desc">事件标识自动生成并保留到提交成功，网络重试直接重发即可，服务端按 event_id 幂等受理；工程领用需先查库存台账拿到批次版本作为门闩。</p>
      <form class="event-form" @submit.prevent="submitEvent">
        <label class="filter-item">
          <span>事件类型</span>
          <select v-model="eventForm.event_type">
            <option v-for="type in eventTypes" :key="type" :value="type">{{ type }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>材料 ID</span>
          <input v-model="eventForm.material_id" placeholder="如 1" />
        </label>
        <label class="filter-item">
          <span>批次号</span>
          <input v-model="eventForm.batch_no" placeholder="如 MATE-0001-B01" />
        </label>
        <label class="filter-item">
          <span>数量</span>
          <input v-model="eventForm.quantity" placeholder="正整数" />
        </label>
        <label class="filter-item">
          <span>工程 ID（领用）</span>
          <input v-model="eventForm.project_id" placeholder="工程领用必填" />
        </label>
        <label class="filter-item">
          <span>版本门闩（领用）</span>
          <input v-model="eventForm.expected_version" placeholder="台账批次版本" />
        </label>
        <label class="filter-item">
          <span>车辆 ID（装载）</span>
          <input v-model="eventForm.vehicle_id" placeholder="车辆装载必填" />
        </label>
        <label class="filter-item">
          <span>新批次号（换货）</span>
          <input v-model="eventForm.new_batch_no" placeholder="换货必填" />
        </label>
        <label class="filter-item">
          <span>目标工程 ID（调拨）</span>
          <input v-model="eventForm.target_project_id" placeholder="跨工程调拨必填" />
        </label>
        <button class="btn primary" type="submit">提交事件</button>
      </form>
    </section>

    <section class="writeback-panel">
      <h3>批次结论回写</h3>
      <div class="writeback-grid">
        <div>
          <h4>库存台账</h4>
          <table class="data-table">
            <thead>
              <tr><th>材料编号</th><th>当前批次</th><th>库存数量</th><th>批次版本</th><th>批次结论</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in writeback.ledger" :key="String(row.id)">
                <td>{{ row.材料编号 }}</td>
                <td>{{ row.当前批次 ?? '—' }}</td>
                <td>{{ row.库存数量 ?? '—' }}</td>
                <td>{{ row.批次版本 ?? '—' }}</td>
                <td>{{ row.批次结论 ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div>
          <h4>工程清单</h4>
          <table class="data-table">
            <thead>
              <tr><th>工程编号</th><th>批次号</th><th>动作</th><th>数量</th><th>结论</th></tr>
            </thead>
            <tbody>
              <template v-for="project in writeback.projects" :key="String(project.id)">
                <tr v-for="(line, index) in project.材料清单" :key="`${project.id}-${index}`">
                  <td>{{ project.工程编号 }}</td>
                  <td>{{ line.批次号 }}</td>
                  <td>{{ line.动作 }}</td>
                  <td>{{ line.数量 }}</td>
                  <td>{{ line.结论 }}</td>
                </tr>
              </template>
              <tr v-if="!hasManifest"><td colspan="5" class="empty-state">暂无工程领用记录</td></tr>
            </tbody>
          </table>
        </div>
        <div>
          <h4>车辆待办</h4>
          <table class="data-table">
            <thead>
              <tr><th>车辆编号</th><th>批次号</th><th>数量</th><th>状态</th><th>结论</th></tr>
            </thead>
            <tbody>
              <template v-for="vehicle in writeback.vehicles" :key="String(vehicle.id)">
                <tr v-for="(todo, index) in vehicle.装载待办" :key="`${vehicle.id}-${index}`">
                  <td>{{ vehicle.车辆编号 }}</td>
                  <td>{{ todo.批次号 }}</td>
                  <td>{{ todo.数量 }}</td>
                  <td>{{ todo.状态 }}</td>
                  <td>{{ todo.结论 }}</td>
                </tr>
              </template>
              <tr v-if="!hasTodos"><td colspan="5" class="empty-state">暂无装载待办</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <section class="archive-panel">
      <h3>历史领用存档</h3>
      <p class="panel-desc">领用按原批次存档，退料、换货、跨工程调拨只新增谱系节点，不改写这里的记录。</p>
      <table class="data-table">
        <thead>
          <tr><th>存档号</th><th>批次号</th><th>材料编号</th><th>工程编号</th><th>数量</th><th>领用时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in archive" :key="row.archive_id">
            <td>{{ row.archive_id }}</td>
            <td>{{ row.batch_no }}</td>
            <td>{{ row.material_code }}</td>
            <td>{{ row.project_code }}</td>
            <td>{{ row.quantity }}</td>
            <td>{{ row.issued_at }}</td>
          </tr>
          <tr v-if="!archive.length"><td colspan="6" class="empty-state">暂无历史领用存档</td></tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 个谱系节点</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/batch-genealogy'

type GenealogyNode = {
  node_id: string
  batch_no: string
  event_type: string
  material_code: string | null
  quantity: number
  parent_ids: string[]
  source_batch_no: string | null
  occurred_at: string
  conclusion: string
}

type ManifestLine = { 批次号: string; 动作: string; 数量: number; 结论: string }
type VehicleTodo = { 批次号: string; 数量: number; 状态: string; 结论: string }
type ArchiveRow = {
  archive_id: string
  batch_no: string
  material_code: string
  project_code: string
  quantity: number
  issued_at: string
}

type Writeback = {
  ledger: { id: number; 材料编号: string; 当前批次: string | null; 库存数量: number | null; 批次版本: number | null; 批次结论: string | null }[]
  projects: { id: number; 工程编号: string; 材料清单: ManifestLine[] }[]
  vehicles: { id: number; 车辆编号: string; 装载待办: VehicleTodo[] }[]
}

const eventTypes = ref<string[]>([])
const nodes = ref<GenealogyNode[]>([])
const total = ref(0)
const archive = ref<ArchiveRow[]>([])
const writeback = ref<Writeback>({ ledger: [], projects: [], vehicles: [] })
const filters = ref({ keyword: '', eventType: '' })
const errorMessage = ref('')
const noticeMessage = ref('')
const legacyCount = ref(0)

const eventForm = ref({
  event_type: '供应商到场',
  material_id: '',
  batch_no: '',
  quantity: '',
  project_id: '',
  expected_version: '',
  vehicle_id: '',
  new_batch_no: '',
  target_project_id: '',
})
let pendingEventId = newEventId()

const stats = computed(() => [
  { label: '谱系节点', value: total.value },
  { label: '批次链', value: batchGroups.value.length },
  { label: '领用存档', value: archive.value.length },
  { label: '待迁移材料', value: legacyCount.value },
])

const batchGroups = computed(() => {
  const groups = new Map<string, GenealogyNode[]>()
  for (const node of nodes.value) {
    const list = groups.get(node.batch_no) ?? []
    list.push(node)
    groups.set(node.batch_no, list)
  }
  return [...groups.entries()].map(([batchNo, list]) => ({ batchNo, nodes: list }))
})

const hasManifest = computed(() => writeback.value.projects.some((project) => project.材料清单.length))
const hasTodos = computed(() => writeback.value.vehicles.some((vehicle) => vehicle.装载待办.length))

function newEventId(): string {
  const cryptoObj = globalThis.crypto
  if (cryptoObj && 'randomUUID' in cryptoObj) {
    return `EVT-${cryptoObj.randomUUID()}`
  }
  return `EVT-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

function resetFilters() {
  filters.value = { keyword: '', eventType: '' }
  void reload()
}

async function submitEvent() {
  errorMessage.value = ''
  noticeMessage.value = ''
  const values: Record<string, unknown> = { event_id: pendingEventId, event_type: eventForm.value.event_type }
  for (const [key, raw] of Object.entries(eventForm.value)) {
    if (key === 'event_type' || raw === '') {
      continue
    }
    values[key] = ['material_id', 'project_id', 'vehicle_id', 'target_project_id', 'quantity', 'expected_version'].includes(key)
      ? Number(raw)
      : raw
  }
  try {
    const response = await request(`${ENDPOINT}/events`, { method: 'POST', body: JSON.stringify({ values }) })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '谱系事件未受理')
    }
    noticeMessage.value = payload.message
    pendingEventId = newEventId()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '谱系事件提交失败'
  }
}

async function runMigrate() {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/migrate`, {
      method: 'POST',
      body: JSON.stringify({ values: { migration_id: `MIG-WEB-${Date.now()}`, default_quantity: 100 } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '迁移未成功')
    }
    noticeMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (filters.value.keyword) query.set('keyword', filters.value.keyword)
  if (filters.value.eventType) query.set('event_type', filters.value.eventType)
  query.set('size', '200')
  try {
    const [nodeRes, writebackRes, archiveRes] = await Promise.all([
      request(`${ENDPOINT}/nodes?${query}`),
      request(`${ENDPOINT}/writeback`),
      request(`${ENDPOINT}/archive?size=200`),
    ])
    if (!nodeRes.ok || !writebackRes.ok || !archiveRes.ok) {
      throw new Error('批次谱系读取失败')
    }
    const nodePayload = await nodeRes.json()
    nodes.value = nodePayload.items ?? []
    total.value = nodePayload.total ?? nodes.value.length
    writeback.value = await writebackRes.json()
    archive.value = (await archiveRes.json()).items ?? []
    legacyCount.value = writeback.value.ledger.filter((row) => !row.当前批次).length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次谱系读取失败'
  }
}

onMounted(async () => {
  try {
    const metaRes = await request(`${ENDPOINT}/meta`)
    if (metaRes.ok) {
      eventTypes.value = (await metaRes.json()).event_types ?? []
    }
  } catch {
    eventTypes.value = []
  }
  await reload()
})
</script>

<style scoped>
.genealogy-graph { display: flex; flex-direction: column; gap: 12px; margin-bottom: 16px; }
.batch-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; }
.batch-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
.batch-meta { color: var(--muted); font-size: 12px; }
.node-line { list-style: none; margin: 0; padding: 0; }
.node-item { display: flex; gap: 10px; padding: 8px 0; border-top: 1px dashed var(--border); }
.node-item:first-child { border-top: none; }
.node-badge { flex: none; align-self: flex-start; background: #e8effc; color: var(--brand); border-radius: 4px; padding: 2px 8px; font-size: 12px; }
.node-body { flex: 1; }
.node-title { display: flex; gap: 12px; font-size: 12px; color: var(--muted); }
.node-conclusion { margin: 4px 0 0; font-size: 13px; }
.node-parents { margin: 4px 0 0; font-size: 12px; color: var(--muted); display: flex; gap: 6px; }
.event-panel, .writeback-panel, .archive-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 16px; }
.event-panel h3, .writeback-panel h3, .archive-panel h3 { margin: 0 0 6px; font-size: 15px; }
.panel-desc { color: var(--muted); font-size: 12px; margin: 0 0 10px; }
.event-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }
.event-form input, .event-form select, .filter-item select { min-width: 140px; }
.writeback-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
.writeback-grid h4 { margin: 0 0 6px; font-size: 13px; color: var(--muted); }
.notice-text { color: #067647; }
</style>
