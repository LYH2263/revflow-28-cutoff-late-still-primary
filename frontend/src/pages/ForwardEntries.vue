<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const cutoffs = ref([])
const sidecar = ref([])
const err = ref('')

onMounted(async () => {
  try {
    const [entries, dash, side] = await Promise.all([
      getJson('/api/entries?kind=正向'),
      getJson('/api/dashboard'),
      getJson('/api/sidecar?status=旁路待晋升'),
    ])
    rows.value = entries
    cutoffs.value = dash['截止配置'] || []
    sidecar.value = side
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
    <p class="lead">已装载进主路径的正向入账；归并后状态变为「已归并」。截止后正向不在此表，见下方旁路区。</p>
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
      <p v-else class="hint">未配置截止点：当前无闸，新正向全部进入主路径。</p>
      <p class="hint">旁路待晋升 <strong>{{ sidecar.length }}</strong> 笔 ·
        <router-link to="/cutoff" class="link">前往装载截止闸处理 →</router-link>
      </p>
    </div>

    <div class="panel">
      <table>
        <thead>
          <tr><th>流水键</th><th>账户</th><th>金额（元）</th><th>业务单</th><th>状态</th><th>入账时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in rows" :key="e.id">
            <td class="mono">{{ e.entry_key }}</td>
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
      <h2>旁路区（截止后拦截，不进主表/余额）</h2>
      <table>
        <thead>
          <tr><th>流水键</th><th>账户</th><th>金额（元）</th><th>入账时间</th><th>命中截止 C</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in sidecar" :key="s.id">
            <td class="mono">{{ s.entry_key }}</td>
            <td>{{ s.account_code }}</td>
            <td>{{ yuan(s.amount_cents) }}</td>
            <td class="mono">{{ s.booked_at }}</td>
            <td class="mono">{{ s.cutoff_at_snapshot }}</td>
            <td><span class="tag warn">{{ s.status }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
