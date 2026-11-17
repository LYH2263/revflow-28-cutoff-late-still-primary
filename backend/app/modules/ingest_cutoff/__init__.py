"""装载截止旁路闸（ingest_cutoff）。

分流路径（本项目择一，已写死）：**装载时按截止分流「旁路 / 正式」**。
不在归并开跑前事后剔除——装载这一处是唯一的分流决定点，避免归并扫描另起
一套口径造成漂移。

边界语义：
- 账户级截止优先于全局截止；无配置即无闸（C 为空 → 走主路径）。
- booked_at > C 的**新正向**禁止进入 ledger_entries 的待归并集合，落入独立
  旁路表 sidecar_forward_entries（本项目旁路落库择「独立旁路表」，不使用
  流水通道标记列）；旁路行对归并与余额不可见，主余额不变。
- 每次装载把「截止时刻 / 正式键 / 旁路键」与分流同一事务写入截止证明
  cutoff_proofs（按装载批次 id 唯一）；晋升成功后把晋升键回填同一证明。
  证明只由装载/晋升写入口维护，查询接口只读。
- 截止配置每次变更追加 cutoff_config_events 审计；刷新 / 下调 / 上调 / 清除
  均不删除历史审计，也不改写旁路行上的 cutoff_at_snapshot。下调 C 不会自动
  洗回旁路，旁路行只能经显式「旁路晋升」进入主路径。
- 晋升必须显式调用且重新校验截止规则：行入账时间仍晚于当前生效截止时拒绝
  晋升。晋升与主路径写入同一事务，任一失败整笔回滚（失败闭），绝不半写入
  正式待归并集合，并另写失败审计。
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

CHANNEL_MAIN = "正式"
CHANNEL_SIDECAR = "旁路"



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


def _insert_main_forward(
    conn, *, entry_key: str, account_id: int, amount: int, biz_ref: str,
    booked_at: str, note: str,
) -> None:
    conn.execute(
        """
        INSERT INTO ledger_entries(
          entry_key, account_id, kind, amount_cents, biz_ref,
          target_entry_key, booked_at, status, note
        ) VALUES (?, ?, '正向', ?, ?, NULL, ?, '待归并', ?)
        """,
        (entry_key, account_id, amount, biz_ref, booked_at, note),
    )


def _insert_sidecar(
    conn, *, entry_key: str, account_id: int, amount: int, biz_ref: str,
    booked_at: str, note: str, cutoff_at: str, ingest_run_id: int, now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO sidecar_forward_entries(
          entry_key, account_id, amount_cents, biz_ref, booked_at, note,
          cutoff_at_snapshot, status, ingest_run_id, intercepted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry_key, account_id, amount, biz_ref, booked_at, note,
            cutoff_at, SIDECAR_PENDING, ingest_run_id, now,
        ),
    )


def _write_cutoff_proof(
    conn, *, batch_id: int, cutoff_by_account: dict[str, str],
    main_keys: list[str], sidecar_keys: list[str],
    promotion_keys: list[str], now: str,
) -> None:
    """截止证明与装载分流同一事务写入；按批次 id 唯一。"""
    cutoffs = sorted(set(cutoff_by_account.values()))
    proof_cutoff = cutoffs[0] if len(cutoffs) == 1 else (";".join(cutoffs) if cutoffs else "")
    conn.execute(
        """
        INSERT INTO cutoff_proofs(
          batch_id, cutoff_at, cutoff_by_account_json, main_keys_json,
          sidecar_keys_json, promotion_keys_json, detail_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(batch_id) DO UPDATE SET
          cutoff_at = excluded.cutoff_at,
          cutoff_by_account_json = excluded.cutoff_by_account_json,
          main_keys_json = excluded.main_keys_json,
          sidecar_keys_json = excluded.sidecar_keys_json,
          promotion_keys_json = excluded.promotion_keys_json,
          detail_json = excluded.detail_json,
          updated_at = excluded.updated_at
        """,
        (
            batch_id,
            proof_cutoff,
            json.dumps(cutoff_by_account, ensure_ascii=False),
            json.dumps(main_keys, ensure_ascii=False),
            json.dumps(sidecar_keys, ensure_ascii=False),
            json.dumps(promotion_keys, ensure_ascii=False),
            json.dumps(
                {
                    "分流路径": "装载时按截止分流（旁路/正式）",
                    "正式通道": CHANNEL_MAIN,
                    "旁路通道": CHANNEL_SIDECAR,
                },
                ensure_ascii=False,
            ),
            now,
            now,
        ),
    )


def ingest_forward_entries(entries: list[dict]) -> dict:
    """批量装载正向；整批同事务（失败闭：任一非法全部不落）。

    - 无截止 或 booked_at <= C：写主表 ledger_entries（正式待归并集合）。
    - booked_at > C：写 sidecar_forward_entries，主表与余额完全不触。
    - 同事务写截止证明（batch_id = ingest_runs.id）。
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
        cutoff_by_account: dict[str, str] = {}
        channel_by_key: dict[str, str] = {}
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

            biz_ref = raw.get("biz_ref", "") or ""
            note = raw.get("note", "") or ""
            cutoff_at = effective_cutoff(conn, account_id)
            if cutoff_at is not None and booked_at > cutoff_at:
                _insert_sidecar(
                    conn,
                    entry_key=key, account_id=account_id, amount=amount,
                    biz_ref=biz_ref, booked_at=booked_at, note=note,
                    cutoff_at=cutoff_at, ingest_run_id=run_id, now=now,
                )
                sidecar_keys.append(key)
                channel_by_key[key] = CHANNEL_SIDECAR
            else:
                _insert_main_forward(
                    conn,
                    entry_key=key, account_id=account_id, amount=amount,
                    biz_ref=biz_ref, booked_at=booked_at, note=note,
                )
                main_keys.append(key)
                channel_by_key[key] = CHANNEL_MAIN
            if cutoff_at is not None:
                cutoff_by_account[str(account_id)] = cutoff_at

        _write_cutoff_proof(
            conn,
            batch_id=run_id,
            cutoff_by_account=cutoff_by_account,
            main_keys=main_keys,
            sidecar_keys=sidecar_keys,
            promotion_keys=[],
            now=now,
        )
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
                json.dumps(
                    {
                        "主路径": main_keys,
                        "旁路": sidecar_keys,
                        "通道": channel_by_key,
                    },
                    ensure_ascii=False,
                ),
                run_id,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM ingest_runs WHERE id = ?", (run_id,)).fetchone()
        out = dict(row)
        out["主路径流水键"] = main_keys
        out["旁路流水键"] = sidecar_keys
        out["通道"] = channel_by_key
        out["截止证明"] = get_cutoff_proof(run_id)
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
        out = rows_to_dicts(conn.execute(sql, params).fetchall())
        for row in out:
            # 通道/证明口径与正式流水同源：旁路表即旁路轨，快照即装载时命中截止
            row["通道"] = CHANNEL_SIDECAR
        return out


# —— 旁路晋升（显式、校验截止、失败闭、同事务） ---------------------------

def promote_sidecar(entry_keys: list[str]) -> dict:
    """把旁路行显式晋升进主路径（正式待归并集合）。

    前置校验（任一不满足 = 整笔拒绝，旁路行原态不动）：
    - 行存在且状态为「旁路待晋升」；
    - 主表无同名键（唯一性）；
    - **截止规则**：晋升时刻该行 booked_at 必须已不晚于账户当前生效截止
      （或该范围已无闸）。仍超截止的行不得晋升——否则等于绕过装载闸。

    通过后：主表插入 + 旁路行状态翻转 + 晋升键回填原装载批次截止证明，
    全部在**同一事务**；任一步失败整体回滚，绝不半写入正式待归并集合。
    失败审计另起事务写入。
    """
    if not entry_keys:
        raise HTTPException(400, "entry_keys 为空，无可晋升旁路行")
    requested = list(dict.fromkeys(entry_keys))
    now = _now()
    with connect() as conn:
        run_cur = conn.execute(
            "INSERT INTO promotion_runs(status, started_at) VALUES ('进行中', ?)", (now,)
        )
        run_id = run_cur.lastrowid
        conn.commit()

    promoted: list[str] = []
    proofs_by_batch: dict[int, list[str]] = {}
    try:
        with connect() as conn:
            for key in requested:
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

                cutoff_at = effective_cutoff(conn, row["account_id"])
                if cutoff_at is not None and row["booked_at"] > cutoff_at:
                    raise ValueError(
                        f"旁路行 {key} 入账时间 {row['booked_at']} 仍晚于当前截止 "
                        f"{cutoff_at}，不得晋升"
                    )

                _insert_main_forward(
                    conn,
                    entry_key=row["entry_key"],
                    account_id=row["account_id"],
                    amount=row["amount_cents"],
                    biz_ref=row["biz_ref"],
                    booked_at=row["booked_at"],
                    note=f"旁路晋升 #{run_id}；{row['note']}".strip("；"),
                )
                conn.execute(
                    """
                    UPDATE sidecar_forward_entries
                    SET status = ?, promotion_run_id = ?, promoted_at = ?, last_error = ''
                    WHERE id = ?
                    """,
                    (SIDECAR_PROMOTED, run_id, now, row["id"]),
                )
                promoted.append(key)
                if row["ingest_run_id"] is not None:
                    proofs_by_batch.setdefault(int(row["ingest_run_id"]), []).append(key)

            # 晋升键回填到原装载批次的截止证明（同事务）
            for batch_id, keys in proofs_by_batch.items():
                proof = conn.execute(
                    "SELECT promotion_keys_json FROM cutoff_proofs WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                if proof:
                    existing = json.loads(proof["promotion_keys_json"] or "[]")
                    merged = list(dict.fromkeys([*existing, *keys]))
                    conn.execute(
                        "UPDATE cutoff_proofs SET promotion_keys_json = ?, updated_at = ? "
                        "WHERE batch_id = ?",
                        (json.dumps(merged, ensure_ascii=False), now, batch_id),
                    )

            conn.execute(
                """
                UPDATE promotion_runs
                SET status = ?, requested_count = ?, promoted_count = ?,
                    failed_count = 0, failure_reason = '', finished_at = ?, detail_json = ?
                WHERE id = ?
                """,
                (
                    PROMO_OK,
                    len(requested),
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
                    len(requested),
                    len(requested),
                    failure_reason,
                    _now(),
                    json.dumps({"已回滚流水键": requested}, ensure_ascii=False),
                    run_id,
                ),
            )
            # 失败痕迹留在旁路行上：状态不翻转（仍可重试）、绝不进主表
            conn2.execute(
                "UPDATE sidecar_forward_entries SET last_error = ? "
                "WHERE entry_key IN (%s)" % ",".join("?" * len(requested)),
                (failure_reason, *requested),
            )
            conn2.commit()
        raise HTTPException(409, f"旁路晋升失败闭，已整体回滚：{failure_reason}") from exc

    with connect() as conn:
        out = dict(conn.execute(
            "SELECT * FROM promotion_runs WHERE id = ?", (run_id,)
        ).fetchone())
        out["截止证明"] = {
            str(batch_id): get_cutoff_proof(batch_id)
            for batch_id in proofs_by_batch
        }
        return out


def list_promotion_runs() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM promotion_runs ORDER BY id DESC").fetchall()
        )


# —— 截止证明（只读接口：按装载批次 id 查询） ----------------------------

def _proof_out(row) -> dict:
    """把证明行展开成可读实体；数字/键集合全部回源证明列本身。"""
    out = dict(row)
    out["cutoff_by_account"] = json.loads(out.pop("cutoff_by_account_json") or "{}")
    out["main_keys"] = json.loads(out.pop("main_keys_json") or "[]")
    out["sidecar_keys"] = json.loads(out.pop("sidecar_keys_json") or "[]")
    out["promotion_keys"] = json.loads(out.pop("promotion_keys_json") or "[]")
    out["detail"] = json.loads(out.pop("detail_json") or "{}")
    return out


def get_cutoff_proof(batch_id: int) -> dict | None:
    """按装载批次 id 查截止证明；不存在返回 None。只读。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM cutoff_proofs WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return _proof_out(row) if row else None


def list_cutoff_proofs(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cutoff_proofs ORDER BY batch_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_proof_out(r) for r in rows]


def get_cutoff_proof_or_404(batch_id: int) -> dict:
    proof = get_cutoff_proof(batch_id)
    if proof is None:
        raise HTTPException(404, f"装载批次 {batch_id} 的截止证明不存在")
    return proof


def list_ingest_runs(limit: int = 50) -> list[dict]:
    """装载批次清单：分列键 + 每键通道 + 同批次截止证明（全部回源装载表/证明表）。"""
    limit = max(1, min(int(limit), 500))
    with connect() as conn:
        rows = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ingest_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        )
        proof_rows = {
            r["batch_id"]: _proof_out(r)
            for r in conn.execute(
                "SELECT * FROM cutoff_proofs ORDER BY batch_id DESC LIMIT ?", (limit,)
            ).fetchall()
        }
    for row in rows:
        detail = json.loads(row.get("detail_json") or "{}")
        row["主路径流水键"] = detail.get("主路径", [])
        row["旁路流水键"] = detail.get("旁路", [])
        row["通道"] = detail.get("通道", {})
        row["截止证明"] = proof_rows.get(row["id"])
    return rows


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
        proofs = [
            _proof_out(r)
            for r in conn.execute(
                "SELECT * FROM cutoff_proofs ORDER BY batch_id DESC LIMIT 50"
            ).fetchall()
        ]
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
            "截止证明": proofs,
        }
