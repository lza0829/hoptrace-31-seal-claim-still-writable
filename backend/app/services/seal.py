"""封存应用服务：管理连接与事务边界，封装模块内的封存域逻辑。"""
from __future__ import annotations

from app.db import connect
from app.modules import timeline_hash_seal as seal_mod


def seal(waybill_id: str, actor: str = "manual") -> dict:
    """封存运单：单连接单事务，提交成功才给回执，失败整体回滚（无半截封印）。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        receipt = seal_mod.seal_waybill(conn, waybill_id, actor=actor)
        conn.commit()
        return receipt
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def unseal(waybill_id: str, reason: str, actor: str = "manual") -> dict:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        receipt = seal_mod.unseal_waybill(conn, waybill_id, reason, actor=actor)
        conn.commit()
        return receipt
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def proof(waybill_id: str) -> dict:
    """只读封存证明：运单、哈希、点数、投影、封印时刻，附当前内容校验结论。"""
    conn = connect()
    try:
        row = seal_mod.get_seal(conn, waybill_id)
        if not row:
            raise seal_mod.SealError(f"运单 {waybill_id} 无封存记录")
        result = seal_mod._proof_row(row)
        if row["status"] == "sealed":
            # 校验失败会抛 SealTamperError——证明接口同样拒绝在对不上哈希时报成功
            verification = seal_mod.verify_seal(conn, waybill_id)
            result["verified"] = True
            result["current_hash"] = verification["current_hash"]
        else:
            result["verified"] = None
            result["current_hash"] = None
        return result
    finally:
        conn.close()


def list_proofs() -> list[dict]:
    conn = connect()
    try:
        return [seal_mod._proof_row(r) for r in seal_mod.list_seals(conn)]
    finally:
        conn.close()


def audit(waybill_id: str | None = None) -> list[dict]:
    conn = connect()
    try:
        return seal_mod.list_audit(conn, waybill_id)
    finally:
        conn.close()


def reconcile(waybill_id: str, actor: str = "manual") -> dict:
    """内容核对：现场重算正式线指纹并与封存值比对，一致才落核对证明。

    单连接单事务：先写核对草稿，重算比对通过后原子转成核对证明再提交；
    比对失败则抛出，事务整体回滚，库里不留草稿、更不会有「通过」假证明。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = seal_mod.reconcile_seal(conn, waybill_id, actor=actor)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def attest(waybill_id: str) -> dict:
    """只读核对核证：最近核对证明 + 现场重算复核（失败闭，不一致即抛错）。"""
    conn = connect()
    try:
        return seal_mod.attest_reconciliation(conn, waybill_id)
    finally:
        conn.close()


def list_reconciliations(waybill_id: str | None = None) -> list[dict]:
    conn = connect()
    try:
        return seal_mod.list_reconciliations(conn, waybill_id)
    finally:
        conn.close()
