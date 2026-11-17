"""截止证明 / 旁路隔离 / 晋升截止校验：回归硬约束逐条覆盖。

与 test_ingest_cutoff.py 分库运行：每个模块自己的 reset_database()。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database
from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _ingest(client, key, account_id=1, amount=1000, booked_at="2026-11-17T10:00:00Z"):
    return client.post(
        "/api/forward-entries/ingest",
        json={"entries": [{
            "entry_key": key, "account_id": account_id, "amount_cents": amount,
            "biz_ref": "ORD-P", "booked_at": booked_at,
        }]},
    )


def _set_cutoff(client, at, scope="全局", account_id=None):
    return client.put("/api/cutoff/configs", json={
        "scope_type": scope, "account_id": account_id, "cutoff_at": at, "reason": "证明用例",
    })


def _balance(client):
    with connect() as conn:
        return conn.execute("SELECT COALESCE(SUM(balance_cents),0) AS s FROM accounts").fetchone()["s"]


def test_late_forward_lands_sidecar_only_and_proof_records_it(client):
    assert _set_cutoff(client, "2026-11-16T23:59:59Z").status_code == 200

    late = _ingest(client, "FWD-PROOF-LATE", amount=4321, booked_at="2026-11-17T08:00:00Z")
    assert late.status_code == 200, late.text
    body = late.json()
    batch_id = body["id"]
    assert body["sidecar_intercepted_count"] == 1
    assert body["main_written_count"] == 0
    assert body["旁路流水键"] == ["FWD-PROOF-LATE"]

    # 正式集合不可见该键
    main_keys = {e["entry_key"] for e in client.get("/api/entries?kind=正向").json()}
    assert "FWD-PROOF-LATE" not in main_keys

    # 只读证明接口按批次 id 查询，四类键齐备
    proof = client.get(f"/api/cutoff/proofs/{batch_id}")
    assert proof.status_code == 200, proof.text
    pj = proof.json()
    assert pj["batch_id"] == batch_id
    assert pj["cutoff_at"] == "2026-11-16T23:59:59Z"
    assert pj["main_keys"] == []
    assert pj["sidecar_keys"] == ["FWD-PROOF-LATE"]
    assert pj["promotion_keys"] == []

    assert client.get("/api/cutoff/proofs/999999").status_code == 404


def test_proof_keys_are_read_only_from_writer_path(client):
    """证明只由装载/晋升写入口维护：没有任何写接口可篡改证明。"""
    paths = {r.path for r in app.routes}
    proof_paths = {p for p in paths if "proofs" in p}
    assert proof_paths
    # 只有 GET 路由（OpenAPI 里证明路径不允许有写方法）
    schema = client.get("/openapi.json").json()
    for path, ops in schema["paths"].items():
        if "proofs" in path:
            assert set(ops) <= {"get"}, f"{path} 不应有写方法：{set(ops)}"


def test_settle_never_scans_sidecar_even_if_it_is_late(client):
    # 旁路仍待晋升；归并只应把正式待归并正向计入
    before_side = {s["entry_key"] for s in client.get("/api/sidecar").json()}
    assert "FWD-PROOF-LATE" in before_side

    run = client.post("/api/settle-runs")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["sidecar_intercepted_count"] == len(before_side)
    assert body["sidecar_promoted_count"] == 0

    with connect() as conn:
        # 旁路行未被回写主表状态（主表根本没有这条键）
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE entry_key='FWD-PROOF-LATE'"
        ).fetchone()["c"] == 0
        side_row = conn.execute(
            "SELECT * FROM sidecar_forward_entries WHERE entry_key='FWD-PROOF-LATE'"
        ).fetchone()
        assert side_row["status"] == "旁路待晋升"
        assert side_row["promotion_run_id"] is None
    # 旁路额不进余额：余额只等于正式已归并净额
    with connect() as conn:
        fwd = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger_entries "
            "WHERE kind='正向' AND status='已归并'"
        ).fetchone()["s"]
        rev = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger_entries "
            "WHERE kind='冲正' AND status='已归并'"
        ).fetchone()["s"]
    assert _balance(client) == fwd - rev


def test_settle_refuses_when_sidecar_key_leaks_into_main(client):
    """旁路键被外部塞进正式待归并集合 → 整趟归并失败闭，不落 run、不改余额。"""
    with connect() as conn:
        conn.execute(
            """INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents,
               booked_at, status) VALUES ('FWD-PROOF-LATE', 1, '正向', 4321,
               '2026-11-17T08:00:00Z', '待归并')"""
        )
        conn.commit()
    balance_before = _balance(client)

    r = client.post("/api/settle-runs")
    assert r.status_code == 409, r.text
    assert "失败闭" in r.json()["detail"]

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM ledger_entries WHERE entry_key='FWD-PROOF-LATE'"
        ).fetchone()
        assert row["status"] == "待归并"  # 未被本轮改动
        assert row["settle_run_id"] is None
    assert _balance(client) == balance_before
    # 清理渗漏行，让后续用例回到干净状态
    with connect() as conn:
        conn.execute("DELETE FROM ledger_entries WHERE entry_key='FWD-PROOF-LATE'")
        conn.commit()


def test_promotion_revalidates_cutoff_and_refuses_still_late(client):
    """显式晋升前必须校验截止：仍晚于当前 C 的行拒绝晋升，不半写入。"""
    r = client.post("/api/sidecar/promote", json={"entry_keys": ["FWD-PROOF-LATE"]})
    assert r.status_code == 409, r.text
    assert "仍晚于当前截止" in r.json()["detail"]

    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE entry_key='FWD-PROOF-LATE'"
        ).fetchone()["c"] == 0
        side_row = conn.execute(
            "SELECT * FROM sidecar_forward_entries WHERE entry_key='FWD-PROOF-LATE'"
        ).fetchone()
        assert side_row["status"] == "旁路待晋升"
        assert side_row["last_error"]

    proof = client.get("/api/cutoff/proofs").json()
    hit = next(p for p in proof if "FWD-PROOF-LATE" in p["sidecar_keys"])
    assert hit["promotion_keys"] == []


def test_promotion_succeeds_when_cutoff_moved_past_and_proof_updates(client):
    """截止上调到该行之后 → 显式晋升成功，晋升键回填原批次证明。"""
    # 上调全局 C 到 11-18，使 11-17 的行不再超截止
    up = _set_cutoff(client, "2026-11-18T00:00:00Z")
    assert up.json()["action"] == "上调"

    r = client.post("/api/sidecar/promote", json={"entry_keys": ["FWD-PROOF-LATE"]})
    assert r.status_code == 200, r.text
    assert r.json()["promoted_count"] == 1

    main = {e["entry_key"]: e for e in client.get("/api/entries?kind=正向").json()}
    assert main["FWD-PROOF-LATE"]["status"] == "待归并"
    assert main["FWD-PROOF-LATE"]["通道"] == "正式"

    with connect() as conn:
        batch_id = conn.execute(
            "SELECT ingest_run_id FROM sidecar_forward_entries "
            "WHERE entry_key='FWD-PROOF-LATE'"
        ).fetchone()["ingest_run_id"]
    proof = client.get(f"/api/cutoff/proofs/{batch_id}").json()
    assert proof["promotion_keys"] == ["FWD-PROOF-LATE"]
    assert proof["sidecar_keys"] == ["FWD-PROOF-LATE"]


def test_cutoff_refresh_does_not_lose_marker_or_leak_second_ingest(client):
    """刷新截止标记 → 二次装载仍必须过闸，不得穿闸。"""
    # 连续多次刷新同一全局截止：库内只能留一行，且取最新值
    first = _set_cutoff(client, "2026-11-18T06:00:00Z")
    assert first.json()["new_cutoff_at"] == "2026-11-18T06:00:00Z"
    second = _set_cutoff(client, "2026-11-18T03:00:00Z")
    assert second.json()["action"] == "下调"

    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cutoff_configs WHERE scope_type='全局' AND account_id IS NULL"
        ).fetchall()
    assert len(rows) == 1, "刷新后不得残留旧截止标记"
    assert rows[0]["cutoff_at"] == "2026-11-18T03:00:00Z"

    # 二次装载：晚于新 C 的正向必须进旁路
    late2 = _ingest(client, "FWD-REFRESH-LATE", booked_at="2026-11-18T05:00:00Z")
    assert late2.json()["sidecar_intercepted_count"] == 1
    assert late2.json()["main_written_count"] == 0
    main_keys = {e["entry_key"] for e in client.get("/api/entries?kind=正向").json()}
    assert "FWD-REFRESH-LATE" not in main_keys

    # 刷新记录仍在（审计不丢）
    actions = [e["action"] for e in client.get("/api/cutoff/config-events").json()]
    assert "下调" in actions


def test_promotion_failure_never_half_writes_formal_pending(client):
    """晋升中途失败：正式待归并集合不得出现任何半写入行。"""
    # 制造一个可晋升行 + 一个主表冲突键，验证整批回滚
    _set_cutoff(client, "2026-11-19T00:00:00Z")  # 无闸效果：两行都满足截止
    a = _ingest(client, "FWD-HALF-A", booked_at="2026-11-18T10:00:00Z")
    b = _ingest(client, "FWD-HALF-B", booked_at="2026-11-18T11:00:00Z")
    assert a.json()["main_written_count"] == 1
    assert b.json()["main_written_count"] == 1
    # 人为把它们移到旁路待晋升状态，再制造 B 的主表冲突
    with connect() as conn:
        for key in ("FWD-HALF-A", "FWD-HALF-B"):
            row = conn.execute(
                "SELECT * FROM ledger_entries WHERE entry_key=?", (key,)
            ).fetchone()
            conn.execute(
                """INSERT INTO sidecar_forward_entries(entry_key, account_id, amount_cents,
                   biz_ref, booked_at, note, cutoff_at_snapshot, status, intercepted_at)
                   VALUES (?, ?, ?, ?, ?, '', '2026-11-19T00:00:00Z', '旁路待晋升',
                   '2026-11-18T00:00:00Z')""",
                (key, row["account_id"], row["amount_cents"], row["biz_ref"], row["booked_at"]),
            )
        conn.commit()

    before = {
        e["entry_key"] for e in client.get("/api/entries?kind=正向").json()
    }
    r = client.post("/api/sidecar/promote", json={"entry_keys": ["FWD-HALF-A", "FWD-HALF-B"]})
    assert r.status_code == 409, r.text
    assert "失败闭" in r.json()["detail"]

    after = {e["entry_key"] for e in client.get("/api/entries?kind=正向").json()}
    assert after == before, "失败闭不得向正式待归并集合半写入任何键"

    side = {s["entry_key"]: s for s in client.get("/api/sidecar").json()}
    assert side["FWD-HALF-A"]["status"] == "旁路待晋升"
    assert side["FWD-HALF-B"]["status"] == "旁路待晋升"
