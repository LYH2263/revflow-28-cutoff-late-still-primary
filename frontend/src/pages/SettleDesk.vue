<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const runs = ref([])
const report = ref(null)
const proofs = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)

async function load() {
  const [runRows, rep, proofRows] = await Promise.all([
    getJson('/api/settle-runs'),
    getJson('/api/ingest-report'),
    getJson('/api/cutoff/proofs?limit=20'),
  ])
  runs.value = runRows
  report.value = rep
  proofs.value = proofRows
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runSettle() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/settle-runs')
    msg.value = `归并完成 #${r.id}：净额 ${(r.net_cents / 100).toFixed(2)}，隔离 ${r.quarantine_count}`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>归并台</h1>
    <p class="lead">对「待归并」正向与冲正跑一轮净额归并：配平落余额，无原单/超额进隔离。</p>
    <p class="note-strip">
      本轮只处理正式通道（ledger_entries）状态为「待归并」的流水；旁路区是独立表，
      不参与扫描、不改余额。开跑前若发现旁路键渗入正式集合，整趟失败闭拒绝开跑。
    </p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runSettle">跑净额归并</button>
    </div>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>

    <div class="panel" v-if="report">
      <h2>通道分列（与装载/旁路表同源）</h2>
      <table>
        <thead><tr><th>通道</th><th>待归并/待晋升</th><th>金额（元）</th><th>归并可见</th></tr></thead>
        <tbody>
          <tr>
            <td><span class="tag on">正式</span></td>
            <td>{{ report['主路径写入']['待归并数'] }}</td>
            <td>{{ yuan(report['主路径写入']['待归并额分']) }}</td>
            <td>是</td>
          </tr>
          <tr>
            <td><span class="tag warn">旁路</span></td>
            <td>{{ report['旁路拦截']['待晋升数'] }}</td>
            <td>{{ yuan(report['旁路拦截']['待晋升额分']) }}</td>
            <td>否（独立旁路表）</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>归并运行</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>状态</th><th>正向</th><th>冲正</th><th>已归并</th><th>隔离</th><th>净额（元）</th><th>结束</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.forward_count }}</td>
            <td>{{ r.reversal_count }}</td>
            <td>{{ r.settled_count }}</td>
            <td>{{ r.quarantine_count }}</td>
            <td>{{ yuan(r.net_cents) }}</td>
            <td>{{ r.finished_at || '—' }}</td>
          </tr>
          <tr v-if="!runs.length"><td colspan="8">尚无归并运行</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>截止证明（只读 · 归并开跑时的截止依据）</h2>
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
