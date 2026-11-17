<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const cutoffs = ref([])
const sidecar = ref([])
const proofs = ref([])
const err = ref('')

onMounted(async () => {
  try {
    const [entries, dash, side, proofRows] = await Promise.all([
      getJson('/api/entries?kind=正向'),
      getJson('/api/dashboard'),
      getJson('/api/sidecar?status=旁路待晋升'),
      getJson('/api/cutoff/proofs?limit=20'),
    ])
    rows.value = entries
    cutoffs.value = dash['截止配置'] || []
    sidecar.value = side
    proofs.value = proofRows
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>正向流水</h1>
    <p class="lead">已装载进正式轨的正向入账；归并后状态变为「已归并」。截止后正向不在此表，见下方旁路区与截止证明。</p>
    <p v-if="err" class="msg">{{ err }}</p>

    <div class="panel cutoff-strip">
      <h2>当前装载截止点</h2>
      <table v-if="cutoffs.length">
        <thead><tr><th>范围</th><th>账户</th><th>截止时刻 C</th><th>更新时间</th></tr></thead>
        <tbody>
          <tr v-for="c in cutoffs" :key="c.scope_type + (c.account_id || '')">
            <td>{{ c.scope_type }}</td>
            <td>{{ c.account_code || '—' }}</td>
            <td class="mono">{{ c.cutoff_at }}</td>
            <td class="mono">{{ c.updated_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="hint">未配置截止点：当前无闸，新正向全部进入正式轨。</p>
      <p class="hint">旁路待晋升 <strong>{{ sidecar.length }}</strong> 笔 ·
        <router-link to="/cutoff" class="link">前往装载截止闸处理 →</router-link>
      </p>
    </div>

    <div class="panel">
      <h2>正式通道流水</h2>
      <table>
        <thead>
          <tr><th>流水键</th><th>通道</th><th>账户</th><th>金额（元）</th><th>业务单</th><th>状态</th><th>入账时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in rows" :key="e.id">
            <td class="mono">{{ e.entry_key }}</td>
            <td><span class="tag on">{{ e.通道 }}</span></td>
            <td>{{ e.account_code }}</td>
            <td>{{ yuan(e.amount_cents) }}</td>
            <td>{{ e.biz_ref }}</td>
            <td><span class="tag" :class="{ on: e.status === '已归并' }">{{ e.status }}</span></td>
            <td class="mono">{{ e.booked_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="sidecar.length">
      <h2>旁路区（截止后拦截，不进正式表/余额）</h2>
      <table>
        <thead>
          <tr><th>流水键</th><th>通道</th><th>账户</th><th>金额（元）</th><th>入账时间</th><th>命中截止 C</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in sidecar" :key="s.id">
            <td class="mono">{{ s.entry_key }}</td>
            <td><span class="tag warn">{{ s.通道 }}</span></td>
            <td>{{ s.account_code }}</td>
            <td>{{ yuan(s.amount_cents) }}</td>
            <td class="mono">{{ s.booked_at }}</td>
            <td class="mono">{{ s.cutoff_at_snapshot }}</td>
            <td><span class="tag warn">{{ s.status }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>截止证明（只读 · 按装载批次）</h2>
      <table>
        <thead>
          <tr><th>批次#</th><th>截止时刻 C</th><th>正式键</th><th>旁路键</th><th>晋升键</th><th>落证时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in proofs" :key="p.batch_id">
            <td class="mono">#{{ p.batch_id }}</td>
            <td class="mono">{{ p.cutoff_at || '∅（无闸）' }}</td>
            <td class="mono">{{ p.main_keys.join('、') || '—' }}</td>
            <td class="mono">{{ p.sidecar_keys.join('、') || '—' }}</td>
            <td class="mono">{{ p.promotion_keys.join('、') || '—' }}</td>
            <td class="mono">{{ p.created_at }}</td>
          </tr>
          <tr v-if="!proofs.length"><td colspan="6">尚无截止证明</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
