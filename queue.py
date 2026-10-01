# -*- coding: utf-8 -*-
"""进程内补传任务队列。

清机时若 ATM 离线，清点数据落库为 sync_tasks(挂起)；守护线程周期扫描，
ATM 恢复在线后自动补传，成功后回写清机单。重复批次由 submit_ref 去重，
模拟核心系统“重复回传拒收”。
"""
from __future__ import annotations

import json
import threading
import time

from db import get_conn, tx, now_iso

STOP = threading.Event()


def _attempt_once(conn, task) -> tuple[bool, str]:
    """模拟一次核心系统回传。返回 (是否成功, 错误信息)。"""
    atm = conn.execute(
        """SELECT a.* FROM atms a JOIN clear_orders o ON o.atm_id=a.id
           WHERE o.id=?""", (task["order_id"],)).fetchone()
    if not atm or not atm["online"]:
        return False, "ATM 仍离线"
    payload = json.loads(task["payload"])
    ref = payload.get("submit_ref") or ""
    if ref:
        # 核心侧去重：同批次已成功过则拒收（正常不会走到，双保险）
        dup = conn.execute(
            "SELECT 1 FROM clear_orders WHERE id=? AND reported=1 AND submit_ref=?",
            (task["order_id"], ref),
        ).fetchone()
        if dup:
            return False, f"核心系统拒收：批次 {ref} 重复回传"
    return True, ""


def pump_once() -> int:
    """执行一轮补传，返回成功条数。"""
    done = 0
    with tx() as conn:
        tasks = conn.execute(
            "SELECT * FROM sync_tasks WHERE status IN ('挂起','失败') ORDER BY created_at"
        ).fetchall()
        for task in tasks:
            ok, err = _attempt_once(conn, task)
            if ok:
                conn.execute(
                    """UPDATE sync_tasks SET status='成功', attempts=attempts+1,
                       last_error='', updated_at=? WHERE id=?""",
                    (now_iso(), task["id"]),
                )
                # 补传成功：若该单尚未产生复核差异则直接进入已复核
                disc_n = conn.execute(
                    "SELECT COUNT(*) c FROM discrepancies WHERE order_id=? AND kind<>'ATM离线' AND kind<>'重复回传'",
                    (task["order_id"],),
                ).fetchone()["c"]
                conn.execute(
                    """UPDATE clear_orders SET pending_sync=0, reported=1,
                       synced_at=?, status=CASE WHEN status='清点中'
                           THEN CASE WHEN ?>0 THEN '待复核' ELSE '已复核' END
                           ELSE status END
                       WHERE id=?""",
                    (now_iso(), disc_n, task["order_id"]),
                )
                conn.execute(
                    """INSERT INTO audit_logs(actor_id,branch_id,action,entity,
                          entity_id,detail,created_at)
                       SELECT NULL, branch_id, '补传成功', 'sync_task', id,
                              '离线数据补传核心成功', ? FROM clear_orders WHERE id=?""",
                    (now_iso(), task["order_id"]),
                )
                done += 1
            else:
                conn.execute(
                    """UPDATE sync_tasks SET status='失败', attempts=attempts+1,
                       last_error=?, updated_at=? WHERE id=?""",
                    (err, now_iso(), task["id"]),
                )
    return done


def queue_stats():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) c FROM sync_tasks GROUP BY status"
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}
    finally:
        conn.close()


def _worker(interval: float):
    while not STOP.wait(interval):
        try:
            pump_once()
        except Exception as exc:  # 守护线程不能因偶发错误退出
            print(f"[sync-queue] {exc}")


def start(interval: float = 2.0):
    STOP.clear()
    t = threading.Thread(target=_worker, args=(interval,), daemon=True,
                         name="sync-queue")
    t.start()
    return t


def stop():
    STOP.set()
