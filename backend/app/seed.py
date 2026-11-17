from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  balance_cents INTEGER NOT NULL DEFAULT 0,
  last_settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  biz_ref TEXT NOT NULL DEFAULT '',
  target_entry_key TEXT,
  booked_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待归并',
  note TEXT NOT NULL DEFAULT '',
  settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS quarantine_items (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  reason TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  settle_run_id INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settle_runs (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  forward_count INTEGER NOT NULL DEFAULT 0,
  reversal_count INTEGER NOT NULL DEFAULT 0,
  settled_count INTEGER NOT NULL DEFAULT 0,
  quarantine_count INTEGER NOT NULL DEFAULT 0,
  net_cents INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS verify_reports (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER REFERENCES settle_runs(id),
  ledger_net_cents INTEGER NOT NULL,
  balance_sum_cents INTEGER NOT NULL,
  quarantine_count INTEGER NOT NULL,
  consistent INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
-- 装载截止闸：当前生效配置（全局一行 + 每账户至多一行）
CREATE TABLE IF NOT EXISTS cutoff_configs (
  id INTEGER PRIMARY KEY,
  scope_type TEXT NOT NULL,
  account_id INTEGER,
  cutoff_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_note TEXT NOT NULL DEFAULT '',
  UNIQUE(scope_type, account_id)
);
-- 截止配置变更审计（只追加）
CREATE TABLE IF NOT EXISTS cutoff_config_events (
  id INTEGER PRIMARY KEY,
  scope_type TEXT NOT NULL,
  account_id INTEGER,
  action TEXT NOT NULL,
  old_cutoff_at TEXT,
  new_cutoff_at TEXT,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
-- 截止后正向旁路区：独立物理表，不进 ledger_entries，归并与余额均不可见
CREATE TABLE IF NOT EXISTS sidecar_forward_entries (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  amount_cents INTEGER NOT NULL,
  biz_ref TEXT NOT NULL DEFAULT '',
  booked_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  cutoff_at_snapshot TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '旁路待晋升',
  ingest_run_id INTEGER,
  promotion_run_id INTEGER,
  intercepted_at TEXT NOT NULL,
  promoted_at TEXT,
  last_error TEXT NOT NULL DEFAULT ''
);
-- 截止配置：全局至多一行、每账户至多一行。
-- 注意 SQLite 的 UNIQUE(scope_type, account_id) 对 NULL 不去重（NULL != NULL），
-- 因此全局行必须另用部分唯一索引兜住，否则重复「设置」会留多行，
-- effective_cutoff 随机取到旧（更宽松）截止 → 二次装载穿闸。
-- 索引在 init_db 中先清重、后创建，兼容老库。
-- 每次装载的分列记账
CREATE TABLE IF NOT EXISTS ingest_runs (
  id INTEGER PRIMARY KEY,
  received_count INTEGER NOT NULL DEFAULT 0,
  main_written_count INTEGER NOT NULL DEFAULT 0,
  sidecar_intercepted_count INTEGER NOT NULL DEFAULT 0,
  rejected_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}'
);
-- 截止证明（只读实体）：按装载批次 id 固化「截止时刻 / 正式键 / 旁路键 / 晋升键」。
-- 与装载分流同一事务写入；晋升成功后把晋升键回写到原装载批次证明。
CREATE TABLE IF NOT EXISTS cutoff_proofs (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL UNIQUE,
  cutoff_at TEXT NOT NULL DEFAULT '',
  cutoff_by_account_json TEXT NOT NULL DEFAULT '{}',
  main_keys_json TEXT NOT NULL DEFAULT '[]',
  sidecar_keys_json TEXT NOT NULL DEFAULT '[]',
  promotion_keys_json TEXT NOT NULL DEFAULT '[]',
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- 旁路晋升运行（成功/失败闭审计）
CREATE TABLE IF NOT EXISTS promotion_runs (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  requested_count INTEGER NOT NULL DEFAULT 0,
  promoted_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  failure_reason TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}'
);
"""

# 老库补列：归并运行的三列分列数字
_SETTLE_RUN_NEW_COLUMNS = (
    ("main_written_count", "INTEGER NOT NULL DEFAULT 0"),
    ("sidecar_intercepted_count", "INTEGER NOT NULL DEFAULT 0"),
    ("sidecar_promoted_count", "INTEGER NOT NULL DEFAULT 0"),
)


def _ensure_cutoff_uniqueness(conn) -> None:
    """老库兜底：清掉历史重复截止行（保留最新），再加部分唯一索引。

    截止标记重复会让 effective_cutoff 读到旧（更宽松）的截止点，
    导致刷新后的二次装载穿闸；这里在启动时收敛为「每范围至多一行」。
    """
    conn.execute(
        """
        DELETE FROM cutoff_configs
        WHERE id NOT IN (
          SELECT MAX(id) FROM cutoff_configs
          GROUP BY scope_type, COALESCE(account_id, -1)
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_cutoff_global "
        "ON cutoff_configs(scope_type) WHERE account_id IS NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_cutoff_account "
        "ON cutoff_configs(scope_type, account_id) WHERE account_id IS NOT NULL"
    )


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(settle_runs)").fetchall()}
        for col, decl in _SETTLE_RUN_NEW_COLUMNS:
            if col not in existing:
                conn.execute(f"ALTER TABLE settle_runs ADD COLUMN {col} {decl}")
        _ensure_cutoff_uniqueness(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        if n:
            conn.commit()
            return
        seed_path = FIXTURES_DIR / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for a in data["accounts"]:
            conn.execute(
                "INSERT INTO accounts(id, code, name, currency, balance_cents) VALUES (?, ?, ?, ?, 0)",
                (a["id"], a["code"], a["name"], a["currency"]),
            )
        for e in data["forward_entries"]:
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '正向', ?, ?, NULL, ?, '待归并', ?)
                """,
                (
                    e["id"],
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        # 冲正 id 与正向可能重叠，用独立自增：先取 max
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ledger_entries").fetchone()["m"]
        for i, e in enumerate(data["reversal_entries"], start=1):
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '冲正', ?, ?, ?, ?, '待归并', ?)
                """,
                (
                    max_id + i,
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e.get("target_entry_key"),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        conn.commit()
