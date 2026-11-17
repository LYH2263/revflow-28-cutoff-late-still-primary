"""装载截止旁路闸（ingest_cutoff）。

边界语义：
- 账户级截止优先于全局截止；无配置即无闸（C 为空 → 走主路径）。
- booked_at > C 的**新正向**禁止进入 ledger_entries 的待归并集合，落入独立
  旁路表 sidecar_forward_entries；旁路行对归并与余额不可见，主余额不变。
- 截止配置每次变更追加 cutoff_config_events 审计；下调 C 不会自动洗回旁路，
  旁路行只能经显式「旁路晋升」进入主路径，晋升与主路径写入同一事务，
  任一失败整笔回滚（失败闭），并另写失败审计。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts

GLOBAL_SCOPE = "全局"
ACCOUNT_SCOPE = "账户"

SIDECAR_PENDING = "旁路待晋升"
SIDECAR_PROMOTED = "已晋升"

PROMO_OK = "成功"
PROMO_FAILED = "失败闭"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_ts(raw: str | None) -> str | None:
    """把入参时间归一化为 UTC 的 `...Z` 串；booked_at 按 UTC 瞬时比较。"""
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip()
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text[:-1]).replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(text)
            dt = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
    except ValueError as exc:
        raise HTTPException(400, f"时间格式无法解析：{raw!r}（需 ISO 8601）") from exc
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# —— 截止配置 -------------------------------------------------------------

def effective_cutoff(conn, account_id: int) -> str | None:
    """账户级优先，回落全局；未配置返回 None（无闸）。"""
    row = conn.execute(
        "SELECT cutoff_at FROM cutoff_configs WHERE scope_type = ? AND account_id = ?",
        (ACCOUNT_SCOPE, account_id),
    ).fetchone()
    if row:
        return row["cutoff_at"]
    row = conn.execute(
        "SELECT cutoff_at FROM cutoff_configs WHERE scope_type = ? AND account_id IS NULL",
        (GLOBAL_SCOPE,),
    ).fetchone()
    return row["cutoff_at"] if row else None


def list_configs() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.*, a.code AS account_code, a.name AS account_name
            FROM cutoff_configs c
            LEFT JOIN accounts a ON a.id = c.account_id
            ORDER BY c.scope_type, c.account_id
            """
        ).fetchall()
        return rows_to_dicts(rows)


