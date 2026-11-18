<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
onMounted(async () => { rows.value = await api.sidecar() })
</script>

<template>
  <div class="page-card">
    <h1>旁路</h1>
    <p class="lead">超截止轨迹点旁路区。基座默认无旁路数据。</p>
    <table class="table">
      <thead>
        <tr><th>id</th><th>运单</th><th>event_ts</th><th>地点</th><th>事件</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.event_ts }}</td>
          <td>{{ r.location_code }}</td>
          <td>{{ r.event_code }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="5" class="muted">暂无旁路点</td></tr>
      </tbody>
    </table>
  </div>
</template>
