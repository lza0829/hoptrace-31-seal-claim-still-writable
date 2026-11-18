"""timeline_hash_seal 端到端测试：封存、写闸、失败闭、哈希互证、解封审计。

覆盖硬约束：
1. 封存实体落库（封存表挂运单+哈希+快照），不是页面文案。
2. 封印后装载正式线被整批拒绝并写明原因，无半截写入，审计留存。
3. 同 id 再读哈希与封存一致才返回成功；点序/投影被篡改则读路径拒绝返回。
4. 显式解封必须带原因并留审计，解封后装载恢复、重封印哈希随内容变化。
"""
import json
import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="hoptrace-seal-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["FIXTURES_DIR"] = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402
from app.modules.timeline_hash_seal import compute_content_hash  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _hops(conn, wid):
    return conn.execute(
        "SELECT * FROM hops WHERE waybill_id=? AND channel='primary' AND retracted=0 "
        "ORDER BY event_ts, id",
        (wid,),
    ).fetchall()


def _seal_row(conn, wid):
    return conn.execute("SELECT * FROM timeline_seals WHERE waybill_id=?", (wid,)).fetchone()


# ---------- 1. 封存：固化哈希与快照，实体落库 ----------

def test_seal_persists_hash_and_snapshot(client):
    # 种子 _refresh_projections 按末点重算投影，先读当前权威值，不硬编码
    current_proj = client.get("/api/waybills/WB-1001/timeline").json()["waybill"]["status_proj"]

    r = client.post("/api/seals/WB-1001", json={"actor": "tester"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    seal = body["seal"]
    assert seal["waybill_id"] == "WB-1001"
    assert seal["content_hash"].startswith("sha256:")
    assert seal["point_count"] == 3
    assert seal["projection"] == current_proj
    assert seal["status"] == "sealed"
    assert seal["sealed_at"]

    conn = connect()
    try:
        row = _seal_row(conn, "WB-1001")
        assert row is not None and row["status"] == "sealed"
        snap = json.loads(row["snapshot_json"])
        assert snap["point_count"] == 3
        assert snap["content_hash"] == row["content_hash"]
        # 快照内点序指纹必须与封存哈希自洽
        assert compute_content_hash(snap["hops"]) == row["content_hash"]
        audit = conn.execute(
            "SELECT * FROM seal_audit_log WHERE waybill_id='WB-1001' AND action='seal'"
        ).fetchall()
        assert len(audit) == 1 and audit[0]["actor"] == "tester"
    finally:
        conn.close()


def test_reseal_is_idempotent_same_hash(client):
    r1 = client.post("/api/seals/WB-1001", json={})
    r2 = client.post("/api/seals/WB-1001", json={})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["seal"]["content_hash"] == r2.json()["seal"]["content_hash"]


def test_seal_unknown_waybill_404(client):
    r = client.post("/api/seals/NOPE", json={})
    assert r.status_code == 404


# ---------- 2. 写闸：封印后装载整批拒绝，无半截写入，审计留存 ----------

def test_ingest_rejected_for_sealed_waybill_full_batch_atomic(client):
    conn = connect()
    try:
        before_1001 = len(_hops(conn, "WB-1001"))
        before_1002 = len(_hops(conn, "WB-1002"))
        hash_before = _seal_row(conn, "WB-1001")["content_hash"]
    finally:
        conn.close()

    r = client.post("/api/ingest/run")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "封存" in body["error"]
    assert body["report"]["written"] == 0
    assert body["report"]["rejected"] == 2
    blocked = {b["waybill_id"]: b for b in body["blocked"]}
    assert "WB-1001" in blocked
    assert blocked["WB-1001"]["content_hash"] == hash_before

    conn = connect()
    try:
        # 整批原子：未封存的 WB-1002 也不能被写入（无半截装载）
        assert len(_hops(conn, "WB-1001")) == before_1001
        assert len(_hops(conn, "WB-1002")) == before_1002
        # 哈希不变
        assert _seal_row(conn, "WB-1001")["content_hash"] == hash_before
        # 拦截审计留存，写明原因
        logs = conn.execute(
            "SELECT * FROM seal_audit_log WHERE waybill_id='WB-1001' AND action='ingest_blocked'"
        ).fetchall()
        assert len(logs) == 1
        assert "写闸" in logs[0]["reason"]
    finally:
        conn.close()


# ---------- 3. 证明接口与读路径：哈希互证，篡改拒绝 ----------

def test_proof_readonly_fields_and_verifies(client):
    r = client.get("/api/seals/WB-1001/proof")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["waybill_id"] == "WB-1001"
    assert p["point_count"] == 3
    assert p["projection"]  # 与封存瞬间投影一致，具体值由种子投影重算决定
    assert p["sealed_at"]
    assert p["verified"] is True
    assert p["current_hash"] == p["content_hash"]


def test_timeline_read_carries_verification(client):
    r = client.get("/api/waybills/WB-1001/timeline")
    assert r.status_code == 200
    body = r.json()
    assert body["seal"]["status"] == "sealed"
    assert body["seal"]["verified"] is True
    assert len(body["hops"]) == 3


def test_tampered_point_order_content_is_refused(client):
    conn = connect()
    try:
        original = conn.execute(
            "SELECT event_code FROM hops WHERE id='H-2'"
        ).fetchone()["event_code"]
        conn.execute("UPDATE hops SET event_code='DELIVERED' WHERE id='H-2'")
        conn.commit()
    finally:
        conn.close()

    # 读路径：篡改则拒绝返回正式线
    r = client.get("/api/waybills/WB-1001/timeline")
    assert r.status_code == 409
    assert "封存校验失败" in r.json()["detail"]

    # 证明接口同样拒绝在对不上哈希时返回成功
    rp = client.get("/api/seals/WB-1001/proof")
    assert rp.status_code == 409

    # 已封印且被篡改，重复封印也不能给出成功回执
    rs = client.post("/api/seals/WB-1001", json={})
    assert rs.status_code == 409

    # 复原现场，后续用例继续
    conn = connect()
    try:
        conn.execute("UPDATE hops SET event_code=? WHERE id='H-2'", (original,))
        conn.commit()
    finally:
        conn.close()
    assert client.get("/api/seals/WB-1001/proof").status_code == 200


def test_tampered_projection_is_refused(client):
    conn = connect()
    try:
        sealed_proj = _seal_row(conn, "WB-1001")["projection"]
        conn.execute("UPDATE waybills SET status_proj='已签收' WHERE id='WB-1001'")
        conn.commit()
    finally:
        conn.close()
    r = client.get("/api/seals/WB-1001/proof")
    assert r.status_code == 409
    assert "投影不一致" in r.json()["detail"]

    conn = connect()
    try:
        conn.execute(
            "UPDATE waybills SET status_proj=? WHERE id='WB-1001'", (sealed_proj,)
        )
        conn.commit()
    finally:
        conn.close()
    assert client.get("/api/seals/WB-1001/proof").status_code == 200


def test_extra_primary_hop_after_seal_is_refused_on_read(client):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) "
            "VALUES ('H-X','WB-1001','2026-11-17T19:00:00Z','X','IN_TRANSIT',9,'primary')"
        )
        conn.commit()
    finally:
        conn.close()
    assert client.get("/api/waybills/WB-1001/timeline").status_code == 409
    conn = connect()
    try:
        conn.execute("DELETE FROM hops WHERE id='H-X'")
        conn.commit()
    finally:
        conn.close()
    assert client.get("/api/waybills/WB-1001/timeline").status_code == 200


