# -*- coding: utf-8 -*-
"""演示样例数据：装钞、清机、差额处置的完整生命周期 + 进行中业务。

幂等：已有员工数据时跳过。账户/卡号/金额均为演示数据。
"""
from __future__ import annotations

from db import get_conn, now_iso
import services as svc


def _insert_master(conn):
    branches = [
        ("b1", "城东支行", "B001"),
        ("b2", "城西支行", "B002"),
        ("b3", "滨江支行", "B003"),
    ]
    conn.executemany(
        "INSERT INTO branches(id,name,code,created_at) VALUES (?,?,?,?)",
        [(b[0], b[1], b[2], now_iso()) for b in branches],
    )
    emps = [
        ("e1", "王建国", "E1001", "主管"),
        ("e2", "李晓梅", "E1002", "清分员"),
        ("e3", "张志强", "E1003", "清分员"),
        ("e4", "陈静", "E1004", "主管"),
        ("e5", "周伟", "E1005", "清分员"),
        ("e6", "赵敏", "E1006", "主管"),
        ("e7", "孙丽", "E1007", "清分员"),
    ]
    conn.executemany(
        "INSERT INTO employees(id,name,staff_no,role,created_at) VALUES (?,?,?,?,?)",
        [(e[0], e[1], e[2], e[3], now_iso()) for e in emps],
    )
    emp_branch = {
        "e1": ["b1", "b2"], "e2": ["b1"], "e3": ["b1"],
        "e4": ["b2"], "e5": ["b2"], "e6": ["b3"], "e7": ["b3"],
    }
    conn.executemany(
        "INSERT INTO employee_branches(employee_id,branch_id) VALUES (?,?)",
        [(eid, bid) for eid, bids in emp_branch.items() for bid in bids],
    )
    atms = [
        ("a1", "ATM-CD-01", "广电", "GT-2050", "b1", 1),
        ("a2", "ATM-CD-02", "日立", "HT-2845", "b1", 0),   # 离线
        ("a3", "ATM-CX-01", "怡化", "YH-6040", "b2", 1),
        ("a4", "ATM-BJ-01", "NCR", "NCR-6622", "b3", 1),
    ]
    conn.executemany(
        "INSERT INTO atms(id,code,brand,model,branch_id,online,created_at) VALUES (?,?,?,?,?,?,?)",
        [(a[0], a[1], a[2], a[3], a[4], a[5], now_iso()) for a in atms],
    )

    def cas(cid, code, denom, loc_type, loc_ref, status, sealed=0, seal=None, cap=2500):
        conn.execute(
            """INSERT INTO cassettes(id,code,denom_id,capacity,seal_no,sealed,
                  location_type,location_ref,status,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cid, code, denom, cap, seal, sealed, loc_type, loc_ref, status, now_iso()),
        )

    # A1 装钞用箱（初始在城东支行库内，未封存）
    for i, d in enumerate(["d100", "d50", "d20", "d10"], start=1):
        cas(f"c200{i}", f"C-200{i}", d, "branch", "b1", "空闲")
    # A2 离线 ATM 装钞用箱
    for i, d in enumerate(["d100", "d50", "d20", "d10"], start=1):
        cas(f"c210{i}", f"C-210{i}", d, "branch", "b1", "空闲")
    # A3 离行式 ATM 用箱（计划锁定后双人签收出库在途）
    for i, d in enumerate(["d100", "d50", "d20", "d10"], start=1):
        cas(f"c300{i}", f"C-300{i}", d, "branch", "b2", "待用")
    # B1 库内备用空箱
    for i, d in enumerate(["d100", "d50", "d20", "d10"], start=1):
        cas(f"c400{i}", f"C-400{i}", d, "branch", "b1", "空闲")
    # A4 装钞用箱
    for i, d in enumerate(["d100", "d50", "d20", "d10"], start=1):
        cas(f"c410{i}", f"C-410{i}", d, "branch", "b3", "空闲")


def _load_atm(atm_id, operator, items, leave_staff=None, lock_only=False):
    """items: [(cassette_id, denom_id, notes)]

    leave_staff=(签收人1, 签收人2, 入库网点)：模拟钞箱离行走在途。
    lock_only=True：仅锁定计划，封存出库后停在在途，等待装柜。
    """
    pid = svc.create_plan(atm_id, operator, "2026-10-01",
                          [{"cassette_id": c, "denom_id": d, "notes": n}
                           for c, d, n in items])
    for c, _, _ in items:
        seal_no = f"SF-{c[1:]}"  # 由钞箱编号生成演示封签号
        svc.seal_cassette(c, seal_no, operator)
    if leave_staff:
        for c, _, _ in items:
            svc.leave_branch(c, leave_staff[0], leave_staff[1])
    if lock_only:
        svc.lock_plan(pid, operator)
        return pid
    if leave_staff:
        for c, _, _ in items:
            svc.arrive_branch(c, leave_staff[2], leave_staff[0])
    svc.lock_plan(pid, operator)
    svc.execute_load(pid, operator)
    return pid


def seed():
    conn = get_conn()
    try:
        if conn.execute("SELECT COUNT(*) c FROM employees").fetchone()["c"]:
            return False
        _insert_master(conn)
        conn.commit()
    finally:
        conn.close()

    # 1) A1 完整生命周期：装钞 → 清机录入(短款+残钞+封签破损) → 金额修正 →
    #    双人复核 → 处置 → 关闭
    _load_atm("a1", "e2", [
        ("c2001", "d100", 1500), ("c2002", "d50", 1000),
        ("c2003", "d20", 1000), ("c2004", "d10", 500),
    ])
    o1 = svc.create_clear_order(
        "a1", "e3",
        remark="吞卡一张（演示卡号 6217 0000 0000 8888），已剪角登记入吞卡登记簿")
    svc.submit_counts(o1, [
        {"denom_id": "d100", "counted_notes": 1480, "residual_notes": 10},
        {"denom_id": "d50", "counted_notes": 1000, "residual_notes": 0},
        {"denom_id": "d20", "counted_notes": 1000, "residual_notes": 0},
        {"denom_id": "d10", "counted_notes": 500, "residual_notes": 0},
    ], "e3", submit_ref="BATCH-A1-001", broken_seals=["C-2001"])
    # 金额修正：100 元 1480 -> 1490（录入串把），主管审批
    svc.correct_amount(o1, "d100", 1490,
                       "清点时两捆相邻券别串把，复盘后更正为 1490 张",
                       operator_id="e3", approver_id="e1")
    discs = _discs_of(o1)
    for did in discs:
        svc.review_discrepancy(did, "e2", "e3", "通过", "双人复盘一致，封签破损已登记责任人")
        svc.resolve_discrepancy(did, "e1", "短款挂账追赔/残钞上缴/封签破损追责，附件齐全")
    svc.close_order(o1, "e1")

    # 2) A2 离线清机：数据暂存 + 补传队列挂起（恢复在线后自动补传）
    _load_atm("a2", "e2", [
        ("c2101", "d100", 1000), ("c2102", "d50", 800),
        ("c2103", "d20", 500), ("c2104", "d10", 500),
    ])
    o2 = svc.create_clear_order("a2", "e2")
    svc.submit_counts(o2, [
        {"denom_id": "d100", "counted_notes": 1000, "residual_notes": 0},
        {"denom_id": "d50", "counted_notes": 800, "residual_notes": 0},
        {"denom_id": "d20", "counted_notes": 500, "residual_notes": 0},
        {"denom_id": "d10", "counted_notes": 500, "residual_notes": 0},
    ], "e2", submit_ref="BATCH-A2-001")

    # 3) A3 装钞计划已锁定，钞箱封存出库在途（双人签收），尚未装柜
    _load_atm("a3", "e1", [
        ("c3001", "d100", 1200), ("c3002", "d50", 1200),
        ("c3003", "d20", 800), ("c3004", "d10", 400),
    ], leave_staff=("e1", "e4", "b2"), lock_only=True)

    # 4) A4 长款差异待复核
    _load_atm("a4", "e6", [
        ("c4101", "d100", 900), ("c4102", "d50", 900),
        ("c4103", "d20", 600), ("c4104", "d10", 300),
    ], leave_staff=("e6", "e7", "b3"))
    o4 = svc.create_clear_order("a4", "e7")
    svc.submit_counts(o4, [
        {"denom_id": "d100", "counted_notes": 900, "residual_notes": 0},
        {"denom_id": "d50", "counted_notes": 902, "residual_notes": 0},
        {"denom_id": "d20", "counted_notes": 600, "residual_notes": 0},
        {"denom_id": "d10", "counted_notes": 300, "residual_notes": 0},
    ], "e7", submit_ref="BATCH-A4-001")
    return True


def _discs_of(order_id):
    conn = get_conn()
    try:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM discrepancies WHERE order_id=? ORDER BY created_at",
            (order_id,))]
    finally:
        conn.close()
