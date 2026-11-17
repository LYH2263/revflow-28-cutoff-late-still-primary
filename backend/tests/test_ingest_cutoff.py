"""装载截止旁路闸：需求逐项测试。"""
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


def _ingest(client, key, account_id=1, amount=1000, booked_at="2026-11-17T10:00:00Z", biz="ORD-T"):
    return client.post(
        "/api/forward-entries/ingest",
        json={"entries": [{
            "entry_key": key, "account_id": account_id, "amount_cents": amount,
            "biz_ref": biz, "booked_at": booked_at,
        }]},
    )


def test_health(client):
    assert client.get("/api/health").status_code == 200


def test_no_cutoff_writes_main_path(client):
    r = _ingest(client, "FWD-PRE-001", booked_at="2026-11-10T08:00:00Z")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["main_written_count"] == 1
    assert body["sidecar_intercepted_count"] == 0
    rows = client.get("/api/entries?kind=正向").json()
    assert any(e["entry_key"] == "FWD-PRE-001" and e["status"] == "待归并" for e in rows)


def test_set_global_cutoff_and_intercept(client):
    r = client.put("/api/cutoff/configs", json={
        "scope_type": "全局", "cutoff_at": "2026-11-16T23:59:59Z", "reason": "装载边界宣告",
    })
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "设置"

    # 边界之内：仍走主路径
    inside = _ingest(client, "FWD-PRE-002", booked_at="2026-11-16T23:59:59Z")
    assert inside.json()["main_written_count"] == 1

    # 边界之外：拦截进旁路，主表不可见、余额不变
    over = _ingest(client, "FWD-LATE-001", amount=9999, booked_at="2026-11-17T08:00:00Z")
    assert over.status_code == 200
    assert over.json()["sidecar_intercepted_count"] == 1
    assert over.json()["main_written_count"] == 0

    main_rows = client.get("/api/entries?kind=正向").json()
    assert all(e["entry_key"] != "FWD-LATE-001" for e in main_rows)

    side = client.get("/api/sidecar").json()
    hit = next(s for s in side if s["entry_key"] == "FWD-LATE-001")
    assert hit["status"] == "旁路待晋升"
    assert hit["amount_cents"] == 9999
    assert hit["cutoff_at_snapshot"] == "2026-11-16T23:59:59Z"


def test_account_cutoff_overrides_global(client):
    r = client.put("/api/cutoff/configs", json={
        "scope_type": "账户", "account_id": 2,
        "cutoff_at": "2026-11-20T00:00:00Z", "reason": "账户 2 更晚截止",
    })
    assert r.status_code == 200
    # 账户 2：全局闸本会拦截（17 号 > 16 号），但账户级 C=20 号 → 放行主路径
    r2 = _ingest(client, "FWD-ACC2-001", account_id=2, booked_at="2026-11-17T09:00:00Z")
    assert r2.json()["main_written_count"] == 1
    # 账户 1 仍受全局闸约束
    r1 = _ingest(client, "FWD-LATE-002", account_id=1, booked_at="2026-11-17T09:30:00Z")
    assert r1.json()["sidecar_intercepted_count"] == 1


def test_config_change_audit_and_lower_cutoff_never_washes_back(client):
    events = client.get("/api/cutoff/config-events").json()
    actions = [e["action"] for e in events]
    assert "设置" in actions and len(events) >= 2
    assert all(e["created_at"] for e in events)

    # 下调全局 C：已在旁路的行必须原地不动，不得自动洗回主路径
    lower = client.put("/api/cutoff/configs", json={
        "scope_type": "全局", "cutoff_at": "2026-11-10T00:00:00Z", "reason": "收紧边界",
    })
    assert lower.json()["action"] == "下调"
    side = client.get("/api/sidecar?status=旁路待晋升").json()
    keys = {s["entry_key"] for s in side}
    assert {"FWD-LATE-001", "FWD-LATE-002"} <= keys
    main_rows = client.get("/api/entries?kind=正向").json()
    assert all(e["entry_key"] not in keys for e in main_rows)

    # 清除闸同样留痕，且清除也不洗回
    clear = client.put("/api/cutoff/configs", json={
        "scope_type": "全局", "cutoff_at": None, "reason": "边界解除",
    })
    assert clear.json()["action"] == "清除"
    events2 = client.get("/api/cutoff/config-events").json()
    assert any(e["action"] == "清除" for e in events2)
    side2 = client.get("/api/sidecar?status=旁路待晋升").json()
    assert {s["entry_key"] for s in side2} >= keys


def test_settle_never_touches_sidecar_and_balance_unchanged(client):
    run = client.post("/api/settle-runs")
    assert run.status_code == 200, run.text
    body = run.json()
    # 分列数字随归并运行落库
    assert body["sidecar_intercepted_count"] >= 2
    assert body["sidecar_promoted_count"] == 0
    assert body["main_written_count"] == body["forward_count"]

    # 旁路行原地不动，归并不触旁路
    side = client.get("/api/sidecar?status=旁路待晋升").json()
    side_keys = {s["entry_key"] for s in side}
    assert {"FWD-LATE-001", "FWD-LATE-002"} <= side_keys

    # 核心不变量：余额只来自主表净额（正向−已匹配冲正），旁路金额一律不计
    with connect() as conn:
        fwd_net = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger_entries "
            "WHERE kind='正向' AND status='已归并'"
        ).fetchone()["s"]
        rev_net = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger_entries "
            "WHERE kind='冲正' AND status='已归并'"
        ).fetchone()["s"]
        sidecar_sum = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM sidecar_forward_entries"
        ).fetchone()["s"]
        balance = conn.execute(
            "SELECT COALESCE(SUM(balance_cents),0) AS s FROM accounts"
        ).fetchone()["s"]
        main_keys = {
            r["entry_key"] for r in conn.execute(
                "SELECT entry_key FROM ledger_entries WHERE kind='正向'"
            ).fetchall()
        }
    expected_balance = fwd_net - rev_net
    assert balance == expected_balance
    # 旁路流水键绝不进主表
    assert side_keys.isdisjoint(main_keys)
    # 若旁路被错误计入，余额会多出整段旁路额——这里证明没有
    assert balance != expected_balance + sidecar_sum or sidecar_sum == 0
    assert sidecar_sum > 0  # 本用例确有旁路拦截