# ---------- 4. 解封：强制原因+审计，解封后可写，重封印哈希更新 ----------

def test_unseal_requires_reason(client):
    r = client.request("DELETE", "/api/seals/WB-1001", json={"reason": ""})
    assert r.status_code == 400


def test_unseal_then_ingest_succeeds_and_reseal_has_new_hash(client):
    old_hash = client.get("/api/seals/WB-1001/proof").json()["content_hash"]

    r = client.request(
        "DELETE", "/api/seals/WB-1001", json={"reason": "客户申诉更正，值班审批单 OPS-42"}
    )
    assert r.status_code == 200
    assert r.json()["seal"]["status"] == "unsealed"

    conn = connect()
    try:
        logs = conn.execute(
            "SELECT * FROM seal_audit_log WHERE waybill_id='WB-1001' AND action='unseal'"
        ).fetchall()
        assert len(logs) == 1 and "OPS-42" in logs[0]["reason"]
    finally:
        conn.close()

    # 解封后装载恢复：H-5 正式写入 WB-1001（同批 H-6 写入 WB-1002）
    ri = client.post("/api/ingest/run")
    assert ri.status_code == 200 and ri.json()["ok"] is True
    assert ri.json()["report"]["written"] == 2

    conn = connect()
    try:
        assert len(_hops(conn, "WB-1001")) == 4
    finally:
        conn.close()

    # 重新封印：点数与哈希随新内容变化，且与当前正式线自洽
    rs = client.post("/api/seals/WB-1001", json={"actor": "tester"})
    assert rs.status_code == 200
    new_seal = rs.json()["seal"]
    assert new_seal["point_count"] == 4
    assert new_seal["content_hash"] != old_hash
    rp = client.get("/api/seals/WB-1001/proof")
    assert rp.status_code == 200 and rp.json()["verified"] is True

    # 再次装载仍被写闸拒绝
    assert client.post("/api/ingest/run").json()["ok"] is False


