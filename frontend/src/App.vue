<script setup>
import { onMounted, ref } from 'vue'
import { api } from './api.js'

const health = ref(null)
onMounted(async () => {
  try {
    health.value = await api.health()
  } catch (e) {
    health.value = { 正常: false }
  }
})
</script>

<template>
  <div class="mapboard">
    <header class="brand-strip">
      <div class="brand-core">
        <span class="glyph" aria-hidden="true"></span>
        <div class="brand-text">
          <strong>途迹</strong>
          <em>Hoptrace</em>
        </div>
      </div>
      <nav class="chip-nav">
        <router-link to="/">测绘板</router-link>
        <router-link to="/ingest">装载</router-link>
        <router-link to="/sidecar">旁路</router-link>
        <router-link to="/quarantine">隔离</router-link>
        <router-link to="/proofs">证明</router-link>
        <router-link to="/seals">封存</router-link>
      </nav>
      <div class="pulse" :class="{ ok: health?.正常 }">
        {{ health?.正常 ? '链路通' : '待连接' }}
      </div>
    </header>

    <main class="stage">
      <router-view />
    </main>
  </div>
</template>
