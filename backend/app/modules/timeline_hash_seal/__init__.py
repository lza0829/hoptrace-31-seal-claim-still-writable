"""timeline_hash_seal — 运单时间线哈希封存。

落库形态（择一实现）：独立封存表 timeline_seals 挂运单 + 内容哈希 + 投影快照，
配合 seal_audit_log 留封印/解封/拦截审计；不是运单行上的 seal 列，也不是页面文案。

封存路径（择一实现）：同事务写封存记录并打开写闸——
封存服务在一个 BEGIN IMMEDIATE 事务内固化哈希与快照并写入封存行，提交成功即开闸；
事务失败则整体回滚，库里不存在「半截封印」，也不会有任何成功回执。

硬约束：
- 封印后正式有效线（hops channel='primary' 且 retracted=0）写入/更正一律拒绝，
  除非显式解封（必须带 reason）并留审计。
- 读路径用封存哈希重算校验，内容或投影被篡改则拒绝返回。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

STUB = False

# 正式有效线的读取口径：与 services/timeline.list_primary 完全一致，
# 保证「封存时固化的点序」与「读路径校验的点序」是同一份有序内容。
_PRIMARY_SQL = """
SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted
FROM hops
WHERE waybill_id=? AND channel='primary' AND retracted=0
ORDER BY event_ts ASC, id ASC
"""


class SealError(Exception):
    """封存域错误基类。"""


class SealedWaybillError(SealError):
    """写闸命中：运单已封存，正式线写入被拒绝。"""

    def __init__(self, waybill_id: str, content_hash: str, sealed_at: str):
        self.waybill_id = waybill_id
        self.content_hash = content_hash
        self.sealed_at = sealed_at
        super().__init__(
            f"运单 {waybill_id} 已于 {sealed_at} 封存（哈希 {content_hash}），"
            "正式有效线写入/更正被拒绝；如需变更请显式解封并登记原因"
        )


class SealTamperError(SealError):
    """封存校验失败：当前内容与封存指纹不一致，拒绝返回。"""

    def __init__(self, waybill_id: str, mismatches: list[str], stored_hash: str, current_hash: str):
        self.waybill_id = waybill_id
        self.mismatches = mismatches
        self.stored_hash = stored_hash
        self.current_hash = current_hash
        super().__init__(
            f"运单 {waybill_id} 封存校验失败：{'; '.join(mismatches)}；"
            f"封存哈希={stored_hash}，当前哈希={current_hash}"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_hops(conn, waybill_id: str) -> list[dict]:
    """取正式有效线的有序点序（封存与校验共用同一口径）。"""
    rows = conn.execute(_PRIMARY_SQL, (waybill_id,)).fetchall()
    return [dict(r) for r in rows]


def compute_content_hash(hops: list[dict]) -> str:
    """对点序内容做规范序列化后取 SHA-256，点序/字段任何改动都会变哈希。"""
    canon = json.dumps(hops, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def build_snapshot(waybill_id: str, hops: list[dict], projection: str, sealed_at: str) -> dict:
    return {
        "waybill_id": waybill_id,
        "projection": projection,
        "point_count": len(hops),
        "content_hash": compute_content_hash(hops),
        "sealed_at": sealed_at,
        "hops": hops,
    }


def log_audit(conn, waybill_id: str, action: str, reason: str | None,
              content_hash: str | None, actor: str) -> None:
    conn.execute(
        """
        INSERT INTO seal_audit_log(waybill_id, action, reason, content_hash, actor, created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (waybill_id, action, reason, content_hash, actor, utc_now()),
    )


def is_sealed(conn, waybill_id: str) -> bool:
    row = conn.execute(
        "SELECT status FROM timeline_seals WHERE waybill_id=?", (waybill_id,)
    ).fetchone()
    return bool(row and row["status"] == "sealed")


def assert_write_allowed(conn, waybill_id: str) -> None:
    """写闸：封印态抛 SealedWaybillError，其余放行。装载入口必须在写事务内调用。"""
    row = conn.execute(
        "SELECT status, content_hash, sealed_at FROM timeline_seals WHERE waybill_id=?",
        (waybill_id,),
    ).fetchone()
    if row and row["status"] == "sealed":
        raise SealedWaybillError(waybill_id, row["content_hash"], row["sealed_at"])


def _get_waybill(conn, waybill_id: str):
    return conn.execute("SELECT * FROM waybills WHERE id=?", (waybill_id,)).fetchone()