# ---------- 5. 失败闭：事务回滚不留半截封印 ----------

def test_failed_seal_transaction_leaves_no_half_seal():
    from app.modules import timeline_hash_seal as mod

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        mod.seal_waybill(conn, "WB-1002")
        # 提交前回滚：库里不得出现可查的半截封印
        conn.rollback()
        row = _seal_row(conn, "WB-1002")
        assert row is None
        assert mod.is_sealed(conn, "WB-1002") is False
    finally:
        conn.close()


def test_audit_and_list_endpoints(client):
    r = client.get("/api/seals")
    assert r.status_code == 200
    rows = r.json()
    assert any(s["waybill_id"] == "WB-1001" and s["status"] == "sealed" for s in rows)

    r = client.get("/api/seals/audit/log?waybill_id=WB-1001")
    assert r.status_code == 200
    actions = {a["action"] for a in r.json()}
    assert {"seal", "unseal", "ingest_blocked"} <= actions


# ---------- 6. 内容核对：现场重算比对、失败闭、核对证明落库、只读核证 ----------

def _recon_rows(conn, wid):
    return conn.execute(
        "SELECT * FROM seal_reconciliations WHERE waybill_id=? ORDER BY id", (wid,)
    ).fetchall()


def test_attest_before_any_reconciliation_is_not_green(client):
    """封印后尚未核对：核证接口不得直接给通过。"""
    r = client.get("/api/seals/WB-1001/reconcile/attest")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "sealed"
    assert body["verified"] is False
    assert body["reconciliation"] is None
    assert body["reason"] == "尚未核对"
    # 现场重算值仍然如实给出，供人工比对
    assert body["current_hash"] == body["content_hash"]


