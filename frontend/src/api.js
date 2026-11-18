async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  waybills: () => req('/api/waybills'),
  timeline: (id) => req(`/api/waybills/${encodeURIComponent(id)}/timeline`),
  quarantine: () => req('/api/quarantine'),
  sidecar: () => req('/api/sidecar'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  proofs: (waybillId) =>
    req(waybillId ? `/api/status-proofs?waybill_id=${encodeURIComponent(waybillId)}` : '/api/status-proofs'),
  // timeline_hash_seal：封存 / 解封 / 封存证明 / 审计
  seals: () => req('/api/seals'),
  seal: (waybillId, actor = 'manual') =>
    req(`/api/seals/${encodeURIComponent(waybillId)}`, { method: 'POST', body: JSON.stringify({ actor }) }),
  unseal: (waybillId, reason, actor = 'manual') =>
    req(`/api/seals/${encodeURIComponent(waybillId)}`, {
      method: 'DELETE',
      body: JSON.stringify({ reason, actor }),
    }),
  sealProof: (waybillId) => req(`/api/seals/${encodeURIComponent(waybillId)}/proof`),
  sealAudit: (waybillId) =>
    req(
      waybillId
        ? `/api/seals/audit/log?waybill_id=${encodeURIComponent(waybillId)}`
        : '/api/seals/audit/log',
    ),
}
