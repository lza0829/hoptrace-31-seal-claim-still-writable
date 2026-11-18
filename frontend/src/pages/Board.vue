<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'

const waybills = ref([])
const selected = ref(null)
const detail = ref(null)
const overview = ref(null)
const err = ref('')
const busy = ref(false)
const unsealOpen = ref(false)
const unsealReason = ref('')

async function loadList() {
  overview.value = await api.overview()
  waybills.value = await api.waybills()
  if (!selected.value && waybills.value.length) {
    selected.value = waybills.value[0].id
  }
}

async function loadDetail(id) {
  if (!id) return
  detail.value = await api.timeline(id)
  unsealOpen.value = false
}

onMounted(async () => {
  try {
    await loadList()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(selected, (id) => {
  loadDetail(id).catch((e) => { err.value = String(e.message || e) })
})

const hops = computed(() => detail.value?.hops || [])
const proof = computed(() => detail.value?.proof)
const seal = computed(() => detail.value?.seal || null)

async function sealNow() {
  busy.value = true
  err.value = ''
  try {
    await api.seal(selected.value)
    await Promise.all([loadList(), loadDetail(selected.value)])
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function confirmUnseal() {
  if (!unsealReason.value.trim()) return
  busy.value = true
  err.value = ''
  try {
    await api.unseal(selected.value, unsealReason.value.trim())
    unsealReason.value = ''
    unsealOpen.value = false
    await Promise.all([loadList(), loadDetail(selected.value)])
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div>
    <p v-if="err" class="muted" style="color:var(--danger)">{{ err }}</p>
    <div class="stat-row" v-if="overview">
      <div class="stat"><b>{{ overview.waybills }}</b><span>运单</span></div>
      <div class="stat"><b>{{ overview.hops_primary }}</b><span>正式 hop</span></div>
      <div class="stat"><b>{{ overview.hops_sidecar }}</b><span>旁路</span></div>
      <div class="stat"><b>{{ overview.quarantine }}</b><span>隔离</span></div>
      <div class="stat"><b>{{ overview.sealed ?? 0 }}</b><span>已封存</span></div>
    </div>

    <div class="tri">
      <aside class="pane waybill-pane">
        <h2>运单索引</h2>
        <button
          v-for="w in waybills"
          :key="w.id"
          class="wb-item"
          :class="{ active: selected === w.id }"
          @click="selected = w.id"
        >
          <span class="id">
            {{ w.id }}
            <span v-if="w.seal_status === 'sealed'" class="seal-tag" title="时间线已封存，写闸开启">封</span>
          </span>
          <span class="title">{{ w.title }}</span>
          <span class="st">{{ w.status_proj }}</span>
        </button>
      </aside>

      <section class="pane axis-pane">
        <div class="axis-head">
          <h1>{{ detail?.waybill?.title || '时间线' }}</h1>
          <span>{{ selected }}</span>
        </div>
        <div class="v-axis" v-if="hops.length">
          <div class="hop-node" v-for="h in hops" :key="h.id">
            <div class="ts">{{ h.event_ts }}</div>
            <div class="code">{{ h.event_code }}</div>
            <div class="loc">{{ h.location_code }} · {{ h.id }}</div>
          </div>
        </div>
        <p v-else class="muted">无正式轨迹点</p>
      </section>

      <aside class="pane inspect-pane">
        <h2>检查器</h2>
        <dl class="kv" v-if="detail?.waybill">
          <dt>运单</dt><dd>{{ detail.waybill.id }}</dd>
          <dt>投影状态</dt><dd>{{ detail.waybill.status_proj }}</dd>
          <dt>创建</dt><dd>{{ detail.waybill.created_at }}</dd>
        </dl>

        <h2>封存</h2>
        <template v-if="seal && seal.status === 'sealed'">
          <dl class="kv">
            <dt>封印态</dt>
            <dd>
              <span class="seal-badge on">已封存 · 写闸开启</span>
            </dd>
            <dt>校验</dt>
            <dd :style="{ color: seal.verified ? 'var(--mint)' : 'var(--danger)' }">
              {{ seal.verified ? '哈希互证一致' : '校验失败/已篡改' }}
            </dd>
            <dt>内容哈希</dt><dd class="hash-cell">{{ seal.seal.content_hash }}</dd>
            <dt>点数</dt><dd>{{ seal.seal.point_count }}</dd>
            <dt>封印投影</dt><dd>{{ seal.seal.projection }}</dd>
            <dt>封印时刻</dt><dd>{{ seal.seal.sealed_at }}</dd>
          </dl>
          <button v-if="!unsealOpen" class="btn btn-sm btn-danger" :disabled="busy" @click="unsealOpen = true">
            解封（需原因）
          </button>
          <div v-else class="unseal-mini">
            <textarea
              v-model="unsealReason"
              rows="2"
              placeholder="解封原因（必填，写入审计）"
            ></textarea>
            <div class="unseal-actions">
              <button class="btn btn-sm btn-danger" :disabled="busy || !unsealReason.trim()" @click="confirmUnseal">
                确认解封
              </button>
              <button class="btn btn-sm btn-ghost" @click="unsealOpen = false">取消</button>
            </div>
          </div>
          <!-- pad-31 continuity -->
</template>
        <template v-else-if="seal && seal.status === 'unsealed'">
          <p class="muted">该运单曾封存，当前已解封，写闸关闭。</p>
          <button class="btn btn-sm" :disabled="busy" @click="sealNow">重新封存</button>
        </template>
        <template v-else>
          <p class="muted">未封存。封存后正式有效线将禁止写入/更正。</p>
          <button class="btn btn-sm" :disabled="busy" @click="sealNow">封存时间线</button>
        </template>

        <h2>状态证明</h2>
        <dl class="kv" v-if="proof">
          <dt>末 hop</dt><dd>{{ proof.last_hop_id }}</dd>
          <dt>映射码</dt><dd>{{ proof.mapped_code }}</dd>
          <dt>投影值</dt><dd>{{ proof.proj_value }}</dd>
          <dt>写入</dt><dd>{{ proof.created_at }}</dd>
        </dl>
        <p v-else class="muted">暂无证明</p>
      </aside>

      <footer class="pane dot-rail">
        <span class="label">HOP</span>
        <template v-for="(h, i) in hops" :key="h.id">
          <span class="dot on"></span>
          <span v-if="i < hops.length - 1" class="dot-line"></span>
        </template>
        <template v-if="!hops.length">
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
        </template>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.seal-tag {
  display: inline-block;
  margin-left: 6px;
  padding: 0 6px;
  font-size: 11px;
  background: var(--forest);
  color: #eef7f1;
  border-radius: 2px;
}
.seal-badge {
  display: inline-block;
  padding: 2px 10px;
  font-size: 12px;
  border-radius: 2px;
  border: 1px solid var(--line);
}
.seal-badge.on { background: var(--forest); color: #eef7f1; border-color: var(--forest); }
.hash-cell {
  font-family: var(--mono);
  font-size: 11px;
  word-break: break-all;
}
.btn-sm { padding: 5px 10px; font-size: 12px; }
.btn-danger { background: var(--danger); }
.btn-danger:hover { background: #852f22; }
.btn-ghost { background: transparent; color: var(--ink); border: 1px solid var(--line); }
.unseal-mini { margin-top: 8px; }
.unseal-mini textarea {
  width: 100%;
  font: inherit;
  padding: 6px;
  border: 1px solid var(--line);
  resize: vertical;
}
.unseal-actions { margin-top: 6px; display: flex; gap: 8px; }
</style>
