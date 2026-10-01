# -*- coding: utf-8 -*-
"""业务服务层：装钞锁定、钞箱封存/签收/领取、清机录入、差异生成、
双人复核、金额修正留痕、网点权限与审计。

所有写操作在 IMMEDIATE 事务中执行（见 db.tx），进程内串行，
配合条件 UPDATE 保证“两人同时领取只一人成功”。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from db import get_conn, tx, now_iso

DUE_DAYS = 2  # 差额处理期限（演示：2 天）


class BusinessError(Exception):
    """业务规则冲突（界面直接展示 message）。"""


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _due_date() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=DUE_DAYS)).date().isoformat()


# ---------------------------------------------------------------- 权限

def can_access_branch(conn, employee_id: str, branch_id: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM employee_branches
           WHERE employee_id=? AND branch_id=?""",
        (employee_id, branch_id),
    ).fetchone()
    return row is not None


def assert_branch(conn, employee_id: str, branch_id: str):
    emp = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not emp:
        raise BusinessError("员工不存在")
    if not can_access_branch(conn, employee_id, branch_id):
        branch = conn.execute("SELECT name FROM branches WHERE id=?", (branch_id,)).fetchone()
        bname = branch["name"] if branch else branch_id
        raise BusinessError(f"员工 {emp['name']} 无权操作网点「{bname}」的数据")
    return emp