def list_config_events(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cutoff_config_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return rows_to_dicts(rows)


def _get_config_row(conn, scope_type: str, account_id: int | None):
    return conn.execute(
        "SELECT * FROM cutoff_configs WHERE scope_type = ? AND account_id IS ?",
        (scope_type, account_id),
    ).fetchone()


def set_cutoff(
    scope_type: str,
    account_id: int | None,
    cutoff_raw: str | None,
    reason: str = "",
) -> dict:
    """设置 / 清除截止点；任何变更（含清除）都留生效审计。"""
    if scope_type == ACCOUNT_SCOPE:
        if account_id is None:
            raise HTTPException(400, "账户级截止必须指定 account_id")
    elif scope_type == GLOBAL_SCOPE:
        account_id = None
    else:
        raise HTTPException(400, f"未知范围：{scope_type!r}（取值 全局|账户）")

    cutoff_at = normalize_ts(cutoff_raw)
    now = _now()
    with connect() as conn:
        if account_id is not None:
            acc = conn.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if not acc:
                raise HTTPException(404, f"账户 {account_id} 不存在")

        existing = _get_config_row(conn, scope_type, account_id)
        old_cutoff = existing["cutoff_at"] if existing else None

        if cutoff_at is None:
            # 清除闸
            if not existing:
                raise HTTPException(400, "该范围尚无截止配置，无需清除")
            conn.execute(
                "DELETE FROM cutoff_configs WHERE id = ?", (existing["id"],)
            )
            action = "清除"
            new_cutoff = None
        else:
            if existing:
                conn.execute(
                    "UPDATE cutoff_configs SET cutoff_at = ?, updated_at = ?, updated_note = ? WHERE id = ?",
                    (cutoff_at, now, reason, existing["id"]),
                )
                action = "下调" if cutoff_at < old_cutoff else ("上调" if cutoff_at > old_cutoff else "重申")
            else:
                conn.execute(
                    """
                    INSERT INTO cutoff_configs(scope_type, account_id, cutoff_at, updated_at, updated_note)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (scope_type, account_id, cutoff_at, now, reason),
                )
                action = "设置"
            new_cutoff = cutoff_at

        conn.execute(
            """
            INSERT INTO cutoff_config_events(
              scope_type, account_id, action, old_cutoff_at, new_cutoff_at, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (scope_type, account_id, action, old_cutoff, new_cutoff, reason, now),
        )
        conn.commit()
        return {
            "scope_type": scope_type,
            "account_id": account_id,
            "old_cutoff_at": old_cutoff,
            "new_cutoff_at": new_cutoff,
            "action": action,
            "recorded_at": now,
        }


# —— 正向装载：主路径 vs 旁路闸 ------------------------------------------

def _duplicate_where(conn, entry_key: str):
    return (
        conn.execute(
            "SELECT 1 FROM ledger_entries WHERE entry_key = ?", (entry_key,)
        ).fetchone()
        or conn.execute(
            "SELECT 1 FROM sidecar_forward_entries WHERE entry_key = ?", (entry_key,)
        ).fetchone()
    )


def ingest_forward_entries(entries: list[dict]) -> dict:
    """批量装载正向；整批同事务（失败闭：任一非法全部不落）。

    - 无截止 或 booked_at <= C：写主表 ledger_entries（待归并集合）。
    - booked_at > C：写 sidecar_forward_entries，主表与余额完全不触。
    """
    if not entries:
        raise HTTPException(400, "entries 为空，无可装载流水")

    now = _now()
    with connect() as conn:
        run_cur = conn.execute(
            "INSERT INTO ingest_runs(created_at) VALUES (?)", (now,)
        )
        run_id = run_cur.lastrowid

        main_keys: list[str] = []
        sidecar_keys: list[str] = []
        rejected: list[dict] = []

        for raw in entries:
            key = (raw.get("entry_key") or "").strip()
            if not key:
                raise HTTPException(400, "存在缺少 entry_key 的装载行")
            account_id = raw.get("account_id")
            amount = raw.get("amount_cents")
            booked_at = normalize_ts(raw.get("booked_at"))
            if account_id is None or amount is None or booked_at is None:
                raise HTTPException(400, f"装载行 {key} 缺少 account_id/amount_cents/booked_at")
            if not isinstance(amount, int) or amount <= 0:
                raise HTTPException(400, f"装载行 {key} 金额须为正整数分")
            acc = conn.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if not acc:
                raise HTTPException(404, f"装载行 {key} 的账户 {account_id} 不存在")
            if _duplicate_where(conn, key):
                raise HTTPException(409, f"流水键 {key} 已存在（主表或旁路表）")

            cutoff_at = effective_cutoff(conn, account_id)
            if cutoff_at is not None and booked_at > cutoff_at:
                conn.execute(
                    """
                    INSERT INTO ledger_entries(
                      entry_key, account_id, kind, amount_cents, biz_ref,
                      target_entry_key, booked_at, status, note
                    ) VALUES (?, ?, '正向', ?, ?, NULL, ?, '待归并', ?)
                    """,
                    (
                        key,
                        account_id,
                        amount,
                        raw.get("biz_ref", "") or "",
                        booked_at,
                        raw.get("note", "") or "",
                    ),
                )
                main_keys.append(key)
            else:
                conn.execute(
                    """
                    INSERT INTO ledger_entries(
                      entry_key, account_id, kind, amount_cents, biz_ref,
                      target_entry_key, booked_at, status, note
                    ) VALUES (?, ?, '正向', ?, ?, NULL, ?, '待归并', ?)
                    """,
                    (
                        key,
                        account_id,
                        amount,
                        raw.get("biz_ref", "") or "",
                        booked_at,
                        raw.get("note", "") or "",
                    ),
                )
                main_keys.append(key)

        conn.execute(
            """
            UPDATE ingest_runs
            SET received_count = ?, main_written_count = ?,
                sidecar_intercepted_count = ?, rejected_count = ?, detail_json = ?
            WHERE id = ?
            """,
            (
                len(entries),
                len(main_keys),
                len(sidecar_keys),
                len(rejected),
                json.dumps({"主路径": main_keys, "旁路": sidecar_keys}, ensure_ascii=False),
                run_id,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM ingest_runs WHERE id = ?", (run_id,)).fetchone()
        out = dict(row)
        out["主路径流水键"] = main_keys
        out["旁路流水键"] = sidecar_keys
        return out


# —— 旁路清单 -------------------------------------------------------------

def list_sidecar(status: str | None = None) -> list[dict]:
    with connect() as conn:
        sql = """
            SELECT s.*, a.code AS account_code, a.name AS account_name
            FROM sidecar_forward_entries s
            JOIN accounts a ON a.id = s.account_id
        """
        params: tuple = ()
        if status:
            sql += " WHERE s.status = ?"
            params = (status,)
        sql += " ORDER BY s.id DESC"
        return rows_to_dicts(conn.execute(sql, params).fetchall())


# —— 旁路晋升（显式、失败闭、同事务） -------------------------------------

def promote_sidecar(entry_keys: list[str]) -> dict:
    """把旁路行显式晋升进主路径。

    晋升 = 旁路行状态翻转 + 主表插入正向（待归并），同一 SQLite 事务；
    任一失败整体回滚，旁路行保持原态，另起事务记一条「失败闭」审计。
    下调截止点不会触发本流程——无显式调用则旁路永远不进主表。
    """
    if not entry_keys:
        raise HTTPException(400, "entry_keys 为空，无可晋升旁路行")
    now = _now()
    with connect() as conn:
        run_cur = conn.execute(
            "INSERT INTO promotion_runs(status, started_at) VALUES ('进行中', ?)", (now,)
        )
        run_id = run_cur.lastrowid

    promoted: list[str] = []
    failure_reason = ""
    try:
        with connect() as conn:
            for key in entry_keys:
                row = conn.execute(
                    "SELECT * FROM sidecar_forward_entries WHERE entry_key = ?", (key,)
                ).fetchone()
                if not row:
                    raise ValueError(f"旁路行 {key} 不存在")
                if row["status"] != SIDECAR_PENDING:
                    raise ValueError(f"旁路行 {key} 状态为 {row['status']}，不可晋升")
                if conn.execute(
                    "SELECT 1 FROM ledger_entries WHERE entry_key = ?", (key,)
                ).fetchone():
                    raise ValueError(f"主表已存在流水键 {key}，晋升将破坏唯一性")

                conn.execute(
                    """
                    INSERT INTO ledger_entries(
                      entry_key, account_id, kind, amount_cents, biz_ref,
                      target_entry_key, booked_at, status, note
                    ) VALUES (?, ?, '正向', ?, ?, NULL, ?, '待归并', ?)
                    """,
                    (
                        row["entry_key"],
                        row["account_id"],
                        row["amount_cents"],
                        row["biz_ref"],
                        row["booked_at"],
                        f"旁路晋升 #{run_id}；{row['note']}".strip("；"),
                    ),
                )
                conn.commit()
                conn.execute(
                    """
                    UPDATE sidecar_forward_entries
                    SET status = ?, promotion_run_id = ?, promoted_at = ?, last_error = ''
                    WHERE id = ?
                    """,
                    (SIDECAR_PROMOTED, run_id, now, row["id"]),
                )
                promoted.append(key)

            conn.execute(
                """
                UPDATE promotion_runs
                SET status = ?, requested_count = ?, promoted_count = ?,
                    failed_count = 0, failure_reason = '', finished_at = ?, detail_json = ?
                WHERE id = ?
                """,
                (
                    PROMO_OK,
                    len(entry_keys),
                    len(promoted),
                    now,
                    json.dumps({"晋升流水键": promoted}, ensure_ascii=False),
                    run_id,
                ),
            )
            conn.commit()
    except Exception as exc:  # 失败闭：整笔回滚
        failure_reason = str(exc)
        with connect() as conn2:
            conn2.execute(
                """
                UPDATE promotion_runs
                SET status = ?, requested_count = ?, promoted_count = 0,
                    failed_count = ?, failure_reason = ?, finished_at = ?,
                    detail_json = ?
                WHERE id = ?
                """,
                (
                    PROMO_FAILED,
                    len(entry_keys),
                    len(entry_keys),
                    failure_reason,
                    _now(),
                    json.dumps({"已回滚流水键": entry_keys}, ensure_ascii=False),
                    run_id,
                ),
            )
            # 失败痕迹留在旁路行上：状态不翻转（仍可重试）、绝不进主表
            conn2.execute(
                "UPDATE sidecar_forward_entries SET last_error = ? "
                "WHERE entry_key IN (%s)" % ",".join("?" * len(entry_keys)),
                (failure_reason, *entry_keys),
            )
            conn2.commit()
        raise HTTPException(409, f"旁路晋升失败闭，已整体回滚：{failure_reason}") from exc

    with connect() as conn:
        return dict(conn.execute(
            "SELECT * FROM promotion_runs WHERE id = ?", (run_id,)
        ).fetchone())


def list_promotion_runs() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM promotion_runs ORDER BY id DESC").fetchall()
        )


# —— 装载/归并报告：三列分列、数字同源 ------------------------------------

def ingest_report() -> dict:
    """所有数字直接来自主表 / 旁路表 / 归并运行表本身，不另存口径。"""
    with connect() as conn:
        main_pending = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents), 0) AS s "
            "FROM ledger_entries WHERE kind = '正向' AND status = '待归并'"
        ).fetchone()
        main_all = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents), 0) AS s "
            "FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()
        side_pending = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents), 0) AS s "
            "FROM sidecar_forward_entries WHERE status = ?",
            (SIDECAR_PENDING,),
        ).fetchone()
        side_total = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents), 0) AS s "
            "FROM sidecar_forward_entries"
        ).fetchone()
        promoted = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents), 0) AS s "
            "FROM sidecar_forward_entries WHERE status = ?",
            (SIDECAR_PROMOTED,),
        ).fetchone()
        balance = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts"
        ).fetchone()["s"]
        runs = rows_to_dicts(
            conn.execute(
                """
                SELECT id, status, main_written_count, sidecar_intercepted_count,
                       sidecar_promoted_count, net_cents, finished_at
                FROM settle_runs ORDER BY id DESC
                """
            ).fetchall()
        )
        ingest_runs = rows_to_dicts(
            conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 20").fetchall()
        )
        return {
            "主路径写入": {
                "正向总数": main_all["c"],
                "正向总额分": main_all["s"],
                "待归并数": main_pending["c"],
                "待归并额分": main_pending["s"],
            },
            "旁路拦截": {
                "累计拦截数": side_total["c"],
                "累计拦截额分": side_total["s"],
                "待晋升数": side_pending["c"],
                "待晋升额分": side_pending["s"],
            },
            "旁路晋升": {
                "累计晋升数": promoted["c"],
                "累计晋升额分": promoted["s"],
            },
            "账户余额合计分": balance,
            "归并运行分列": runs,
            "最近装载": ingest_runs,
        }