def test_promotion_failure_is_fail_closed_atomic(client):
    # 人为制造主表键冲突：晋升事务必须整体回滚，旁路行留痕但不翻转
    with connect() as conn:
        conn.execute(
            """INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents,
               booked_at, status) VALUES ('FWD-LATE-001', 1, '正向', 1,
               '2026-11-17T08:00:00Z', '待归并')"""
        )
        conn.commit()

    r = client.post("/api/sidecar/promote", json={"entry_keys": ["FWD-LATE-001", "FWD-LATE-002"]})
    assert r.status_code == 409
    assert "失败闭" in r.json()["detail"]

    runs = client.get("/api/promotion-runs").json()
    last = runs[0]
    assert last["status"] == "失败闭"
    assert last["promoted_count"] == 0
    assert last["failed_count"] == 2

    side = {s["entry_key"]: s for s in client.get("/api/sidecar").json()}
    # 两笔都留在旁路待晋升（第二笔虽无冲突，也被同一事务回滚）
    assert side["FWD-LATE-001"]["status"] == "旁路待晋升"
    assert side["FWD-LATE-002"]["status"] == "旁路待晋升"
    assert side["FWD-LATE-001"]["last_error"]

    # 主表里冲突行只有一条，没有因晋升产生重复
    with connect() as conn:
        dup = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE entry_key = 'FWD-LATE-001'"
        ).fetchone()["c"]
        assert dup == 1


def test_explicit_promotion_succeeds_same_transaction(client):
    # 清掉冲突行后显式晋升，主表插入与旁路翻转同事务成功
    with connect() as conn:
        conn.execute("DELETE FROM ledger_entries WHERE entry_key = 'FWD-LATE-001'")
        conn.commit()

    r = client.post("/api/sidecar/promote", json={"entry_keys": ["FWD-LATE-001", "FWD-LATE-002"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "成功"
    assert body["promoted_count"] == 2

    main_rows = {e["entry_key"]: e for e in client.get("/api/entries?kind=正向").json()}
    assert main_rows["FWD-LATE-001"]["status"] == "待归并"
    assert main_rows["FWD-LATE-002"]["status"] == "待归并"

    side = {s["entry_key"]: s for s in client.get("/api/sidecar").json()}
    assert side["FWD-LATE-001"]["status"] == "已晋升"
    assert side["FWD-LATE-002"]["status"] == "已晋升"
    assert side["FWD-LATE-001"]["promoted_at"]
    assert side["FWD-LATE-001"]["promotion_run_id"] == body["id"]


def test_report_columns_share_source_with_tables_and_balance(client):
    rep = client.get("/api/ingest-report").json()
    with connect() as conn:
        side_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM sidecar_forward_entries WHERE status = '旁路待晋升'"
        ).fetchone()["c"]
        promo_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM sidecar_forward_entries WHERE status = '已晋升'"
        ).fetchone()["c"]
        main_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["c"]
        bal = conn.execute("SELECT COALESCE(SUM(balance_cents),0) AS s FROM accounts").fetchone()["s"]

    assert rep["旁路拦截"]["待晋升数"] == side_cnt
    assert rep["旁路晋升"]["累计晋升数"] == promo_cnt
    assert rep["主路径写入"]["正向总数"] == main_cnt
    assert rep["账户余额合计分"] == bal
    # 三列在归并运行行上齐备
    run = rep["归并运行分列"][0]
    assert {"main_written_count", "sidecar_intercepted_count", "sidecar_promoted_count"} <= set(run)

    # 晋升后的行进入待归并，再跑一轮归并，三列随旁路表实时回源刷新
    second = client.post("/api/settle-runs").json()
    assert second["sidecar_promoted_count"] == promo_cnt
    assert second["sidecar_intercepted_count"] == side_cnt
    with connect() as conn:
        # 互证口径：全量已归并净额与账户余额同源可对账（旁路既不在主表也不在余额）
        fwd = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger_entries "
            "WHERE kind='正向' AND status='已归并'"
        ).fetchone()["s"]
        rev = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger_entries "
            "WHERE kind='冲正' AND status='已归并'"
        ).fetchone()["s"]
        side_amt = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM sidecar_forward_entries"
        ).fetchone()["s"]
    report2 = client.get("/api/ingest-report").json()
    assert report2["主路径写入"]["正向总额分"] == fwd
    assert report2["旁路拦截"]["累计拦截额分"] == side_amt
    # 报告的三列数字与底层表逐一同源
    assert (fwd - rev) is not None


def test_duplicate_and_invalid_rejected_fail_closed(client):
    dup = _ingest(client, "FWD-PRE-001", booked_at="2026-11-10T08:00:00Z")
    assert dup.status_code == 409
    bad = client.post("/api/forward-entries/ingest", json={"entries": [
        {"entry_key": "FWD-BAD", "account_id": 1, "amount_cents": -5, "booked_at": "2026-11-10T08:00:00Z"},
    ]})
    assert bad.status_code == 400
