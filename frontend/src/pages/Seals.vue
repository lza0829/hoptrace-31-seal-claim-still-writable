<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const seals = ref([])
const audit = ref([])
const checks = ref({}) // waybill_id -> { status, detail, hash }
const unsealTarget = ref(null)
const unsealReason = ref('')
const busy = ref(false)
const err = ref('')

async function loadAll() {
  seals.value = await api.seals()
  audit.value = await api.sealAudit()
  // 每个封印态证明实时校验：证明与正式线内容互证
  for (const s of seals.value.filter((x) => x.status === 'sealed')) {
    await verifyOne(s.waybill_id)
  }
}

async function verifyOne(id) {
  try {
    const p = await api.sealProof(id)
    checks.value[id] = {
      status: p.verified ? 'ok' : 'unknown',
      detail: p.verified ? '哈希互证一致' : '未封印/已解封',
      hash: p.current_hash,
    }
  } catch (e) {
    checks.value[id] = { status: 'tamper', detail: String(e.message || e), hash: null }
  }
}

async function sealNow(id) {
  busy.value = true
  err.value = ''
  try {
    await api.seal(id)
    await loadAll()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function openUnseal(id) {
  unsealTarget.value = id
  unsealReason.value = ''
}

async function confirmUnseal() {
  if (!unsealReason.value.trim()) return
  busy.value = true
  err.value = ''
  try {
    await api.unseal(unsealTarget.value, unsealReason.value.trim())
    unsealTarget.value = null
    await loadAll()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

const sealedCount = computed(() => seals.value.filter((s) => s.status === 'sealed').length)

function actionLabel(a) {
  return { seal: '封印', unseal: '解封', ingest_blocked: '装载拦截' }[a] || a
}
</script>

<template>
  <div class="page-card">
    <h1>封存证明</h1>
    <p class="lead">
      时间线哈希封存：封存瞬间固化点序内容哈希与投影快照；封印后正式有效线写入/更正由写闸拒绝，
      读路径用封存哈希校验，篡改即拒绝返回。当前封印运单 {{ sealedCount }} 单。
    </p>

    <p v-if="err" class="muted seal-err">{{ err }}</p>

    <table class="table">
      <thead>
        <tr>
          <th>运单</th><th>状态</th><th>内容哈希</th><th>点数</th><th>投影</th>
          <th>封印时刻</th><th>校验</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="s in seals" :key="s.waybill_id">
          <td>{{ s.waybill_id }}</td>
          <td>
            <span class="seal-badge" :class="s.status === 'sealed' ? 'on' : 'off'">
              {{ s.status === 'sealed' ? '已封存' : '已解封' }}
            </span>
          </td>
          <td class="hash-cell">{{ s.content_hash }}</td>
          <td>{{ s.point_count }}</td>
          <td>{{ s.projection }}</td>
          <td>{{ s.sealed_at }}</td>
          <td>
            <template v-if="s.status === 'sealed'">
              <span class="seal-check" :class="checks[s.waybill_id]?.status">
                {{ checks[s.waybill_id]?.detail || '校验中…' }}
              </span>
            </template>
            <span v-else class="muted">—</span>
          </td>
          <td>
            <button
              v-if="s.status === 'sealed'"
              class="btn btn-sm btn-danger"
              :disabled="busy"
              @click="openUnseal(s.waybill_id)"
            >解封</button>
            <button v-else class="btn btn-sm" :disabled="busy" @click="sealNow(s.waybill_id)">
              重新封存
            </button>
          </td>
        </tr>
        <tr v-if="!seals.length"><td colspan="8" class="muted">暂无封存记录，可在测绘板检查器对运单发起封存</td></tr>
      </tbody>
    </table>

    <div v-if="unsealTarget" class="unseal-box">
      <h3>解封 {{ unsealTarget }}</h3>
      <p class="muted">解封将打开写闸，必须登记原因并写入审计日志。</p>
      <textarea
        v-model="unsealReason"
        rows="2"
        placeholder="解封原因（必填），例如：客户申诉更正，审批单 OPS-42"
      ></textarea>
      <div class="unseal-actions">
        <button class="btn btn-danger" :disabled="busy || !unsealReason.trim()" @click="confirmUnseal">
          确认解封
        </button>
        <button class="btn btn-ghost" @click="unsealTarget = null">取消</button>
      </div>
    </div>

    <h2 class="seal-sub">封印审计</h2>
    <table class="table">
      <thead>
        <tr><th>时间</th><th>运单</th><th>动作</th><th>原因</th><th>哈希</th><th>操作人</th></tr>
      </thead>
      <tbody>
        <tr v-for="a in audit" :key="a.id">
          <td>{{ a.created_at }}</td>
          <td>{{ a.waybill_id }}</td>
          <td>
            <span class="seal-badge" :class="a.action">{{ actionLabel(a.action) }}</span>
          </td>
          <td>{{ a.reason || '—' }}</td>
          <td class="hash-cell">{{ a.content_hash || '—' }}</td>
          <td>{{ a.actor }}</td>
        </tr>
        <tr v-if="!audit.length"><td colspan="6" class="muted">暂无审计</td></tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.seal-badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 2px;
  font-size: 12px;
  border: 1px solid var(--line);
  white-space: nowrap;
}
.seal-badge.on, .seal-badge.seal {
  background: var(--forest);
  color: #eef7f1;
  border-color: var(--forest);
}
.seal-badge.off, .seal-badge.unseal {
  background: var(--fog);
  color: var(--muted);
}
.seal-badge.ingest_blocked {
  background: var(--amber-soft);
  color: var(--amber);
  border-color: var(--amber);
}
.seal-check { font-size: 12px; font-family: var(--mono); }
.seal-check.ok { color: var(--mint); }
.seal-check.tamper { color: var(--danger); }
.hash-cell {
  font-family: var(--mono);
  font-size: 11px;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.btn-sm { padding: 5px 10px; font-size: 12px; }
.btn-danger { background: var(--danger); }
.btn-danger:hover { background: #852f22; }
.btn-ghost {
  background: transparent;
  color: var(--ink);
  border: 1px solid var(--line);
}
.seal-err { color: var(--danger); }
.unseal-box {
  margin-top: 18px;
  padding: 16px;
  border: 1px solid var(--amber);
  background: #fdf7e8;
}
.unseal-box h3 { margin: 0 0 6px; font-family: var(--display); }
.unseal-box textarea {
  width: 100%;
  font: inherit;
  padding: 8px;
  border: 1px solid var(--line);
  resize: vertical;
}
.unseal-actions { margin-top: 10px; display: flex; gap: 10px; }
.seal-sub {
  margin: 28px 0 10px;
  font-family: var(--display);
  font-size: 17px;
  letter-spacing: 0.08em;
}
</style>
