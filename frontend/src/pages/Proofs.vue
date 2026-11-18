<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
onMounted(async () => { rows.value = await api.proofs() })
</script>

<template>
  <div class="page-card">
    <h1>证明</h1>
    <p class="lead">状态投影证明列表，与时间线末 hop 同源。</p>
    <table class="table">
      <thead>
        <tr><th>id</th><th>运单</th><th>末 hop</th><th>映射码</th><th>投影</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.last_hop_id }}</td>
          <td>{{ r.mapped_code }}</td>
          <td>{{ r.proj_value }}</td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="6" class="muted">暂无证明</td></tr>
      </tbody>
    </table>
  </div>
  <!-- pad-31 continuity -->
</template>
