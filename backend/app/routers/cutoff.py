from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.modules import ingest_cutoff as gate

router = APIRouter(tags=["装载截止闸"])


class CutoffIn(BaseModel):
    scope_type: str = "全局"  # 全局 | 账户
    account_id: int | None = None
    cutoff_at: str | None = None  # ISO 8601；null = 清除该范围截止
    reason: str = ""


class ForwardIn(BaseModel):
    entry_key: str
    account_id: int
    amount_cents: int
    biz_ref: str = ""
    booked_at: str
    note: str = ""


class IngestIn(BaseModel):
    entries: list[ForwardIn]


class PromoteIn(BaseModel):
    entry_keys: list[str]


@router.get("/cutoff/configs")
def get_configs():
    return gate.list_configs()


@router.get("/cutoff/config-events")
def get_config_events(limit: int = Query(100, ge=1, le=1000)):
    return gate.list_config_events(limit)


@router.put("/cutoff/configs")
def put_config(body: CutoffIn):
    return gate.set_cutoff(
        body.scope_type.strip(), body.account_id, body.cutoff_at, body.reason
    )


@router.post("/forward-entries/ingest")
def post_ingest(body: IngestIn):
    return gate.ingest_forward_entries([e.model_dump() for e in body.entries])


@router.get("/sidecar")
def get_sidecar(status: str | None = Query(None, description="旁路待晋升|已晋升|晋升失败")):
    return gate.list_sidecar(status)


@router.post("/sidecar/promote")
def post_promote(body: PromoteIn):
    return gate.promote_sidecar(body.entry_keys)


@router.get("/promotion-runs")
def get_promotions():
    return gate.list_promotion_runs()


@router.get("/cutoff/proofs")
def get_proofs(limit: int = Query(100, ge=1, le=1000)):
    return gate.list_cutoff_proofs(limit)


@router.get("/cutoff/proofs/{batch_id}")
def get_proof(batch_id: int):
    """按装载批次 id 查询截止证明（只读）。"""
    return gate.get_cutoff_proof_or_404(batch_id)


@router.get("/ingest-runs")
def get_ingest_runs(limit: int = Query(50, ge=1, le=500)):
    """装载批次：正式/旁路分列 + 每键通道 + 同批次截止证明。"""
    return gate.list_ingest_runs(limit)


@router.get("/ingest-report")
def get_report():
    return gate.ingest_report()
