from app.modules.timeline_hash_seal import (
    SealTamperError,
    get_seal,
    verify_seal,
)


def list_primary(conn, waybill_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted
        FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY event_ts ASC, id ASC
        """,
        (waybill_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def sealed_view(conn, waybill_id: str) -> dict | None:
    """封存协同读：封印态运单用封存哈希校验当前内容。

    - 无封存：返回 None，调用方按普通读处理。
    - 封印且校验通过：返回含证明与验证结论的视图，证明与正式线互证。
    - 内容/投影被篡改：抛 SealTamperError，由路由层拒绝返回正式线。
    """
    seal = get_seal(conn, waybill_id)
    if not seal:
        return None
    if seal["status"] != "sealed":
        return {
            "status": seal["status"],
            "seal": dict(seal),
            "verified": None,
        }
    # verify_seal 失败即抛异常，绝不把对不上哈希的正式线当成功结果返回
    verification = verify_seal(conn, waybill_id)
    return {
        "status": "sealed",
        "seal": dict(seal),
        "verified": verification["verified"],
        "current_hash": verification["current_hash"],
    }


__all__ = ["list_primary", "sealed_view", "SealTamperError"]