def test_reconcile_recomputes_and_persists_proof(client):
    r = client.post("/api/seals/WB-1001/reconcile", json={"actor": "auditor"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    rec = body["reconciliation"]
    assert rec["waybill_id"] == "WB-1001"
    assert rec["outcome"] == "passed"
    assert rec["content_hash"] == rec["recomputed_hash"]
    assert rec["point_count"] == 4
    assert rec["mismatches"] == []
    assert rec["checked_by"] == "auditor"

    conn = connect()
    try:
        rows = _recon_rows(conn, "WB-1001")
        # 落库只有通过证明，没有草稿残留
        assert len(rows) == 1
        assert rows[0]["outcome"] == "passed"
        assert rows[0]["recomputed_hash"] == rows[0]["content_hash"]
    finally:
        conn.close()

    # 只读核证：最近核对证明与现场指纹对应，才给绿灯
    ra = client.get("/api/seals/WB-1001/reconcile/attest")
    assert ra.status_code == 200, ra.text
    att = ra.json()
    assert att["verified"] is True
    assert att["reason"] is None
    assert att["reconciliation"]["id"] == rec["id"]
    assert att["reconciliation"]["recomputed_hash"] == att["current_hash"] == att["content_hash"]

    # 已通过核对仍不解除写闸：装载继续被拒
    assert client.post("/api/ingest/run").json()["ok"] is False


def test_fixture_point_order_changed_after_seal_fails_reconcile(client):
    """夹具在封印后改点序：核对必须失败（409），且不落任何通过证明。"""
    conn = connect()
    try:
        before = len(_recon_rows(conn, "WB-1001"))
        original_ts = conn.execute(
            "SELECT event_ts FROM hops WHERE id='H-2'"
        ).fetchone()["event_ts"]
        # 改点序：把 H-2 的时间挪到最后，正式有效线顺序发生变化、指纹随之改变
        conn.execute(
            "UPDATE hops SET event_ts='2026-11-17T23:59:00Z' WHERE id='H-2'"
        )
        conn.commit()
    finally:
        conn.close()

    # 内容核对：现场重算指纹与封存值不一致 → 409 失败闭
    r = client.post("/api/seals/WB-1001/reconcile", json={"actor": "auditor"})
    assert r.status_code == 409, r.text
    assert "封存校验失败" in r.json()["detail"]

    # 不得写入「通过」核对证明，也不留草稿
    conn = connect()
    try:
        rows = _recon_rows(conn, "WB-1001")
        assert len(rows) == before
        assert all(row["outcome"] == "passed" for row in rows)
    finally:
        conn.close()

    # 只读核证同样失败闭：现场指纹已分叉，绝不返回绿灯
    ra = client.get("/api/seals/WB-1001/reconcile/attest")
    assert ra.status_code == 409
    assert "封存校验失败" in ra.json()["detail"]

    # 复原夹具点序
    conn = connect()
    try:
        conn.execute("UPDATE hops SET event_ts=? WHERE id='H-2'", (original_ts,))
        conn.commit()
    finally:
        conn.close()

    # 复原后核证恢复绿灯（既有核对证明仍对应当前指纹）
    assert client.get("/api/seals/WB-1001/reconcile/attest").json()["verified"] is True


def test_reconcile_requires_sealed_waybill(client):
    # 从未封存的运单没有可核对封存值
    r = client.post("/api/seals/NOPE/reconcile", json={})
    assert r.status_code == 404


def test_reconcile_on_unsealed_waybill_is_rejected(client):
    """解封后无法核对：封存值已翻状态，核对接口拒绝并在核证中如实返回。"""
    r = client.request(
        "DELETE", "/api/seals/WB-1002", json={"reason": "核对前置用例"}
    )
    assert r.status_code == 400  # WB-1002 从未封存

    # 对 WB-1002 先封存再解封，复核「解封后不能核对」
    assert client.post("/api/seals/WB-1002", json={}).status_code == 200
    assert client.request(
        "DELETE", "/api/seals/WB-1002", json={"reason": "测试解封"}
    ).status_code == 200
    assert client.post("/api/seals/WB-1002/reconcile", json={}).status_code == 404

    ra = client.get("/api/seals/WB-1002/reconcile/attest")
    assert ra.status_code == 200
    assert ra.json()["status"] == "unsealed"
    assert ra.json()["verified"] is None


def test_reconcile_failure_leaves_no_draft_row():
    """模块级：核对失败时草稿被删，事务回滚后库里不存在任何半截核对。"""
    from app.modules import timeline_hash_seal as mod

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        mod.seal_waybill(conn, "WB-1002")
        conn.commit()
        seal_hash = _seal_row(conn, "WB-1002")["content_hash"]

        # 在写事务内制造点序变化，核对必然失败
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) "
            "VALUES ('H-DRAFT','WB-1002','2026-11-17T23:00:00Z','X','DELIVERED',9,'primary')"
        )
        with pytest.raises(mod.SealTamperError):
            mod.reconcile_seal(conn, "WB-1002")
        # 失败时草稿已删除
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM seal_reconciliations WHERE waybill_id='WB-1002'"
        ).fetchone()["c"] == 0
        conn.rollback()

        # 复核：库里没有核对行，点序变化也回滚，封存哈希原样
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM seal_reconciliations WHERE waybill_id='WB-1002'"
        ).fetchone()["c"] == 0
        assert _seal_row(conn, "WB-1002")["content_hash"] == seal_hash
        assert mod.is_sealed(conn, "WB-1002") is True
    finally:
        conn.close()


def test_reconcile_proofs_endpoint_is_readonly(client):
    r = client.get("/api/seals/WB-1001/reconcile/proofs")
    assert r.status_code == 200
    rows = r.json()
    assert rows and all(x["outcome"] == "passed" for x in rows)
    assert rows[0]["content_hash"] == rows[0]["recomputed_hash"]