def audit(conn, actor_id, action, entity, entity_id="", detail="", branch_id=None):
    conn.execute(
        """INSERT INTO audit_logs(actor_id,branch_id,action,entity,entity_id,detail,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (actor_id, branch_id, action, entity, str(entity_id), detail, now_iso()),
    )


# ---------------------------------------------------------------- 查询

def list_atms_by_branch():
    """ATM 台账：按网点分组。"""
    conn = get_conn()
    try:
        branches = conn.execute("SELECT * FROM branches ORDER BY code").fetchall()
        result = []
        for b in branches:
            atms = conn.execute(
                """SELECT a.*,
                   (SELECT COUNT(*) FROM cassettes c
                      WHERE c.location_type='atm' AND c.location_ref=a.id) cassette_n
                   FROM atms a WHERE a.branch_id=? ORDER BY a.code""",
                (b["id"],),
            ).fetchall()
            result.append((b, atms))
        return result
    finally:
        conn.close()


def get_employee(employee_id):
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------- 装钞计划

def create_plan(atm_id: str, employee_id: str, planned_date: str,
                items: list[dict], note=""):
    """items: [{cassette_id, denom_id, notes}]"""
    with tx() as conn:
        atm = conn.execute("SELECT * FROM atms WHERE id=?", (atm_id,)).fetchone()
        if not atm:
            raise BusinessError("ATM 不存在")
        assert_branch(conn, employee_id, atm["branch_id"])
        plan_id = _uid("lp")
        plan_no = f"ZA{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        conn.execute(
            """INSERT INTO load_plans(id,plan_no,atm_id,branch_id,planned_date,
                  status,created_by,created_at,note)
               VALUES (?,?,?,?,?, '草稿', ?,?,?)""",
            (plan_id, plan_no, atm_id, atm["branch_id"], planned_date,
             employee_id, now_iso(), note),
        )
        for it in items:
            cas = conn.execute("SELECT * FROM cassettes WHERE id=?",
                               (it["cassette_id"],)).fetchone()
            if not cas:
                raise BusinessError("钞箱不存在")
            if it["notes"] > cas["capacity"]:
                raise BusinessError(
                    f"钞箱 {cas['code']} 计划 {it['notes']} 张，超过容量 {cas['capacity']} 张")
            denom = conn.execute("SELECT value_cents FROM denominations WHERE id=?",
                                 (it["denom_id"],)).fetchone()
            conn.execute(
                """INSERT INTO load_plan_items(id,plan_id,cassette_id,denom_id,
                      notes,amount_cents) VALUES (?,?,?,?,?,?)""",
                (_uid("lpi"), plan_id, it["cassette_id"], it["denom_id"],
                 it["notes"], it["notes"] * denom["value_cents"]),
            )
        audit(conn, employee_id, "创建装钞计划", "load_plan", plan_id, plan_no,
              atm["branch_id"])
        return plan_id


def lock_plan(plan_id: str, employee_id: str):
    """装钞前锁定计划，锁定后明细不可改。"""
    with tx() as conn:
        plan = conn.execute("SELECT * FROM load_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise BusinessError("计划不存在")
        assert_branch(conn, employee_id, plan["branch_id"])
        if plan["status"] != "草稿":
            raise BusinessError(f"计划当前为「{plan['status']}」，仅草稿可锁定")
        n = conn.execute("SELECT COUNT(*) c FROM load_plan_items WHERE plan_id=?",
                         (plan_id,)).fetchone()["c"]
        if n == 0:
            raise BusinessError("空计划不能锁定")
        conn.execute(
            "UPDATE load_plans SET status='已锁定', locked_by=?, locked_at=? WHERE id=?",
            (employee_id, now_iso(), plan_id),
        )
        audit(conn, employee_id, "锁定装钞计划", "load_plan", plan_id, plan["plan_no"],
              plan["branch_id"])


def update_plan_item_notes(item_id: str, notes: int, employee_id: str):
    """修改计划数量：草稿才行；已封存钞箱不能直接改数量。"""
    with tx() as conn:
        row = conn.execute(
            """SELECT i.*, p.status, p.branch_id, c.sealed, c.code, c.capacity
               FROM load_plan_items i
               JOIN load_plans p ON p.id=i.plan_id
               JOIN cassettes c ON c.id=i.cassette_id
               WHERE i.id=?""", (item_id,)).fetchone()
        if not row:
            raise BusinessError("明细不存在")
        assert_branch(conn, employee_id, row["branch_id"])
        if row["status"] != "草稿":
            raise BusinessError(f"计划已{row['status']}，明细不可修改")
        if row["sealed"]:
            raise BusinessError(f"钞箱 {row['code']} 已封存，不能直接修改数量")
        if notes > row["capacity"]:
            raise BusinessError(f"超过钞箱容量 {row['capacity']} 张")
        denom = conn.execute("SELECT value_cents FROM denominations WHERE id=?",
                             (row["denom_id"],)).fetchone()
        conn.execute(
            "UPDATE load_plan_items SET notes=?, amount_cents=? WHERE id=?",
            (notes, notes * denom["value_cents"], item_id),
        )
        audit(conn, employee_id, "修改计划数量", "load_plan_item", item_id,
              f"{row['notes']}->{notes} 张", row["branch_id"])


def execute_load(plan_id: str, employee_id: str):
    """执行装钞：计划必须已锁定；钞箱出库（双人签收）后装入 ATM。

    实际出库的双人签收在 leave_branch 完成；本方法要求计划内钞箱均已封存，
    并把它们从网点库/在途落到 ATM 上。
    """
    with tx() as conn:
        plan = conn.execute("SELECT * FROM load_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise BusinessError("计划不存在")
        assert_branch(conn, employee_id, plan["branch_id"])
        if plan["status"] != "已锁定":
            raise BusinessError("仅已锁定计划可以执行装钞")
        atm = conn.execute("SELECT * FROM atms WHERE id=?", (plan["atm_id"],)).fetchone()
        items = conn.execute("SELECT * FROM load_plan_items WHERE plan_id=?",
                             (plan_id,)).fetchall()
        for it in items:
            cas = conn.execute("SELECT * FROM cassettes WHERE id=?",
                               (it["cassette_id"],)).fetchone()
            if not cas["sealed"]:
                raise BusinessError(f"钞箱 {cas['code']} 未封存，不得装柜")
            conn.execute(
                """UPDATE cassettes SET location_type='atm', location_ref=?,
                   status='在柜', updated_at=? WHERE id=?""",
                (atm["id"], now_iso(), cas["id"]),
            )
            conn.execute(
                """INSERT INTO cassette_events(cassette_id,event_type,from_loc,to_loc,
                      seal_no,employee_id,ref_no,detail,created_at)
                   VALUES (?, '装柜', ?, ?, ?, ?, ?, ?, ?)""",
                (cas["id"], cas["location_type"] + ":" + (cas["location_ref"] or ""),
                 "atm:" + atm["id"], cas["seal_no"], employee_id,
                 plan["plan_no"], f"{it['notes']} 张", now_iso()),
            )
        conn.execute("UPDATE load_plans SET status='已完成' WHERE id=?", (plan_id,))
        audit(conn, employee_id, "执行装钞", "load_plan", plan_id, plan["plan_no"],
              plan["branch_id"])


# ---------------------------------------------------------------- 钞箱

def seal_cassette(cassette_id: str, seal_no: str, employee_id: str):
    """封存钞箱并写封签号；封存后数量不可直接改。"""
    with tx() as conn:
        cas = conn.execute("SELECT * FROM cassettes WHERE id=?", (cassette_id,)).fetchone()
        if not cas:
            raise BusinessError("钞箱不存在")
        branch_id = cas["location_ref"] if cas["location_type"] == "branch" else None
        if branch_id:
            assert_branch(conn, employee_id, branch_id)
        if cas["sealed"]:
            raise BusinessError("钞箱已封存，如需换签请先拆封登记")
        dup = conn.execute("SELECT 1 FROM cassettes WHERE seal_no=? AND id<>?",
                           (seal_no, cassette_id)).fetchone()
        if dup:
            raise BusinessError("封签号已被使用")
        conn.execute(
            "UPDATE cassettes SET sealed=1, seal_no=?, updated_at=? WHERE id=?",
            (seal_no, now_iso(), cassette_id),
        )
        conn.execute(
            """INSERT INTO cassette_events(cassette_id,event_type,to_loc,seal_no,
                  seal_intact,employee_id,detail,created_at)
               VALUES (?, '封存', ?, ?, 1, ?, '加贴封签', ?)""",
            (cassette_id, "branch:" + (cas["location_ref"] or ""), seal_no,
             employee_id, now_iso()),
        )
        audit(conn, employee_id, "封存钞箱", "cassette", cassette_id,
              f"封签 {seal_no}", branch_id)


def leave_branch(cassette_id: str, staff1_id: str, staff2_id: str):
    """钞箱离开网点：必须两名【不同】员工签收；并发只允许一组成功。"""
    with tx() as conn:
        cas = conn.execute("SELECT * FROM cassettes WHERE id=?", (cassette_id,)).fetchone()
        if not cas:
            raise BusinessError("钞箱不存在")
        if cas["location_type"] != "branch":
            raise BusinessError("钞箱不在网点，无法办理出库")
        if not cas["sealed"]:
            raise BusinessError("未封存钞箱不得出库")
        if staff1_id == staff2_id:
            raise BusinessError("离开网点必须由两名不同员工签收")
        branch_id = cas["location_ref"]
        e1 = assert_branch(conn, staff1_id, branch_id)
        e2 = assert_branch(conn, staff2_id, branch_id)
        # 条件更新：status 仍处于可出库状态才抢占成功
        cur = conn.execute(
            """UPDATE cassettes SET status='在途', location_type='transit',
                  location_ref=NULL, updated_at=?
               WHERE id=? AND sealed=1 AND status<>'在途'""",
            (now_iso(), cassette_id),
        )
        if cur.rowcount == 0:
            raise BusinessError(f"钞箱 {cas['code']} 已被他人领取，本次签收失败")
        conn.execute(
            """INSERT INTO cassette_events(cassette_id,event_type,from_loc,to_loc,
                  seal_no,seal_intact,employee_id,employee2_id,detail,created_at)
               VALUES (?, '出库', 'branch', 'transit', ?, 1, ?, ?,
                   '双人签收出库', ?)""",
            (cassette_id, cas["seal_no"], staff1_id, staff2_id, now_iso()),
        )
        audit(conn, staff1_id, "钞箱出库(双人签收)", "cassette", cassette_id,
              f"{e1['name']} + {e2['name']}", branch_id)


def plan_seal_and_leave(plan_id: str, staff1_id: str, staff2_id: str):
    """页面对外动作：计划必须已锁定；将计划内尚在网点库的钞箱统一封存并双人签收出库。

    已封存的钞箱直接复用原封签；同事务保证双人签收的原子性与并发安全。
    """
    with tx() as conn:
        plan = conn.execute("SELECT * FROM load_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise BusinessError("计划不存在")
        assert_branch(conn, staff1_id, plan["branch_id"])
        assert_branch(conn, staff2_id, plan["branch_id"])
        if staff1_id == staff2_id:
            raise BusinessError("离开网点必须由两名不同员工签收")
        if plan["status"] != "已锁定":
            raise BusinessError("计划锁定后才能办理封存出库")
        items = conn.execute(
            """SELECT i.*, c.* FROM load_plan_items i
               JOIN cassettes c ON c.id=i.cassette_id WHERE i.plan_id=?""",
            (plan_id,)).fetchall()
        moved = []
        for cas in items:
            if cas["location_type"] != "branch":
                continue  # 已出库/在柜则跳过
            seal_no = cas["seal_no"]
            if not cas["sealed"]:
                seal_no = f"SF-{cas['code'][2:]}-{uuid.uuid4().hex[:4].upper()}"
                conn.execute(
                    "UPDATE cassettes SET sealed=1, seal_no=? WHERE id=?",
                    (seal_no, cas["id"]))
                conn.execute(
                    """INSERT INTO cassette_events(cassette_id,event_type,to_loc,seal_no,
                          seal_intact,employee_id,detail,created_at)
                       VALUES (?, '封存', 'branch', ?, 1, ?, '装钞前封存', ?)""",
                    (cas["id"], seal_no, staff1_id, now_iso()))
            cur = conn.execute(
                """UPDATE cassettes SET status='在途', location_type='transit',
                      location_ref=NULL, updated_at=?
                   WHERE id=? AND sealed=1 AND status<>'在途'""",
                (now_iso(), cas["id"]))
            if cur.rowcount == 0:
                raise BusinessError(f"钞箱 {cas['code']} 已被他人领取，本次签收失败")
            conn.execute(
                """INSERT INTO cassette_events(cassette_id,event_type,from_loc,to_loc,
                      seal_no,seal_intact,employee_id,employee2_id,detail,created_at)
                   VALUES (?, '出库', 'branch', 'transit', ?, 1, ?, ?,
                       '双人签收出库', ?)""",
                (cas["id"], seal_no, staff1_id, staff2_id, now_iso()))
            moved.append(cas["code"])
        audit(conn, staff1_id, "计划封存出库(双人签收)", "load_plan", plan_id,
              f"{plan['plan_no']}：{('、'.join(moved)) or '无待出库钞箱'}",
              plan["branch_id"])
        return moved


def arrive_branch(cassette_id: str, branch_id: str, receiver_id: str):
    """钞箱到达网点入库（单人签收，轨迹连续）。"""
    with tx() as conn:
        cas = conn.execute("SELECT * FROM cassettes WHERE id=?", (cassette_id,)).fetchone()
        if not cas:
            raise BusinessError("钞箱不存在")
        assert_branch(conn, receiver_id, branch_id)
        if cas["location_type"] != "transit":
            raise BusinessError("钞箱不在途，无法办理入库")
        conn.execute(
            """UPDATE cassettes SET location_type='branch', location_ref=?,
               status='待清分', updated_at=? WHERE id=?""",
            (branch_id, now_iso(), cassette_id),
        )
        conn.execute(
            """INSERT INTO cassette_events(cassette_id,event_type,from_loc,to_loc,
                  seal_no,employee_id,detail,created_at)
               VALUES (?, '签收', 'transit', 'branch', ?, ?, '到达网点入库', ?)""",
            (cassette_id, cas["seal_no"], receiver_id, now_iso()),
        )
        audit(conn, receiver_id, "钞箱入库签收", "cassette", cassette_id,
              cas["code"], branch_id)


def cassette_trace(cassette_id: str):
    conn = get_conn()
    try:
        cas = conn.execute("SELECT * FROM cassettes WHERE id=?",
                           (cassette_id,)).fetchone()
        events = conn.execute(
            """SELECT e.*, emp.name employee_name, emp2.name employee2_name
               FROM cassette_events e
               LEFT JOIN employees emp ON emp.id=e.employee_id
               LEFT JOIN employees emp2 ON emp2.id=e.employee2_id
               WHERE e.cassette_id=? ORDER BY e.id""",
            (cassette_id,),
        ).fetchall()
        return cas, events
    finally:
        conn.close()


# ---------------------------------------------------------------- 清机

def create_clear_order(atm_id: str, employee_id: str, plan_id=None, remark=""):
    with tx() as conn:
        atm = conn.execute("SELECT * FROM atms WHERE id=?", (atm_id,)).fetchone()
        if not atm:
            raise BusinessError("ATM 不存在")
        assert_branch(conn, employee_id, atm["branch_id"])
        if not plan_id:
            row = conn.execute(
                """SELECT id FROM load_plans WHERE atm_id=? AND status='已完成'
                   ORDER BY created_at DESC LIMIT 1""", (atm_id,)).fetchone()
            plan_id = row["id"] if row else None
        expected = {}
        if plan_id:
            for it in conn.execute(
                "SELECT denom_id, SUM(notes) notes FROM load_plan_items WHERE plan_id=? GROUP BY denom_id",
                (plan_id,),
            ):
                expected[it["denom_id"]] = it["notes"]
        order_id = _uid("co")
        order_no = f"QJ{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        expected_total = 0
        conn.execute(
            """INSERT INTO clear_orders(id,order_no,atm_id,branch_id,plan_id,status,
                  created_by,created_at,remark) VALUES (?,?,?,?,?, '清点中', ?,?,?)""",
            (order_id, order_no, atm_id, atm["branch_id"], plan_id,
             employee_id, now_iso(), remark),
        )
        for d in conn.execute("SELECT * FROM denominations ORDER BY sort_seq"):
            exp = expected.get(d["id"], 0)
            conn.execute(
                """INSERT INTO clear_counts(order_id,denom_id,expected_notes)
                   VALUES (?,?,?)""",
                (order_id, d["id"], exp),
            )
            expected_total += exp * d["value_cents"]
        conn.execute("UPDATE clear_orders SET expected_cents=? WHERE id=?",
                     (expected_total, order_id))
        audit(conn, employee_id, "创建清机单", "clear_order", order_id, order_no,
              atm["branch_id"])
        return order_id


def _add_discrepancy(conn, order, kind, amount_cents, description, employee_id):
    did = _uid("dc")
    code = f"CY{datetime.now(timezone.utc).strftime('%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    conn.execute(
        """INSERT INTO discrepancies(id,code,order_id,cassette_id,kind,amount_cents,
              due_date,status,description,created_by,created_at)
           VALUES (?,?,?,?,?,?,?, '待复核', ?,?,?)""",
        (did, code, order["id"], None, kind, amount_cents, _due_date(),
         description, employee_id, now_iso()),
    )
    return did


def submit_counts(order_id: str, counts: list[dict], employee_id: str,
                  submit_ref: str = "", broken_seals: list[str] | None = None):
    """按券别录入实点（含残钞）并回传。

    - 同一 submit_ref 重复回传 → 生成「重复回传」差异并拒绝；
    - ATM 离线 → 数据本地落账，生成「ATM离线」差异并入补传队列。
    counts: [{denom_id, counted_notes, residual_notes}]
    broken_seals: 封签破损的钞箱编号列表
    """
    broken_seals = broken_seals or []
    with tx() as conn:
        order = conn.execute("SELECT * FROM clear_orders WHERE id=?",
                             (order_id,)).fetchone()
        if not order:
            raise BusinessError("清机单不存在")
        assert_branch(conn, employee_id, order["branch_id"])
        if order["status"] in ("已关闭",):
            raise BusinessError("清机单已关闭，不能再回传")

        # 重复回传判定：同一回传引用且该单已接收过（在线已回传或离线已暂存）
        if submit_ref:
            hit = conn.execute(
                "SELECT 1 FROM clear_orders WHERE id=? AND submit_ref=? AND submit_ref<>''",
                (order_id, submit_ref),
            ).fetchone()
            if hit:
                _add_discrepancy(conn, order, "重复回传", 0,
                                 f"回传批次 {submit_ref} 重复提交，已拦截", employee_id)
                audit(conn, employee_id, "重复回传拦截", "clear_order", order_id,
                      submit_ref, order["branch_id"])
                conn.commit()  # 差异记录需保留，先落账再报错
                raise BusinessError("重复回传：该批次已成功提交过")

        total = residual_total = 0
        for c in counts:
            denom = conn.execute("SELECT * FROM denominations WHERE id=?",
                                 (c["denom_id"],)).fetchone()
            counted = int(c.get("counted_notes", 0))
            residual = int(c.get("residual_notes", 0))
            if counted < 0 or residual < 0 or residual > counted:
                raise BusinessError(f"{denom['label']} 录入数量不合法")
            amount = counted * denom["value_cents"]
            total += amount
            residual_total += residual * denom["value_cents"]
            conn.execute(
                """UPDATE clear_counts SET counted_notes=?, residual_notes=?, amount_cents=?
                   WHERE order_id=? AND denom_id=?""",
                (counted, residual, amount, order_id, c["denom_id"]),
            )

        diff = total - order["expected_cents"]
        conn.execute(
            """UPDATE clear_orders SET counted_cents=?, residual_cents=?, diff_cents=?
               WHERE id=?""",
            (total, residual_total, diff, order_id),
        )

        new_discs = []
        if diff < 0:
            new_discs.append(_add_discrepancy(
                conn, order, "短款", -diff,
                f"实点比账面少 {-diff/100:.2f} 元", employee_id))
        elif diff > 0:
            new_discs.append(_add_discrepancy(
                conn, order, "长款", diff,
                f"实点比账面多 {diff/100:.2f} 元", employee_id))
        if residual_total > 0:
            new_discs.append(_add_discrepancy(
                conn, order, "残钞", residual_total,
                f"清分残钞合计 {residual_total/100:.2f} 元", employee_id))
        for code in broken_seals:
            cas = conn.execute("SELECT id FROM cassettes WHERE code=?", (code,)).fetchone()
            did = _add_discrepancy(
                conn, order, "封签破损", 0,
                f"钞箱 {code} 封签破损，需核验责任", employee_id)
            if cas:
                conn.execute("UPDATE discrepancies SET cassette_id=? WHERE id=?",
                             (cas["id"], did))
                conn.execute(
                    """INSERT INTO cassette_events(cassette_id,event_type,seal_no,
                          seal_intact,employee_id,ref_no,detail,created_at)
                       VALUES (?, '拆封', ?, 0, ?, ?, '清机发现封签破损', ?)""",
                    (cas["id"], code, employee_id, order["order_no"], now_iso()),
                )
            new_discs.append(did)

        atm = conn.execute("SELECT * FROM atms WHERE id=?", (order["atm_id"],)).fetchone()
        if not atm["online"]:
            # 离线：挂起补传
            new_discs.append(_add_discrepancy(
                conn, order, "ATM离线", 0,
                f"ATM {atm['code']} 离线，清点数据已本地暂存并入补传队列", employee_id))
            task_id = _uid("st")
            payload = json.dumps({
                "order_no": order["order_no"], "total_cents": total,
                "diff_cents": diff, "submit_ref": submit_ref}, ensure_ascii=False)
            conn.execute(
                """INSERT INTO sync_tasks(id,order_id,payload,status,attempts,
                      created_at,updated_at) VALUES (?,?,?, '挂起', 0,?,?)""",
                (task_id, order_id, payload, now_iso(), now_iso()),
            )
            conn.execute(
                "UPDATE clear_orders SET pending_sync=1, submit_ref=COALESCE(NULLIF(?, ''), submit_ref) WHERE id=?",
                (submit_ref, order_id),
            )
            audit(conn, employee_id, "清机录入(离线挂起)", "clear_order", order_id,
                  f"差异 {len(new_discs)} 条", order["branch_id"])
            return {"offline": True, "discrepancies": new_discs, "diff_cents": diff}

        conn.execute(
            """UPDATE clear_orders SET reported=1, synced_at=?, status=?,
                  submit_ref=COALESCE(NULLIF(?, ''), submit_ref) WHERE id=?""",
            (now_iso(), "待复核" if new_discs else "已复核", submit_ref, order_id),
        )
        audit(conn, employee_id, "清机录入并回传", "clear_order", order_id,
              f"差异 {len(new_discs)} 条，差额 {diff/100:.2f} 元", order["branch_id"])
        return {"offline": False, "discrepancies": new_discs, "diff_cents": diff}


def correct_amount(order_id: str, denom_id: str, new_counted_notes: int,
                   reason: str, operator_id: str, approver_id: str):
    """金额修正：保留原值、原因、审批人；审批人不能是经办人本人。"""
    with tx() as conn:
        order = conn.execute("SELECT * FROM clear_orders WHERE id=?",
                             (order_id,)).fetchone()
        if not order:
            raise BusinessError("清机单不存在")
        assert_branch(conn, operator_id, order["branch_id"])
        assert_branch(conn, approver_id, order["branch_id"])
        if operator_id == approver_id:
            raise BusinessError("审批人不能是经办人本人")
        if not reason.strip():
            raise BusinessError("修正必须填写原因")
        if order["status"] == "已关闭":
            raise BusinessError("清机单已关闭，不能修正")
        cc = conn.execute("SELECT * FROM clear_counts WHERE order_id=? AND denom_id=?",
                          (order_id, denom_id)).fetchone()
        denom = conn.execute("SELECT * FROM denominations WHERE id=?",
                             (denom_id,)).fetchone()
        old_cents = cc["amount_cents"]
        new_cents = new_counted_notes * denom["value_cents"]
        if old_cents == new_cents:
            raise BusinessError("修正后金额与原值一致，无需修正")
        conn.execute(
            """INSERT INTO amount_corrections(order_id,denom_id,old_cents,new_cents,
                  reason,approver_id,operator_id,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (order_id, denom_id, old_cents, new_cents, reason,
             approver_id, operator_id, now_iso()),
        )
        conn.execute(
            """UPDATE clear_counts SET counted_notes=?, amount_cents=?
               WHERE order_id=? AND denom_id=?""",
            (new_counted_notes, new_cents, order_id, denom_id),
        )
        row = conn.execute(
            """SELECT SUM(amount_cents) t,
                      SUM(residual_notes * (SELECT value_cents FROM denominations d
                                             WHERE d.id=clear_counts.denom_id)) r
               FROM clear_counts WHERE order_id=?""", (order_id,)).fetchone()
        total, residual = row["t"] or 0, row["r"] or 0
        new_diff = total - order["expected_cents"]
        conn.execute(
            "UPDATE clear_orders SET counted_cents=?, residual_cents=?, diff_cents=? WHERE id=?",
            (total, residual, new_diff, order_id),
        )
        # 同步调整尚未处置的短/长款差异金额；完全消除时保留记录置 0 供复核确认
        disc = conn.execute(
            """SELECT * FROM discrepancies WHERE order_id=? AND kind IN ('短款','长款')
               AND status IN ('待复核','复核驳回') ORDER BY created_at DESC LIMIT 1""",
            (order_id,)).fetchone()
        if disc:
            if new_diff == 0:
                conn.execute(
                    "UPDATE discrepancies SET amount_cents=0, description=? WHERE id=?",
                    (disc["description"] + "；经金额修正后差额已消除", disc["id"]))
            else:
                kind = "短款" if new_diff < 0 else "长款"
                conn.execute(
                    "UPDATE discrepancies SET kind=?, amount_cents=?, description=? WHERE id=?",
                    (kind, abs(new_diff),
                     f"修正后实点与账面{kind} {abs(new_diff)/100:.2f} 元", disc["id"]))
        audit(conn, operator_id, "金额修正", "clear_order", order_id,
              f"{denom['label']}: {old_cents/100:.2f}->{new_cents/100:.2f}；"
              f"原因：{reason}；审批人：{approver_id}", order["branch_id"])


# ---------------------------------------------------------------- 复核与关闭

def review_discrepancy(discrepancy_id: str, reviewer1_id: str, reviewer2_id: str,
                       result: str, comment: str):
    """双人复核：两名不同员工，且都有该网点权限。"""
    with tx() as conn:
        d = conn.execute("SELECT * FROM discrepancies WHERE id=?",
                         (discrepancy_id,)).fetchone()
        if not d:
            raise BusinessError("差异记录不存在")
        order = conn.execute("SELECT * FROM clear_orders WHERE id=?",
                             (d["order_id"],)).fetchone()
        assert_branch(conn, reviewer1_id, order["branch_id"])
        assert_branch(conn, reviewer2_id, order["branch_id"])
        if reviewer1_id == reviewer2_id:
            raise BusinessError("复核必须由两名不同员工完成")
        if result not in ("通过", "驳回"):
            raise BusinessError("复核结论只能是通过/驳回")
        if d["status"] not in ("待复核", "复核驳回"):
            raise BusinessError(f"差异当前为「{d['status']}」，无需重复复核")
        conn.execute(
            """INSERT INTO reviews(discrepancy_id,reviewer1_id,reviewer2_id,result,
                  comment,created_at) VALUES (?,?,?,?,?,?)""",
            (discrepancy_id, reviewer1_id, reviewer2_id, result, comment, now_iso()),
        )
        new_status = "复核通过" if result == "通过" else "复核驳回"
        conn.execute(
            "UPDATE discrepancies SET status=?, resolved_by=?, resolved_at=?, resolution=? WHERE id=?",
            (new_status, reviewer1_id, now_iso(), comment, discrepancy_id),
        )
        audit(conn, reviewer1_id, f"差异复核{result}", "discrepancy",
              discrepancy_id,
              f"{d['code']}；复核人1 {reviewer1_id} / 复核人2 {reviewer2_id}",
              order["branch_id"])


def resolve_discrepancy(discrepancy_id: str, employee_id: str, resolution: str):
    """复核通过后的处置登记。"""
    with tx() as conn:
        d = conn.execute("SELECT * FROM discrepancies WHERE id=?",
                         (discrepancy_id,)).fetchone()
        order = conn.execute("SELECT * FROM clear_orders WHERE id=?",
                             (d["order_id"],)).fetchone()
        assert_branch(conn, employee_id, order["branch_id"])
        if d["status"] != "复核通过":
            raise BusinessError("仅复核通过的差异可以处置")
        conn.execute(
            "UPDATE discrepancies SET status='已处置', resolution=? WHERE id=?",
            ((d["resolution"] or "") + " | 处置：" + resolution, discrepancy_id),
        )
        audit(conn, employee_id, "差异处置", "discrepancy", discrepancy_id,
              resolution, order["branch_id"])


def close_order(order_id: str, employee_id: str):
    """关闭清机单：存在未完成复核的差异时禁止关闭。"""
    with tx() as conn:
        order = conn.execute("SELECT * FROM clear_orders WHERE id=?",
                             (order_id,)).fetchone()
        if not order:
            raise BusinessError("清机单不存在")
        assert_branch(conn, employee_id, order["branch_id"])
        if order["status"] == "已关闭":
            raise BusinessError("清机单已关闭")
        pending = conn.execute(
            "SELECT COUNT(*) c FROM discrepancies WHERE order_id=? AND status IN ('待复核','复核驳回')",
            (order_id,),
        ).fetchone()["c"]
        if pending:
            raise BusinessError(f"还有 {pending} 条差异未复核通过，禁止关闭清机单")
        if order["pending_sync"]:
            raise BusinessError("尚有补传任务未完成，不能关闭")
        conn.execute(
            "UPDATE clear_orders SET status='已关闭', closed_at=? WHERE id=?",
            (now_iso(), order_id),
        )
        audit(conn, employee_id, "关闭清机单", "clear_order", order_id,
              order["order_no"], order["branch_id"])


# ---------------------------------------------------------------- ATM 在线状态

def set_atm_online(atm_id: str, online: bool, employee_id: str | None = None):
    with tx() as conn:
        conn.execute("UPDATE atms SET online=? WHERE id=?", (1 if online else 0, atm_id))
        audit(conn, employee_id, "ATM 上线" if online else "ATM 离线",
              "atm", atm_id, "")
