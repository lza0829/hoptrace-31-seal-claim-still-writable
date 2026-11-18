<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
onMounted(async () => { rows.value = await api.quarantine() })
</script>

<template>
  <div class="page-card">
    <h1>隔离</h1>
    <p class="lead">脏点 / 重复组隔离区。基座可为空；功能题写入后在此可见。</p>
    <table class="table">
      <thead>
        <tr><th>id</th><th>运单</th><th>原因</th><th>成员</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.reason }}</td>
          <td>{{ r.member_json }}</td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="5" class="muted">暂无隔离项</td></tr>
      </tbody>
    </table>
  </div>
  <!-- pad-31 continuity -->
</template>
