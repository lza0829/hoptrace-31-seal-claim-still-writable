import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules.timeline_hash_seal import SealedWaybillError, log_audit, utc_now
from app.seed import map_status


def run_batch() -> dict:
    """基座装载：按夹具追加；乱序重排 / 旁路 / 重复隔离留给功能题空桩。

    timeline_hash_seal 写闸协同：在 BEGIN IMMEDIATE 写事务内先做权威预检，
    批次中任一运单处于封印态，整批拒绝并回滚（不留半截写入），
    拦截原因写入 seal_audit_log（独立事务，不随回滚丢失）。
    """
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"ok": False, "error": "缺少 ingest_batch.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    cutoff = payload.get("cutoff_ts")
    hops = payload.get("hops", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # 写闸：同事务权威判定，封印态运单一个都不许写正式线
        blocked = _find_sealed(conn, hops)
        if blocked:
            conn.rollback()
            _record_blocks(blocked, batch_id)
            return {
                "ok": False,
                "error": "批次含已封存运单，装载被写闸拒绝（整批未写入）",
                "batch_id": batch_id,
                "blocked": blocked,
                "report": {
                    "batch_id": batch_id,
                    "written": 0,
                    "input": len(hops),
                    "rejected": len(hops),
                },
            }

        written = 0
        for hop in hops:
            wid = hop["waybill_id"]
            # 行级再保险：预检后若状态被同连接后续逻辑改变仍会被拦
            _gate(conn, wid)
            exists = conn.execute("SELECT 1 FROM waybills WHERE id=?", (wid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
                    (wid, hop.get("waybill_title") or wid, "未知", _now()),
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
                VALUES (?,?,?,?,?,?, 'primary')
                """,
                (
                    hop["id"],
                    wid,
                    hop["event_ts"],
                    hop["location_code"],
                    hop["event_code"],
                    hop.get("seq"),
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                written += 1
                proj = map_status(hop["event_code"])
                conn.execute("UPDATE waybills SET status_proj=? WHERE id=?", (proj, wid))
                conn.execute(
                    "INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at) VALUES (?,?,?,?,?)",
                    (wid, hop["id"], hop["event_code"], proj, _now()),
                )
        report = {
            "batch_id": batch_id,
            "cutoff_ts": cutoff,
            "written": written,
            "input": len(hops),
        }
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, cutoff_ts, created_at, report_json) VALUES (?,?,?,?)",
            (batch_id, cutoff, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except SealedWaybillError as e:
        conn.rollback()
        return {"ok": False, "error": str(e), "batch_id": batch_id}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _find_sealed(conn, hops: list[dict]) -> list[dict]:
    """返回批次中命中写闸的运单清单（含封存哈希与封印时刻，用于回执写明原因）。"""
    seen: set[str] = set()
    blocked: list[dict] = []
    for hop in hops:
        wid = hop["waybill_id"]
        if wid in seen:
            continue
        seen.add(wid)
        row = conn.execute(
            "SELECT waybill_id, content_hash, sealed_at FROM timeline_seals "
            "WHERE waybill_id=? AND status='sealed'",
            (wid,),
        ).fetchone()
        if row:
            blocked.append(
                {
                    "waybill_id": row["waybill_id"],
                    "content_hash": row["content_hash"],
                    "sealed_at": row["sealed_at"],
                    "reason": "运单已封存，正式有效线禁止写入/更正",
                }
            )
    return blocked


def _gate(conn, waybill_id: str) -> None:
    from app.modules.timeline_hash_seal import assert_write_allowed

    assert_write_allowed(conn, waybill_id)


def _record_blocks(blocked: list[dict], batch_id: str) -> None:
    """拦截审计单独事务落库：写事务已回滚，审计不能随之丢失。"""
    audit = connect()
    try:
        audit.execute("BEGIN IMMEDIATE")
        for b in blocked:
            log_audit(
                audit,
                b["waybill_id"],
                "ingest_blocked",
                f"批次 {batch_id} 被写闸拒绝：{b['reason']}",
                b["content_hash"],
                "ingest",
            )
        audit.commit()
    except Exception:
        audit.rollback()
        raise
    finally:
        audit.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
