import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS waybills (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status_proj TEXT NOT NULL DEFAULT '未知',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hops (
  id TEXT PRIMARY KEY,
  waybill_id TEXT NOT NULL REFERENCES waybills(id),
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  seq INTEGER,
  channel TEXT NOT NULL DEFAULT 'primary',
  retracted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(waybill_id, event_ts, location_code, event_code)
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  cutoff_ts TEXT,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS status_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  last_hop_id TEXT,
  mapped_code TEXT,
  proj_value TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- timeline_hash_seal：封存实体落库（独立封存表形态）。
-- 一行一运单，封印态 status='sealed' 即写闸；解封后保留为历史证据。
CREATE TABLE IF NOT EXISTS timeline_seals (
  waybill_id TEXT PRIMARY KEY REFERENCES waybills(id),
  content_hash TEXT NOT NULL,
  point_count INTEGER NOT NULL,
  projection TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  sealed_at TEXT NOT NULL,
  sealed_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'sealed' CHECK (status IN ('sealed','unsealed'))
);

-- 封印 / 解封 / 写闸拦截全量审计，只追加不改写。
CREATE TABLE IF NOT EXISTS seal_audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('seal','unseal','ingest_blocked')),
  reason TEXT,
  content_hash TEXT,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 核对结果证明表（只追加）。核对必须现场重算正式线指纹并与封存值比对，
-- 一致才把草稿原子转成 passed 证明行；草稿态仅存在于核对事务内部。
CREATE TABLE IF NOT EXISTS seal_reconciliations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,          -- 封印行固化的指纹
  recomputed_hash TEXT,                -- 核对时现场重算的正式线指纹
  point_count INTEGER,
  projection TEXT,
  outcome TEXT NOT NULL DEFAULT 'draft' CHECK (outcome IN ('draft','passed')),
  mismatches_json TEXT NOT NULL DEFAULT '[]',
  checked_at TEXT NOT NULL,
  checked_by TEXT NOT NULL
);
"""

EVENT_STATUS = {
    "PICKED": "已揽收",
    "IN_TRANSIT": "运输中",
    "ARRIVED_HUB": "到站",
    "OUT_FOR_DELIVERY": "派送中",
    "DELIVERED": "已签收",
    "EXCEPTION": "异常",
}


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    n = conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"]
    if n == 0:
        _seed_fixtures(conn)
    conn.commit()
    conn.close()


def _seed_fixtures(conn):
    fixture = FIXTURES_DIR / "seed_hops.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for wb in data.get("waybills", []):
        conn.execute(
            "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
            (wb["id"], wb["title"], wb.get("status_proj", "未知"), wb["created_at"]),
        )
    for hop in data.get("hops", []):
        conn.execute(
            "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) VALUES (?,?,?,?,?,?,?)",
            (
                hop["id"],
                hop["waybill_id"],
                hop["event_ts"],
                hop["location_code"],
                hop["event_code"],
                hop.get("seq"),
                hop.get("channel", "primary"),
            ),
        )
    _refresh_projections(conn)


def _refresh_projections(conn):
    rows = conn.execute(
        """
        SELECT h.waybill_id, h.id, h.event_code
        FROM hops h
        JOIN (
          SELECT waybill_id, MAX(event_ts || '|' || id) AS mk
          FROM hops WHERE retracted=0 AND channel='primary'
          GROUP BY waybill_id
        ) t ON t.waybill_id=h.waybill_id AND (h.event_ts || '|' || h.id)=t.mk
        WHERE h.retracted=0 AND h.channel='primary'
        """
    ).fetchall()
    for r in rows:
        proj = EVENT_STATUS.get(r["event_code"], "未知")
        conn.execute("UPDATE waybills SET status_proj=? WHERE id=?", (proj, r["waybill_id"]))
        conn.execute(
            "INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at) VALUES (?,?,?,?,datetime('now'))",
            (r["waybill_id"], r["id"], r["event_code"], proj),
        )


def map_status(event_code: str) -> str:
    return EVENT_STATUS.get(event_code, "未知")
