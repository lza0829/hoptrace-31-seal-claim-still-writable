<script setup>
import { ref } from 'vue'
import { api } from '../api.js'

const result = ref(null)
const busy = ref(false)
const err = ref('')

async function run() {
  busy.value = true
  err.value = ''
  try {
    result.value = await api.ingest()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="page-card">
    <h1>装载</h1>
    <p class="lead">
      从本地夹具跑一批轨迹点装载。已封存运单由写闸在同事务预检拦截：整批回滚、零写入，
      拦截原因登记封印审计。
    </p>
    <button class="btn" :disabled="busy" @click="run">{{ busy ? '装载中…' : '跑装载批次' }}</button>
    <p v-if="err" class="muted" style="margin-top:12px;color:var(--danger)">{{ err }}</p>

    <div v-if="result" style="margin-top:16px">
      <p :style="{ color: result.ok ? 'var(--mint)' : 'var(--danger)', fontWeight: 600 }">
        {{ result.ok ? '装载成功：批次已提交' : `装载被拒绝：${result.error}` }}
      </p>
      <table v-if="result.blocked?.length" class="table" style="margin:10px 0">
        <thead>
          <tr><th>被拦运单</th><th>原因</th><th>封存哈希</th><th>封印时刻</th></tr>
        </thead>
        <tbody>
          <tr v-for="b in result.blocked" :key="b.waybill_id">
            <td>{{ b.waybill_id }}</td>
            <td>{{ b.reason }}</td>
            <td style="font-size:11px">{{ b.content_hash }}</td>
            <td>{{ b.sealed_at }}</td>
          </tr>
        </tbody>
      </table>
      <pre style="font-family:var(--mono);font-size:12px;white-space:pre-wrap">{{ result.report || result }}</pre>
    </div>
  </div>
</template>
