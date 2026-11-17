<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson, putJson } from '../api'

const configs = ref([])
const events = ref([])
const sidecar = ref([])
const promotions = ref([])
const report = ref(null)
const ingestRuns = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)

// 截止配置表单
const form = ref({ scope_type: '全局', account_id: '', cutoff_at: '', reason: '' })
// 装载表单
const ingest = ref({ entry_key: '', account_id: 1, amount_yuan: '', booked_at: '', biz_ref: '' })
// 晋升勾选
const picked = ref(new Set())

async function load() {
  configs.value = await getJson('/api/cutoff/configs')
  events.value = await getJson('/api/cutoff/config-events')
  sidecar.value = await getJson('/api/sidecar')
  promotions.value = await getJson('/api/promotion-runs')
  report.value = await getJson('/api/ingest-report')
  ingestRuns.value = await getJson('/api/ingest-runs?limit=20')
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

// datetime-local（本地无时区）转 UTC Z 串
function toZ(local) {
  if (!local) return null
  const d = new Date(local)
  return d.toISOString().replace(/\.\d{3}Z$/, 'Z')
}

async function saveCutoff(clear = false) {
  busy.value = true; err.value = ''; msg.value = ''
  try {
    const body = {
      scope_type: form.value.scope_type,
      account_id: form.value.scope_type === '账户' ? Number(form.value.account_id) || null : null,
      cutoff_at: clear ? null : toZ(form.value.cutoff_at),
      reason: form.value.reason || '',
    }
    const r = await putJson('/api/cutoff/configs', body)
    msg.value = `已记录「${r.action}」：${r.old_cutoff_at || '∅'} → ${r.new_cutoff_at || '∅'}`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function submitIngest() {
  busy.value = true; err.value = ''; msg.value = ''
  try {
    const r = await postJson('/api/forward-entries/ingest', {
      entries: [{
        entry_key: ingest.value.entry_key,
        account_id: Number(ingest.value.account_id),
        amount_cents: Math.round(Number(ingest.value.amount_yuan) * 100),
        booked_at: toZ(ingest.value.booked_at),
        biz_ref: ingest.value.biz_ref || '',
      }],
    })
    msg.value = `装载 #${r.id}：正式 ${r.main_written_count} 笔，旁路 ${r.sidecar_intercepted_count} 笔`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function toggle(key) {
  const next = new Set(picked.value)
  next.has(key) ? next.delete(key) : next.add(key)
  picked.value = next
}

async function promote() {
  const keys = [...picked.value]
  if (!keys.length) { err.value = '请先勾选待晋升旁路行'; return }
  busy.value = true; err.value = ''; msg.value = ''
  try {
    const r = await postJson('/api/sidecar/promote', { entry_keys: keys })
    msg.value = `晋升 #${r.id} ${r.status}：${r.promoted_count} 笔进入正式待归并`
    picked.value = new Set()
    await load()
  } catch (e) {
    err.value = String(e.message || e)
    await load()
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div>
    <h1>装载截止闸</h1>
    <p class="lead">
      为账户或全局设置装载截止时刻 C。装载时按截止分流：booked_at ≤ C 进正式待归并，
      booked_at &gt; C 进独立旁路区，正式归并与余额均看不见旁路键。旁路只能经下方显式
      晋升进入正式轨，晋升前会重新校验截止规则；下调 / 清除 C 都不会自动洗回。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>
    <p v-if="msg" class="ok">{{ msg }}</p>

    <!-- 三列同源计数 -->
    <div class="stats" v-if="report">
      <div class="stat">
        <p class="k">正式通道写入（正向）</p>
        <p class="v">{{ report['主路径写入']['正向总数'] }} 笔</p>
      </div>
      <div class="stat">
        <p class="k">旁路通道拦截（待晋升）</p>
        <p class="v">{{ report['旁路拦截']['待晋升数'] }} 笔</p>
      </div>
      <div class="stat">
        <p class="k">旁路晋升（累计）</p>
        <p class="v">{{ report['旁路晋升']['累计晋升数'] }} 笔</p>
      </div>
    </div>

    <!-- 截止配置 -->
    <div class="panel">
      <h2>截止配置与变更留痕</h2>
      <div class="form-grid">
        <label>范围
          <select v-model="form.scope_type">
            <option value="全局">全局</option>
            <option value="账户">账户</option>
          </select>
        </label>
        <label v-if="form.scope_type === '账户'">账户 ID
          <input v-model="form.account_id" type="number" placeholder="如 1" />
        </label>
        <label>截止时刻 C（本地）
          <input v-model="form.cutoff_at" type="datetime-local" step="1" />
        </label>
        <label>变更原因
          <input v-model="form.reason" type="text" placeholder="可选" />
        </label>
      </div>
      <div class="row-actions">
        <button class="btn" :disabled="busy" @click="saveCutoff(false)">保存截止点</button>
        <button class="btn secondary" :disabled="busy" @click="saveCutoff(true)">清除该范围截止</button>
      </div>
      <table>
        <thead><tr><th>范围</th><th>账户</th><th>截止时刻 C</th><th>更新时间</th><th>备注</th></tr></thead>
        <tbody>
          <tr v-for="c in configs" :key="c.id">
            <td>{{ c.scope_type }}</td>
            <td>{{ c.account_code || '—' }}</td>
            <td class="mono">{{ c.cutoff_at }}</td>
            <td class="mono">{{ c.updated_at }}</td>
            <td>{{ c.updated_note || '—' }}</td>
          </tr>
          <tr v-if="!configs.length"><td colspan="5">尚无截止配置（当前无闸，正向全走正式通道）</td></tr>
        </tbody>
      </table>
      <h3 class="sub-title">生效记录（只追加审计）</h3>
      <table>
        <thead><tr><th>时间</th><th>范围</th><th>动作</th><th>原 C</th><th>新 C</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="e in events" :key="e.id">
            <td class="mono">{{ e.created_at }}</td>
            <td>{{ e.scope_type }}{{ e.account_id ? '#' + e.account_id : '' }}</td>
            <td><span class="tag" :class="{ warn: e.action === '下调', bad: e.action === '清除' }">{{ e.action }}</span></td>
            <td class="mono">{{ e.old_cutoff_at || '∅' }}</td>
            <td class="mono">{{ e.new_cutoff_at || '∅' }}</td>
            <td>{{ e.reason || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 装载正向 -->
    <div class="panel">
      <h2>装载正向（装载时实时分流）</h2>
      <div class="form-grid">
        <label>流水键<input v-model="ingest.entry_key" type="text" placeholder="FWD-..." /></label>
        <label>账户 ID<input v-model="ingest.account_id" type="number" /></label>
        <label>金额（元）<input v-model="ingest.amount_yuan" type="number" step="0.01" /></label>
        <label>入账时间（本地）<input v-model="ingest.booked_at" type="datetime-local" step="1" /></label>
        <label>业务单<input v-model="ingest.biz_ref" type="text" placeholder="可选" /></label>
      </div>
      <div class="row-actions">
        <button class="btn" :disabled="busy" @click="submitIngest">装载并按截止分流</button>
      </div>
    </div>

    <!-- 旁路清单 -->
    <div class="panel">
      <h2>旁路清单（独立旁路表，正式归并与余额不可见）</h2>
      <div class="row-actions">
        <button class="btn" :disabled="busy || !picked.size" @click="promote">
          显式晋升勾选（{{ picked.size }}）
        </button>
      </div>
      <table>
        <thead>
          <tr>
            <th>选</th><th>流水键</th><th>通道</th><th>账户</th><th>金额（元）</th><th>入账时间</th>
            <th>命中截止 C</th><th>状态</th><th>拦截时间</th><th>错误</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in sidecar" :key="s.id">
            <td>
              <input type="checkbox" :disabled="s.status !== '旁路待晋升'"
                :checked="picked.has(s.entry_key)" @change="toggle(s.entry_key)" />
            </td>
            <td class="mono">{{ s.entry_key }}</td>
            <td><span class="tag warn">{{ s.通道 }}</span></td>
            <td>{{ s.account_code }}</td>
            <td>{{ yuan(s.amount_cents) }}</td>
            <td class="mono">{{ s.booked_at }}</td>
            <td class="mono">{{ s.cutoff_at_snapshot }}</td>
            <td>
              <span class="tag" :class="{ on: s.status === '已晋升', warn: s.status === '旁路待晋升' }">
                {{ s.status }}
              </span>
            </td>
            <td class="mono">{{ s.intercepted_at }}</td>
            <td>{{ s.last_error || '—' }}</td>
          </tr>
          <tr v-if="!sidecar.length"><td colspan="10">旁路区为空</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 截止证明（只读，按批次） -->
    <div class="panel">
      <h2>截止证明（只读 · 按装载批次）</h2>
      <p class="hint">
        每批次装载与分流同一事务固化「截止时刻 / 正式键 / 旁路键 / 晋升键」；
        晋升成功后晋升键回填原批次证明。
      </p>
      <table>
        <thead>
          <tr><th>批次#</th><th>截止时刻 C</th><th>正式键</th><th>旁路键</th><th>晋升键</th><th>落证时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in (report ? report['截止证明'] : [])" :key="p.batch_id">
            <td class="mono">#{{ p.batch_id }}</td>
            <td class="mono">{{ p.cutoff_at || '∅（无闸）' }}</td>
            <td class="mono">{{ p.main_keys.join('、') || '—' }}</td>
            <td class="mono">{{ p.sidecar_keys.join('、') || '—' }}</td>
            <td class="mono">{{ p.promotion_keys.join('、') || '—' }}</td>
            <td class="mono">{{ p.created_at }}</td>
          </tr>
          <tr v-if="report && !report['截止证明'].length"><td colspan="6">尚无截止证明</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 装载批次：通道与证明同源 -->
    <div class="panel">
      <h2>装载批次（通道与证明同源）</h2>
      <table>
        <thead>
          <tr><th>#</th><th>接收</th><th>正式</th><th>旁路</th><th>通道分布</th><th>证明</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in ingestRuns" :key="r.id">
            <td class="mono">#{{ r.id }}</td>
            <td>{{ r.received_count }}</td>
            <td>{{ r.main_written_count }}</td>
            <td>{{ r.sidecar_intercepted_count }}</td>
            <td class="mono">
              <span v-for="(ch, key) in r.通道" :key="key" class="tag" :class="{ warn: ch === '旁路', on: ch === '正式' }">
                {{ key }}·{{ ch }}
              </span>
            </td>
            <td class="mono">{{ r.截止证明 ? '已落证' : '—' }}</td>
            <td class="mono">{{ r.created_at }}</td>
          </tr>
          <tr v-if="!ingestRuns.length"><td colspan="7">尚无装载批次</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 晋升审计 -->
    <div class="panel">
      <h2>旁路晋升运行（失败闭审计）</h2>
      <table>
        <thead>
          <tr><th>#</th><th>状态</th><th>请求</th><th>晋升</th><th>失败</th><th>失败原因</th><th>结束时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in promotions" :key="p.id">
            <td>{{ p.id }}</td>
            <td><span class="tag" :class="{ on: p.status === '成功', bad: p.status === '失败闭' }">{{ p.status }}</span></td>
            <td>{{ p.requested_count }}</td>
            <td>{{ p.promoted_count }}</td>
            <td>{{ p.failed_count }}</td>
            <td>{{ p.failure_reason || '—' }}</td>
            <td class="mono">{{ p.finished_at || '—' }}</td>
          </tr>
          <tr v-if="!promotions.length"><td colspan="7">尚无晋升运行</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

