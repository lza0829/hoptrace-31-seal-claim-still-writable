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

内容核对（核对核证）：
- 核对必须**现场重算**正式有效线指纹，并与封印行 content_hash、快照内嵌指纹
  逐项比对；任何不一致一律失败闭（抛 SealTamperError），绝不返回「通过」。
- 落库形态：只追加的核对结果证明表 seal_reconciliations（不是页面文案）。
- 提交形态：先写核对草稿（outcome='draft'）→ 现场重算比对 → 一致才把草稿
  原子转成核对证明（outcome='passed'）并随调用方事务提交；不一致则删除草稿
  并抛错，调用方回滚后库里不留任何半截核对。
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
    """用封存哈希校验当前正式线内容与投影，篡改则抛 SealTamperError。

    核对口径：**现场重算**正式有效线指纹（不信封存行里的旧值），再与
    封印行 content_hash、快照内嵌指纹、点数、投影逐项比对。
    只要有任一项对不上，就抛 SealTamperError 失败闭——绝不在对不上时返回通过。
    """
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

    # 失败闭：任何不一致都必须抛错，不能只把 mismatches 挂在成功回执上。
    if mismatches:
        raise SealTamperError(waybill_id, mismatches, seal["content_hash"], live_hash)

    return {
        **_proof_row(seal),
        "verified": True,
        "current_hash": live_hash,
        "mismatches": mismatches,
    }


def reconcile_seal(conn, waybill_id: str, actor: str = "manual") -> dict:
    """内容核对：现场重算正式线指纹 → 与封存值比对 → 一致才原子落核对证明。

    提交形态（先草稿后证明）：
    1. 先写一条 outcome='draft' 的核对草稿行（记录封印指纹与核对时刻）。
    2. 现场重算正式线指纹，连同点数、投影、快照内嵌指纹逐项比对。
    3. 一致：把草稿原子转成 outcome='passed' 并回读证明；随后随调用方事务提交。
       不一致：删除草稿并抛 SealTamperError，调用方回滚，库里不留半截核对。

    核对对象必须是封印态运单：未封印/已解封没有可核对的封存值，抛 SealError。
    """
    seal = conn.execute(
        "SELECT * FROM timeline_seals WHERE waybill_id=?", (waybill_id,)
    ).fetchone()
    if not seal:
        raise SealError(f"运单 {waybill_id} 无封存记录，无法核对")
    if seal["status"] != "sealed":
        raise SealError(f"运单 {waybill_id} 当前不是封印态，无可核对的封存值")

    checked_at = utc_now()
    cur = conn.execute(
        """
        INSERT INTO seal_reconciliations(
          waybill_id, content_hash, recomputed_hash, point_count, projection,
          outcome, mismatches_json, checked_at, checked_by
        ) VALUES (?,?,?,?,?, 'draft', '[]', ?, ?)
        """,
        (waybill_id, seal["content_hash"], None, seal["point_count"],
         seal["projection"], checked_at, actor),
    )
    draft_id = cur.lastrowid

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
        mismatches.append("正式线点序内容哈希与封存值不一致（现场点序已被改）")
    if seal["point_count"] != len(live_hops):
        mismatches.append(
            f"点数不一致：封存 {seal['point_count']} / 当前 {len(live_hops)}"
        )
    if seal["projection"] != live_projection:
        mismatches.append(
            f"投影不一致：封存 {seal['projection']!r} / 当前 {live_projection!r}"
        )

    if mismatches:
        # 草稿不保留：删除后抛错，调用方回滚，库里无「核对通过」假证明。
        conn.execute("DELETE FROM seal_reconciliations WHERE id=?", (draft_id,))
        raise SealTamperError(waybill_id, mismatches, seal["content_hash"], live_hash)

    # 一致：原子把草稿转成核对证明，并写全现场重算值。
    conn.execute(
        """
        UPDATE seal_reconciliations
        SET recomputed_hash=?, point_count=?, projection=?, outcome='passed'
        WHERE id=? AND outcome='draft'
        """,
        (live_hash, len(live_hops), live_projection, draft_id),
    )
    proof = conn.execute(
        "SELECT * FROM seal_reconciliations WHERE id=?", (draft_id,)
    ).fetchone()
    if not proof or proof["outcome"] != "passed":
        raise SealError(f"运单 {waybill_id} 核对证明回读校验失败，事务应回滚")
    return _reconcile_row(proof)


def latest_reconciliation(conn, waybill_id: str):
    """最近一次核对证明（passed）；草稿不会落库留存，故必为已通过证明。"""
    return conn.execute(
        "SELECT * FROM seal_reconciliations WHERE waybill_id=? AND outcome='passed' "
        "ORDER BY id DESC LIMIT 1",
        (waybill_id,),
    ).fetchone()


def list_reconciliations(conn, waybill_id: str | None = None) -> list[dict]:
    if waybill_id:
        rows = conn.execute(
            "SELECT * FROM seal_reconciliations WHERE waybill_id=? AND outcome='passed' "
            "ORDER BY id DESC",
            (waybill_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM seal_reconciliations WHERE outcome='passed' ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return [_reconcile_row(r) for r in rows]



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


def _reconcile_row(row) -> dict:
    return {
        "id": row["id"],
        "waybill_id": row["waybill_id"],
        "content_hash": row["content_hash"],
        "recomputed_hash": row["recomputed_hash"],
        "point_count": row["point_count"],
        "projection": row["projection"],
        "outcome": row["outcome"],
        "mismatches": json.loads(row["mismatches_json"] or "[]"),
        "checked_at": row["checked_at"],
        "checked_by": row["checked_by"],
    }


def attest_reconciliation(conn, waybill_id: str) -> dict:
    """只读核对核证：最近核对证明 + 现场再算复核。

    「通过」必须同时满足，否则绝不返回 verified=True：
    1. 运单当前处于封印态；
    2. 现场重算指纹仍等于封存值（verify_seal 不抛错）；
    3. 存在最近一次 passed 核对证明，且其 content_hash 对应当前封印行、
       recomputed_hash 对应当前现场指纹——内嵌指纹与现场不得分叉。
    现场指纹已变（封印后被改点序等）时 verify_seal 直接抛错，由路由层 409 失败闭。
    """
    seal = conn.execute(
        "SELECT * FROM timeline_seals WHERE waybill_id=?", (waybill_id,)
    ).fetchone()
    if not seal:
        raise SealError(f"运单 {waybill_id} 无封存记录")

    latest = latest_reconciliation(conn, waybill_id)
    latest_row = _reconcile_row(latest) if latest else None
    if seal["status"] != "sealed":
        return {
            "waybill_id": waybill_id,
            "status": seal["status"],
            "verified": None,
            "reason": "运单已解封，无可核对封存值",
            "content_hash": seal["content_hash"],
            "current_hash": None,
            "reconciliation": latest_row,
        }

    # 现场重算复核：指纹一旦分叉立即抛错，绝不给绿灯。
    verification = verify_seal(conn, waybill_id)
    live_hash = verification["current_hash"]

    result = {
        "waybill_id": waybill_id,
        "status": "sealed",
        "content_hash": seal["content_hash"],
        "current_hash": live_hash,
        "reconciliation": latest_row,
        "verified": False,
        "reason": None,
    }
    if latest_row is None:
        result["reason"] = "尚未核对"
        return result
    if (
        latest_row["content_hash"] != seal["content_hash"]
        or latest_row["recomputed_hash"] != live_hash
    ):
        result["reason"] = "最近核对证明与当前封印/现场指纹不对应（重封或内容变更后需重新核对）"
        return result

    result["verified"] = True
    return result


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
