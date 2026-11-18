from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.modules.timeline_hash_seal import SealError, SealTamperError
from app.services import seal as seal_svc

router = APIRouter(prefix="/api/seals", tags=["timeline-hash-seal"])


class SealBody(BaseModel):
    actor: str = "manual"


class UnsealBody(BaseModel):
    reason: str
    actor: str = "manual"


def _fail(exc: Exception, status: int = 409):
    return HTTPException(status_code=status, detail=str(exc))


@router.post("/{waybill_id}")
def seal_waybill(waybill_id: str, body: SealBody):
    """对指定运单做时间线封存：固化点序哈希与投影快照，提交即开写闸。"""
    try:
        return {"ok": True, "seal": seal_svc.seal(waybill_id, actor=body.actor)}
    except SealTamperError as e:
        # 已封印但内容被改：绝不返回成功
        raise _fail(e)
    except SealError as e:
        raise _fail(e, status=404)


@router.delete("/{waybill_id}")
def unseal_waybill(waybill_id: str, body: UnsealBody):
    """显式解封，必须给原因，全程留审计。"""
    try:
        return {"ok": True, "seal": seal_svc.unseal(waybill_id, body.reason, actor=body.actor)}
    except SealError as e:
        raise _fail(e, status=400)


@router.get("/{waybill_id}/proof")
def seal_proof(waybill_id: str):
    """只读封存证明：运单、哈希、点数、投影、封印时刻，并附实时校验结论。"""
    try:
        return seal_svc.proof(waybill_id)
    except SealTamperError as e:
        raise _fail(e)
    except SealError as e:
        raise _fail(e, status=404)


@router.get("")
def list_seal_proofs():
    return seal_svc.list_proofs()


@router.get("/audit/log")
def seal_audit(waybill_id: str | None = None):
    return seal_svc.audit(waybill_id)
