# -*- coding: utf-8 -*-
"""SQLite 数据访问与自动迁移。

所有金额以“分”为单位（INTEGER），避免浮点误差。
钞箱容量以“张”为单位。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("ATM_DB", os.path.join(os.getcwd(), "data", "atm.db"))
ATTACH_DIR = os.environ.get("ATM_ATTACH", os.path.join(os.getcwd(), "data", "attachments"))

_lock = threading.RLock()
_initialized = False


def now_iso() -> str:
    """当前 UTC ISO 时间（业务时间也统一用它，演示项目不做时区换算）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def tx(conn=None):
    """串行化事务：进程内全局锁 + IMMEDIATE，保证领取/签收并发正确。"""
    own = conn is None
    if own:
        conn = get_conn()
    _lock.acquire()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _lock.release()
        if own:
            conn.close()


SCHEMA = """
-- 网点
CREATE TABLE IF NOT EXISTS branches (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    code        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

-- 员工（与网点多对多，决定网点数据权限）
CREATE TABLE IF NOT EXISTS employees (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    staff_no    TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL DEFAULT '柜员',      -- 主管/清分员/柜员
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employee_branches (
    employee_id TEXT NOT NULL REFERENCES employees(id),
    branch_id   TEXT NOT NULL REFERENCES branches(id),
    PRIMARY KEY (employee_id, branch_id)
);

-- 券别
CREATE TABLE IF NOT EXISTS denominations (
    id          TEXT PRIMARY KEY,
    value_cents INTEGER NOT NULL UNIQUE,           -- 面值（分）
    label       TEXT NOT NULL,                      -- “100 元”
    sort_seq    INTEGER NOT NULL
);

-- ATM
CREATE TABLE IF NOT EXISTS atms (
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    brand       TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    branch_id   TEXT NOT NULL REFERENCES branches(id),
    online      INTEGER NOT NULL DEFAULT 1,         -- 0 = 离线（回传挂起）
    created_at  TEXT NOT NULL
);

-- 钞箱：编号、券别容量、封签号、当前位置
CREATE TABLE IF NOT EXISTS cassettes (
    id            TEXT PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,             -- 钞箱编号
    denom_id      TEXT REFERENCES denominations(id),
    capacity      INTEGER NOT NULL DEFAULT 2500,    -- 券别容量（张）
    seal_no       TEXT,                             -- 封签号（封存后不可直接改数量）
    sealed        INTEGER NOT NULL DEFAULT 0,
    -- 当前位置：branch 仓 / atm 机 / transit 在途 / vault 中心库
    location_type TEXT NOT NULL DEFAULT 'vault',
    location_ref  TEXT,                             -- 网点或 ATM id
    status        TEXT NOT NULL DEFAULT '空闲',      -- 空闲/待用/在柜/待清分/在途
    updated_at    TEXT NOT NULL
);

-- 装钞计划（锁定后才能装钞）
CREATE TABLE IF NOT EXISTS load_plans (
    id           TEXT PRIMARY KEY,
    plan_no      TEXT NOT NULL UNIQUE,
    atm_id       TEXT NOT NULL REFERENCES atms(id),
    branch_id    TEXT NOT NULL REFERENCES branches(id),
    planned_date TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT '草稿',       -- 草稿/已锁定/装钞中/已完成/已取消
    locked_by    TEXT REFERENCES employees(id),
    locked_at    TEXT,
    created_by   TEXT REFERENCES employees(id),
    created_at   TEXT NOT NULL,
    note         TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS load_plan_items (
    id           TEXT PRIMARY KEY,
    plan_id      TEXT NOT NULL REFERENCES load_plans(id) ON DELETE CASCADE,
    cassette_id  TEXT NOT NULL REFERENCES cassettes(id),
    denom_id     TEXT NOT NULL REFERENCES denominations(id),
    notes        INTEGER NOT NULL,                  -- 计划张数
    amount_cents INTEGER NOT NULL                   -- 计划金额（分）
);

-- 钞箱轨迹（装钞、签收、交接、封存、清机出入）
CREATE TABLE IF NOT EXISTS cassette_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cassette_id  TEXT NOT NULL REFERENCES cassettes(id),
    event_type   TEXT NOT NULL,                     -- 出库/装柜/签收/交接/封存/拆封/清机/回库
    from_loc     TEXT DEFAULT '',
    to_loc       TEXT DEFAULT '',
    seal_no      TEXT DEFAULT '',
    seal_intact  INTEGER DEFAULT 1,                 -- 封签是否完好
    employee_id  TEXT REFERENCES employees(id),
    employee2_id TEXT REFERENCES employees(id),
    ref_no       TEXT DEFAULT '',                   -- 关联单据号
    detail       TEXT DEFAULT '',
    created_at   TEXT NOT NULL
);

-- 清机单
CREATE TABLE IF NOT EXISTS clear_orders (
    id            TEXT PRIMARY KEY,
    order_no      TEXT NOT NULL UNIQUE,
    atm_id        TEXT NOT NULL REFERENCES atms(id),
    branch_id     TEXT NOT NULL REFERENCES branches(id),
    plan_id       TEXT REFERENCES load_plans(id),
    status        TEXT NOT NULL DEFAULT '清点中',    -- 清点中/待复核/已复核/已关闭
    expected_cents INTEGER NOT NULL DEFAULT 0,      -- 应点金额（账面）
    counted_cents INTEGER NOT NULL DEFAULT 0,       -- 实点金额合计
    residual_cents INTEGER NOT NULL DEFAULT 0,      -- 残钞金额合计
    diff_cents    INTEGER NOT NULL DEFAULT 0,       -- 短/长款
    reported      INTEGER NOT NULL DEFAULT 0,       -- 是否已成功回传
    pending_sync  INTEGER NOT NULL DEFAULT 0,       -- 是否有挂起的补传
    submit_ref    TEXT DEFAULT '',                  -- 回传批次号（判重）
    created_by    TEXT REFERENCES employees(id),
    created_at    TEXT NOT NULL,
    closed_at     TEXT,
    synced_at     TEXT,
    remark        TEXT DEFAULT ''                  -- 现场备注（如吞卡演示卡号登记）
);

-- 清机清点明细（逐券别录入）
CREATE TABLE IF NOT EXISTS clear_counts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT NOT NULL REFERENCES clear_orders(id) ON DELETE CASCADE,
    denom_id     TEXT NOT NULL REFERENCES denominations(id),
    expected_notes INTEGER NOT NULL DEFAULT 0,
    counted_notes  INTEGER NOT NULL DEFAULT 0,
    residual_notes INTEGER NOT NULL DEFAULT 0,      -- 残钞张数
    amount_cents   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(order_id, denom_id)
);

-- 差异记录
CREATE TABLE IF NOT EXISTS discrepancies (
    id           TEXT PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,
    order_id     TEXT REFERENCES clear_orders(id),
    cassette_id  TEXT REFERENCES cassettes(id),
    kind         TEXT NOT NULL,                     -- 短款/长款/残钞/封签破损/ATM离线/重复回传
    amount_cents INTEGER NOT NULL DEFAULT 0,
    due_date     TEXT NOT NULL,                     -- 处理期限
    status       TEXT NOT NULL DEFAULT '待复核',     -- 待复核/复核通过/复核驳回/已处置
    description  TEXT DEFAULT '',
    created_by   TEXT REFERENCES employees(id),
    created_at   TEXT NOT NULL,
    resolved_by  TEXT REFERENCES employees(id),
    resolved_at  TEXT,
    resolution   TEXT DEFAULT ''
);

-- 双人复核
CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    discrepancy_id TEXT NOT NULL REFERENCES discrepancies(id) ON DELETE CASCADE,
    reviewer1_id TEXT REFERENCES employees(id),
    reviewer2_id TEXT REFERENCES employees(id),
    result       TEXT NOT NULL,                      -- 通过/驳回
    comment      TEXT DEFAULT '',
    created_at   TEXT NOT NULL
);

-- 金额修正留痕（原值、新值、原因、审批人）
CREATE TABLE IF NOT EXISTS amount_corrections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT NOT NULL REFERENCES clear_orders(id),
    denom_id      TEXT REFERENCES denominations(id),
    old_cents     INTEGER NOT NULL,
    new_cents     INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    approver_id   TEXT NOT NULL REFERENCES employees(id),
    operator_id   TEXT REFERENCES employees(id),
    created_at    TEXT NOT NULL
);

-- 清机附件（本地目录保存文件）
CREATE TABLE IF NOT EXISTS attachments (
    id           TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES clear_orders(id),
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT DEFAULT '',
    size_bytes   INTEGER DEFAULT 0,
    uploaded_by  TEXT REFERENCES employees(id),
    created_at   TEXT NOT NULL
);

-- 进程内补传任务队列（同时落库，重启不丢）
CREATE TABLE IF NOT EXISTS sync_tasks (
    id           TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES clear_orders(id),
    payload      TEXT NOT NULL,                     -- 待回传数据快照 JSON
    status       TEXT NOT NULL DEFAULT '挂起',       -- 挂起/待发/成功/失败
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 审计日志
CREATE TABLE IF NOT EXISTS audit_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id     TEXT REFERENCES employees(id),
    branch_id    TEXT,
    action       TEXT NOT NULL,
    entity       TEXT NOT NULL,
    entity_id    TEXT DEFAULT '',
    detail       TEXT DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_cassette ON cassette_events(cassette_id, id);
CREATE INDEX IF NOT EXISTS idx_counts_order ON clear_counts(order_id);
CREATE INDEX IF NOT EXISTS idx_disc_order ON discrepancies(order_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_logs(created_at);
"""


def _migrate_columns(conn):
    """对已存在的库补齐后加列（幂等）。"""
    wanted = {
        "clear_orders": [("submit_ref", "TEXT DEFAULT ''"),
                         ("remark", "TEXT DEFAULT ''")],
    }
    for table, cols in wanted.items():
        existing = {r["name"] for r in conn.execute(
            f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, decl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db():
    """自动迁移：建库建表 + 初始券别（幂等）。"""
    global _initialized
    with _lock:
        if _initialized:
            return
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        os.makedirs(ATTACH_DIR, exist_ok=True)
        conn = get_conn()
        try:
            conn.executescript(SCHEMA)
            _migrate_columns(conn)
            seed_denominations(conn)
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def seed_denominations(conn):
    cur = conn.execute("SELECT COUNT(*) c FROM denominations")
    if cur.fetchone()["c"]:
        return
    rows = [
        ("d100", 10000, "100 元", 1),
        ("d50", 5000, "50 元", 2),
        ("d20", 2000, "20 元", 3),
        ("d10", 1000, "10 元", 4),
    ]
    conn.executemany(
        "INSERT INTO denominations(id,value_cents,label,sort_seq) VALUES (?,?,?,?)", rows
    )