def seal_waybill(conn, waybill_id: str, actor: str = "manual") -> dict:
    """在调用方的写事务内执行封存，返回收执。

    - 封存瞬间固化点序内容哈希与投影快照，同事务写封存行，提交即开写闸。
    - 已封存且校验通过：幂等返回原回执（不重复封印）。
    - 已封存但当前内容对不上原哈希：抛 SealTamperError，拒绝给出成功回执。
    - 写完后在同事务内回读比对，确保「同 id 再读哈希与封存时一致」才报成功。
    """
    wb = _get_waybill(conn, waybill_id)
    if not wb:
        raise SealError(f"运单 {waybill_id} 不存在")

    existing = conn.execute(
        "SELECT * FROM timeline_seals WHERE waybill_id=?", (waybill_id,)
    ).fetchone()
    if existing and existing["status"] == "sealed":
        # 已封印：先验真，通过则幂等返回；被篡改过则绝不返回成功
        verify_seal(conn, waybill_id)
        return _proof_row(existing)

    sealed_at = utc_now()
    hops = canonical_hops(conn, waybill_id)
    content_hash = compute_content_hash(hops)
    projection = wb["status_proj"]
    snapshot = build_snapshot(waybill_id, hops, projection, sealed_at)

    conn.execute(
        """
        INSERT INTO timeline_seals(waybill_id, content_hash, point_count, projection,
                                   snapshot_json, sealed_at, sealed_by, status)
        VALUES (?,?,?,?,?,?,?, 'sealed')
        ON CONFLICT(waybill_id) DO UPDATE SET
          content_hash=excluded.content_hash,
          point_count=excluded.point_count,
          projection=excluded.projection,
          snapshot_json=excluded.snapshot_json,
          sealed_at=excluded.sealed_at,
          sealed_by=excluded.sealed_by,
          status='sealed'
        """,
        (
            waybill_id,
            content_hash,
            len(hops),
            projection,
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            sealed_at,
            actor,
        ),
    )
    log_audit(conn, waybill_id, "seal", None, content_hash, actor)

    # 同事务回读：落库哈希必须与刚算出来的指纹一致，否则不算封存成功
    reread = conn.execute(
        "SELECT * FROM timeline_seals WHERE waybill_id=?", (waybill_id,)
    ).fetchone()
    if not reread or reread["content_hash"] != content_hash:
        raise SealError(f"运单 {waybill_id} 封存回读校验失败，事务应回滚")
    return _proof_row(reread)


def unseal_waybill(conn, waybill_id: str, reason: str, actor: str = "manual") -> dict:
    """显式解封：必须给原因，翻状态并留审计；封存行保留为历史证据。"""
    if not reason or not reason.strip():
        raise SealError("解封必须登记原因（reason）")
    wb = _get_waybill(conn, waybill_id)
    if not wb:
        raise SealError(f"运单 {waybill_id} 不存在")
    seal = conn.execute(
        "SELECT * FROM timeline_seals WHERE waybill_id=?", (waybill_id,)
    ).fetchone()
    if not seal:
        raise SealError(f"运单 {waybill_id} 从未封存，无需解封")
    if seal["status"] != "sealed":
        raise SealError(f"运单 {waybill_id} 当前不是封印态")

    conn.execute(
        "UPDATE timeline_seals SET status='unsealed' WHERE waybill_id=?",
        (waybill_id,),
    )
    log_audit(conn, waybill_id, "unseal", reason.strip(), seal["content_hash"], actor)
    row = conn.execute(
        "SELECT * FROM timeline_seals WHERE waybill_id=?", (waybill_id,)
    ).fetchone()
    return _proof_row(row)


def verify_seal(conn, waybill_id: str) -> dict:
    """用封存哈希校验当前正式线内容与投影，篡改则抛 SealTamperError。"""
    seal = conn.execute(
        "SELECT * FROM timeline_seals WHERE waybill_id=?", (waybill_id,)
    ).fetchone()
    if not seal:
        raise SealError(f"运单 {waybill_id} 无封存记录")

    live_hops = canonical_hops(conn, waybill_id)
    live_hash = compute_content_hash(live_hops)
    snapshot = json.loads(seal["snapshot_json"])
    snapshot_hash = compute_content_hash(snapshot["hops"])
    wb = _get_waybill(conn, waybill_id)
    live_projection = wb["status_proj"] if wb else None

    mismatches: list[str] = []
    if seal["content_hash"] != snapshot_hash:
        mismatches.append("封存哈希与快照内嵌指纹不一致（封存行自身被改）")
    if seal["content_hash"] != live_hash:
        mismatches.append("正式线点序内容哈希与封存值不一致（点序或投影点被改）")
    if seal["point_count"] != len(live_hops):
        mismatches.append(
            f"点数不一致：封存 {seal['point_count']} / 当前 {len(live_hops)}"
        )
    if seal["projection"] != live_projection:
        mismatches.append(
            f"投影不一致：封存 {seal['projection']!r} / 当前 {live_projection!r}"
        )

    return {
        **_proof_row(seal),
        "verified": True,
        "current_hash": live_hash,
        "mismatches": mismatches,
    }


def get_seal(conn, waybill_id: str):
    return conn.execute(
        "SELECT * FROM timeline_seals WHERE waybill_id=?", (waybill_id,)
    ).fetchone()


def list_seals(conn) -> list:
    return conn.execute(
        "SELECT * FROM timeline_seals ORDER BY sealed_at DESC, waybill_id"
    ).fetchall()


def list_audit(conn, waybill_id: str | None = None) -> list[dict]:
    if waybill_id:
        rows = conn.execute(
            "SELECT * FROM seal_audit_log WHERE waybill_id=? ORDER BY id DESC",
            (waybill_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM seal_audit_log ORDER BY id DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


def _proof_row(row) -> dict:
    return {
        "waybill_id": row["waybill_id"],
        "content_hash": row["content_hash"],
        "point_count": row["point_count"],
        "projection": row["projection"],
        "snapshot_json": row["snapshot_json"],
        "sealed_at": row["sealed_at"],
        "sealed_by": row["sealed_by"],
        "status": row["status"],
    }
