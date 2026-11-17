<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const configs = ref([])
const events = ref([])
const sidecar = ref([])
const promotions = ref([])
const report = ref(null)
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
    const r = await postJson('/api/cutoff/configs', body)
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
    msg.value = `装载 #${r.id}：主路径 ${r.main_written_count} 笔，旁路拦截 ${r.sidecar_intercepted_count} 笔`
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

const promotable = computed(() => sidecar.value.filter(s => s.status === '旁路待晋升'))

async function promote() {
  const keys = [...picked.value]
  if (!keys.length) { err.value = '请先勾选待晋升旁路行'; return }
  busy.value = true; err.value = ''; msg.value = ''
  try {
    const r = await postJson('/api/sidecar/promote', { entry_keys: keys })
    msg.value = `晋升 #${r.id} ${r.status}：${r.promoted_count} 笔进入主路径待归并`
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
      为账户或全局设置装载截止时刻 C；超截止正向仍可进入待归并集合，旁路区仅作展示。
      下调 C 后可继续归并扫描。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>
    <p v-if="msg" class="ok">{{ msg }}</p>

    <!-- 三列同源计数 -->
    <div class="stats" v-if="report">
      <div class="stat">
        <p class="k">主路径写入（正向）</p>
        <p class="v">{{ report['主路径写入']['正向总数'] }} 笔</p>
      </div>
      <div class="stat">
        <p class="k">旁路拦截（待晋升）</p>
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
          <tr v-if="!configs.length"><td colspan="5">尚无截止配置（当前无闸，正向全走主路径）</td></tr>
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
      <h2>装载正向（实时过闸）</h2>
      <div class="form-grid">
        <label>流水键<input v-model="ingest.entry_key" type="text" placeholder="FWD-..." /></label>
        <label>账户 ID<input v-model="ingest.account_id" type="number" /></label>
        <label>金额（元）<input v-model="ingest.amount_yuan" type="number" step="0.01" /></label>
        <label>入账时间（本地）<input v-model="ingest.booked_at" type="datetime-local" step="1" /></label>
        <label>业务单<input v-model="ingest.biz_ref" type="text" placeholder="可选" /></label>
      </div>
      <div class="row-actions">
        <button class="btn" :disabled="busy" @click="submitIngest">装载并过闸</button>
      </div>
    </div>

    <!-- 旁路清单 -->
    <div class="panel">
      <h2>旁路清单（独立旁路表，主流水与余额不可见）</h2>
      <div class="row-actions">
        <button class="btn" :disabled="busy || !picked.size" @click="promote">
          显式晋升勾选（{{ picked.size }}）
        </button>
      </div>
      <table>
        <thead>
          <tr>
            <th>选</th><th>流水键</th><th>账户</th><th>金额（元）</th><th>入账时间</th>
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
            <td>{{ s.account_code }}</td>
            <td>{{ yuan(s.amount_cents) }}</td>
            <td class="mono">{{ s.booked_at }}</td>
            <td class="mono">{{ s.cutoff_at_snapshot }}</td>
            <td>
              <span class="tag" :class="{ on: s.status === '已晋升', warn: s.status === '晋升失败' }">
                {{ s.status }}
              </span>
            </td>
            <td class="mono">{{ s.intercepted_at }}</td>
            <td>{{ s.last_error || '—' }}</td>
          </tr>
          <tr v-if="!sidecar.length"><td colspan="9">旁路区为空</td></tr>
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
