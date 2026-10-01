# -*- coding: utf-8 -*-
"""服务端渲染页面。所有用户内容经 html.escape；金额列右对齐。"""
from __future__ import annotations

import html
from urllib.parse import quote

from db import get_conn

fmt_yuan = lambda c: f"¥{int(c or 0)/100:,.2f}"  # 由 web.py 注入带符号版本

KIND_BADGE = {
    "短款": "kind-short", "长款": "kind-long", "残钞": "kind-residual",
    "封签破损": "kind-seal", "ATM离线": "kind-offline", "重复回传": "kind-dup",
}
STATUS_BADGE = {
    "草稿": "b-gray", "已锁定": "b-gold", "装钞中": "b-gold", "已完成": "b-green",
    "已取消": "b-gray", "清点中": "b-gold", "待复核": "b-red", "已复核": "b-green",
    "已关闭": "b-gray", "待处置": "b-gold", "复核通过": "b-green", "复核驳回": "b-red",
    "已处置": "b-green", "挂起": "b-gold", "失败": "b-red", "成功": "b-green", "待发": "b-gold",
}


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def money(cents, signed=False) -> str:
    try:
        cents = int(cents or 0)
    except (TypeError, ValueError):
        cents = 0
    sign = "-" if cents < 0 else ("+" if signed and cents > 0 else "")
    return f"{sign}¥{abs(cents)/100:,.2f}"


def badge(text, cls=""):
    t = esc(text)
    return f'<span class="badge {cls or STATUS_BADGE.get(str(text), "b-gray")}">{t}</span>'


# ---------------------------------------------------------------- 布局

NAV = [
    ("/atms", "ATM 台账"), ("/plans", "装钞计划"), ("/clear", "清机录入"),
    ("/trace", "钞箱轨迹"), ("/review", "差额复核"), ("/audit", "审计查询"),
]


def layout(title, active, emp, body, extra_head=""):
    conn = get_conn()
    try:
        emp_row = conn.execute("SELECT * FROM employees WHERE id=?", (emp,)).fetchone()
        branches = conn.execute(
            """SELECT b.* FROM branches b JOIN employee_branches eb ON eb.branch_id=b.id
               WHERE eb.employee_id=? ORDER BY b.code""", (emp,)).fetchall()
        bnames = "、".join(b["name"] for b in branches)
    finally:
        conn.close()
    nav_html = "".join(
        f'<a class="nav-item {"active" if path == active else ""}" href="{path}">{name}</a>'
        for path, name in NAV)
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · ATM 现金清分平台</title>
<link rel="stylesheet" href="/static/app.css">
{extra_head}
</head><body>
<header class="topbar">
  <div class="brand"><span class="brand-mark">❖</span> 现金清分与钞箱交接平台
    <small>Cash Sorting &amp; Cassette Handover</small></div>
  <nav>{nav_html}</nav>
  <div class="userbox">
    <span class="user-name">{esc(emp_row["name"])}</span>
    <span class="user-role">{esc(emp_row["role"])} · {esc(bnames)}</span>
    <button class="btn-mini" onclick="api('/api/logout',{{}},()=>location.href='/')">退出</button>
  </div>
</header>
<main class="container">{body}</main>
<footer class="foot">演示系统 · 账户、卡号、金额均为模拟数据 · SQLite + 本地附件 + 进程内补传队列</footer>
<script src="/static/app.js"></script>
</body></html>"""


def page_title(zh, sub=""):
    return f'<div class="page-head"><h1>{zh}</h1><p>{sub}</p></div>'


def flash():
    return '<div id="flash" class="flash" style="display:none"></div>'


def stat_chips(items):
    return '<div class="chips">' + "".join(
        f'<div class="chip"><b>{v if isinstance(v, str) else v}</b><span>{esc(k)}</span></div>'
        for k, v in items) + "</div>"


# ---------------------------------------------------------------- 登录

def page_login():
    conn = get_conn()
    try:
        emps = conn.execute(
            """SELECT e.*, GROUP_CONCAT(b.name,'、') bnames FROM employees e
               LEFT JOIN employee_branches eb ON eb.employee_id=e.id
               LEFT JOIN branches b ON b.id=eb.branch_id
               GROUP BY e.id ORDER BY e.staff_no""").fetchall()
    finally:
        conn.close()
    cards = "".join(
        f"""<button class="login-card" onclick="login('{e["id"]}')">
          <b>{esc(e["name"])}</b><span>{esc(e["staff_no"])} · {esc(e["role"])}</span>
          <small>{esc(e["bnames"] or "未分配网点")}</small></button>"""
        for e in emps)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · ATM 现金清分平台</title>
<link rel="stylesheet" href="/static/app.css"></head>
<body class="login-body">
<div class="login-wrap">
  <div class="login-brand"><span class="brand-mark">❖</span> 现金清分与钞箱交接平台</div>
  <p class="login-tip">演示环境请选择一名员工身份登录（网点权限随之生效）</p>
  <div class="login-grid">{cards}</div>
</div>
<script src="/static/app.js"></script></body></html>"""


# ---------------------------------------------------------------- ATM 台账

def _accessible_branch_ids(conn, emp):
    return [r["branch_id"] for r in conn.execute(
        "SELECT branch_id FROM employee_branches WHERE employee_id=?", (emp,))]


def page_atms(emp):
    conn = get_conn()
    try:
        bids = _accessible_branch_ids(conn, emp)
        branches = [conn.execute("SELECT * FROM branches WHERE id=?", (b,)).fetchone()
                    for b in bids]
        groups = []
        total_atm = total_cas = 0
        for b in branches:
            atms = conn.execute("SELECT * FROM atms WHERE branch_id=? ORDER BY code",
                                (b["id"],)).fetchall()
            cards = []
            for a in atms:
                total_atm += 1
                cass = conn.execute(
                    """SELECT c.*, d.label denom_label, d.value_cents
                       FROM cassettes c LEFT JOIN denominations d ON d.id=c.denom_id
                       WHERE c.location_type='atm' AND c.location_ref=? ORDER BY d.sort_seq""",
                    (a["id"],)).fetchall()
                total_cas += len(cass)
                rows = "".join(
                    f"""<tr><td><span class="cas-code">{esc(c["code"])}</span></td>
                      <td>{esc(c["denom_label"])}</td>
                      <td class="num">{esc(c["capacity"]):} 张</td>
                      <td>{seal_chip(c["seal_no"], c["sealed"])}</td></tr>"""
                    for c in cass) or '<tr><td colspan="4" class="empty">无机内钞箱</td></tr>'
                online = a["online"]
                cards.append(f"""
                <div class="atm-card">
                  <div class="atm-card-head">
                    <div><b>{esc(a["code"])}</b>
                      <span class="atm-model">{esc(a["brand"])} {esc(a["model"])}</span></div>
                    {online_dot(online)}
                  </div>
                  <table class="mini-table"><thead><tr>
                    <th>钞箱</th><th>券别</th><th class="num">容量</th><th>封签</th>
                  </tr></thead><tbody>{rows}</tbody></table>
                  <div class="atm-card-foot">
                    <a class="btn-mini" href="/trace?cassette={esc(cass[0]["id"]) if cass else ""}">查看轨迹</a>
                    <button class="btn-mini" onclick='toggleOnline(this,{a["id"]!r},{0 if online else 1})'>
                      {"置为离线" if online else "恢复在线"}</button>
                  </div>
                </div>""")
            groups.append(f"""
            <section class="branch-group">
              <h2 class="group-title"><span class="gold-bar"></span>{esc(b["name"])}
                <small>{esc(b["code"])}</small>
                <span class="group-count">{len(atms)} 台 ATM</span></h2>
              <div class="card-grid">{''.join(cards) or empty_hint("该网点暂无 ATM")}</div>
            </section>""")
        body = page_title("ATM 台账", "按网点分组展示 ATM、机内钞箱券别容量与封签状态")
        body += stat_chips([("可见网点", len(branches)), ("ATM 总数", total_atm),
                            ("机内钞箱", total_cas)])
        body += flash() + "".join(groups)
        return layout("ATM 台账", "/atms", emp, body)
    finally:
        conn.close()


def online_dot(online):
    if online:
        return '<span class="online-dot on" title="在线">● 在线</span>'
    return '<span class="online-dot off" title="离线">● 离线</span>'


def seal_chip(seal_no, sealed, intact=1):
    if not sealed:
        return '<span class="seal none">未封存</span>'
    cls = "seal ok" if intact else "seal broken"
    txt = "封签完好" if intact else "封签破损"
    return f'<span class="{cls}">🔏 {esc(seal_no)} · {txt}</span>'


def empty_hint(text):
    return f'<div class="empty-hint">{esc(text)}</div>'


# ---------------------------------------------------------------- 装钞计划

def page_plans(emp):
    conn = get_conn()
    try:
        bids = _accessible_branch_ids(conn, emp)
        ph = ",".join("?" * len(bids))
        plans = conn.execute(
            f"""SELECT p.*, a.code atm_code, b.name branch_name,
                (SELECT COUNT(*) FROM load_plan_items i WHERE i.plan_id=p.id) item_n,
                (SELECT COALESCE(SUM(amount_cents),0) FROM load_plan_items i WHERE i.plan_id=p.id) total
                FROM load_plans p JOIN atms a ON a.id=p.atm_id
                JOIN branches b ON b.id=p.branch_id
                WHERE p.branch_id IN ({ph}) ORDER BY p.created_at DESC""",
            bids).fetchall() if bids else []
        emps_by_branch = {b: _staff_options(conn, b) for b in bids}
        cards = []
        for p in plans:
            items = conn.execute(
                """SELECT i.*, c.code cas_code, c.sealed, c.seal_no, c.location_type,
                          d.label denom_label, c.capacity
                   FROM load_plan_items i JOIN cassettes c ON c.id=i.cassette_id
                   JOIN denominations d ON d.id=i.denom_id
                   WHERE i.plan_id=? ORDER BY d.sort_seq""", (p["id"],)).fetchall()
            rows = []
            for i in items:
                loc = {"branch": "网点库", "atm": "机内", "transit": "在途",
                       "vault": "中心库"}.get(i["location_type"], i["location_type"])
                seal = seal_chip(i["seal_no"], i["sealed"])
                edit = ""
                if p["status"] == "草稿":
                    edit = (f'<input class="num-input" type="number" min="0" value="{i["notes"]}" '
                            f'data-item="{i["id"]}" data-cap="{i["capacity"]}">')
                else:
                    edit = f'<span class="num cell-num">{i["notes"]:,} 张</span>'
                rows.append(f"""<tr>
                  <td><span class="cas-code">{esc(i["cas_code"])}</span></td>
                  <td>{esc(i["denom_label"])}</td>
                  <td class="num">{edit}</td>
                  <td class="num cell-money">{money(i["amount_cents"])}</td>
                  <td>{seal}<br><small class="muted">{loc}</small></td></tr>""")
            staff = emps_by_branch.get(p["branch_id"], "")
            actions = []
            if p["status"] == "草稿":
                actions.append(
                    f'<button class="btn-gold" onclick="savePlanItems(this,{p["id"]!r})">保存数量</button>')
                actions.append(
                    f'<button class="btn-green" onclick="actPlan(this,{p["id"]!r},\'/api/plans/lock\')">锁定计划</button>')
            if p["status"] == "已锁定":
                actions.append(f"""<fieldset class="dual">
                    <legend>双人签收出库</legend>
                    <select data-s1>{staff}</select><select data-s2>{staff}</select>
                    <button class="btn-mini" onclick="sealAndLeave(this,{p["id"]!r})">封存并出库</button>
                  </fieldset>""")
                actions.append(
                    f'<button class="btn-green" onclick="actPlan(this,{p["id"]!r},\'/api/plans/load\')">执行装钞</button>')
            cards.append(f"""
            <div class="plan-card">
              <div class="plan-head">
                <div><b>{esc(p["plan_no"])}</b> {badge(p["status"])}
                  <span class="muted">{esc(p["branch_name"])} · {esc(p["atm_code"])} · 计划日 {esc(p["planned_date"])}</span></div>
                <div class="plan-total">计划金额 <b class="big-money">{money(p["total"])}</b>
                  <span class="muted">{p["item_n"]} 个钞箱</span></div>
              </div>
              <table class="data-table"><thead><tr>
                <th>钞箱</th><th>券别</th><th class="num">张数</th><th class="num">金额</th><th>封签 / 位置</th>
              </tr></thead><tbody>{''.join(rows)}</tbody>
              <tfoot><tr><td colspan="3" class="num">合计</td>
                <td class="num cell-money">{money(p["total"])}</td><td></td></tr></tfoot></table>
              <div class="plan-actions">{''.join(actions)}</div>
            </div>""")

        # 新建计划表单
        atm_opts = ""
        for b in bids:
            brow = conn.execute("SELECT * FROM branches WHERE id=?", (b,)).fetchone()
            opts = "".join(f'<option value="{a["id"]}">{esc(a["code"])}（{esc(a["brand"])} {esc(a["model"])})</option>'
                           for a in conn.execute("SELECT * FROM atms WHERE branch_id=? ORDER BY code", (b,)))
            atm_opts += f'<optgroup label="{esc(brow["name"])}">{opts}</optgroup>'
        cas_rows = []
        for b in bids:
            brow = conn.execute("SELECT * FROM branches WHERE id=?", (b,)).fetchone()
            opts = "".join(
                f"""<option value="{c["id"]}" data-branch="{b}" data-denom="{esc(c["denom_id"] or "")}">
                    {esc(c["code"])}（{esc(dl)}·容量{c["capacity"]}张）</option>"""
                for c in conn.execute(
                    """SELECT c.*, d.label dl FROM cassettes c LEFT JOIN denominations d ON d.id=c.denom_id
                       WHERE c.location_type='branch' AND c.location_ref=? ORDER BY c.code""", (b,))
                for dl in [c["dl"]])
            if opts:
                cas_rows.append(f'<optgroup label="{esc(brow["name"])}">{opts}</optgroup>')
        cas_opts = "".join(cas_rows)
        create = f"""
        <div class="create-box">
          <h3>新建装钞计划</h3>
          <div class="form-row">
            <label>ATM <select id="np-atm">{atm_opts}</select></label>
            <label>计划日期 <input id="np-date" type="date" value="2026-10-01"></label>
            <label>备注 <input id="np-note" placeholder="如：节后加钞"></label>
          </div>
          <table class="data-table" id="np-items"><thead><tr>
            <th>钞箱（库内）</th><th>券别</th><th class="num">张数</th><th></th></tr></thead><tbody>
            {''.join(f'''<tr class="np-row">
              <td><select class="np-cas">{cas_opts}</select></td>
              <td><select class="np-denom">{denom_options(conn)}</select></td>
              <td class="num"><input class="num-input np-notes" type="number" min="0" value="0"></td>
              <td><button class="btn-mini danger" onclick="this.closest('tr').remove()">删除</button></td>
            </tr>''' for _ in range(4))}
          </tbody></table>
          <button class="btn-green" onclick="createPlan()">创建计划（草稿）</button>
          <small class="muted">提示：计划须先锁定才能装钞；已封存钞箱不能直接改数量。</small>
        </div>"""

        body = page_title("装钞计划", "锁定后装钞；钞箱离开网点须两名不同员工签收")
        body += stat_chips([("计划总数", len(plans)),
                            ("进行中", sum(1 for p in plans if p["status"] in ("草稿", "已锁定"))),
                            ("已完成", sum(1 for p in plans if p["status"] == "已完成"))])
        body += flash() + create + ("".join(cards) or empty_hint("暂无装钞计划"))
        return layout("装钞计划", "/plans", emp, body)
    finally:
        conn.close()


def denom_options(conn):
    return "".join(f'<option value="{d["id"]}">{esc(d["label"])}</option>'
                   for d in conn.execute("SELECT * FROM denominations ORDER BY sort_seq"))


def _staff_options(conn, branch_id):
    rows = conn.execute(
        """SELECT e.* FROM employees e JOIN employee_branches eb ON eb.employee_id=e.id
           WHERE eb.branch_id=? ORDER BY e.staff_no""", (branch_id,)).fetchall()
    return "".join(f'<option value="{e["id"]}">{esc(e["name"])}（{esc(e["role"])}）</option>'
                   for e in rows)


# ---------------------------------------------------------------- 清机录入

def page_clear(emp, order_id):
    conn = get_conn()
    try:
        bids = _accessible_branch_ids(conn, emp)
        orders = conn.execute(
            """SELECT o.*, a.code atm_code, b.name branch_name FROM clear_orders o
               JOIN atms a ON a.id=o.atm_id JOIN branches b ON b.id=o.branch_id
               WHERE o.branch_id IN (%s) ORDER BY o.created_at DESC"""
            % ",".join("?" * len(bids)), bids).fetchall() if bids else []
        ph = ",".join("?" * len(bids))
        atm_opts = ""
        for b in bids:
            brow = conn.execute("SELECT name FROM branches WHERE id=?", (b,)).fetchone()
            opts = "".join(f'<option value="{a["id"]}">{esc(a["code"])}</option>'
                           for a in conn.execute("SELECT * FROM atms WHERE branch_id=? ORDER BY code", (b,)))
            atm_opts += f'<optgroup label="{esc(brow["name"])}">{opts}</optgroup>'
        sidebar = f"""
        <div class="side">
          <div class="side-box">
            <h3>新建清机单</h3>
            <select id="new-order-atm">{atm_opts}</select>
            <input id="new-order-remark" type="text" placeholder="现场备注（如吞卡卡号登记）"
              style="width:100%;margin-bottom:6px">
            <button class="btn-green wide" onclick="createOrder()">按最近装钞计划建单</button>
          </div>
          <div class="side-box">
            <h3>清机单</h3>
            {''.join(f'''<a class="order-link {"cur" if o["id"]==order_id else ""}" href="/clear?order={o["id"]}">
                <b>{esc(o["order_no"])}</b> {badge(o["status"])}
                <small>{esc(o["atm_code"])} · 差额 <span class="{"diff-minus" if o["diff_cents"]<0 else "diff-plus" if o["diff_cents"]>0 else ""}">{money(o["diff_cents"], signed=True)}</span></small>
              </a>''' for o in orders) or empty_hint("暂无清机单")}
          </div>
        </div>"""

        if not order_id or not conn.execute("SELECT 1 FROM clear_orders WHERE id=?", (order_id,)).fetchone():
            main = f'<div class="main-box">{empty_hint("请选择或新建一张清机单")}</div>'
            body = page_title("清机录入", "按券别逐档录入实点与残钞；离线自动进入补传队列")
            body += flash() + f'<div class="clear-layout">{sidebar}{main}</div>'
            return layout("清机录入", "/clear", emp, body)

        o = conn.execute(
            """SELECT o.*, a.code atm_code, a.online, b.name branch_name
               FROM clear_orders o JOIN atms a ON a.id=o.atm_id
               JOIN branches b ON b.id=o.branch_id WHERE o.id=?""", (order_id,)).fetchone()
        if o["branch_id"] not in bids:
            raise PermissionError("无权访问其他网点清机单")
        counts = conn.execute(
            """SELECT cc.*, d.label denom_label, d.value_cents, d.sort_seq
               FROM clear_counts cc JOIN denominations d ON d.id=cc.denom_id
               WHERE cc.order_id=? ORDER BY d.sort_seq""", (order_id,)).fetchall()
        rows = []
        for c in counts:
            rows.append(f"""<tr>
              <td>{esc(c["denom_label"])}</td>
              <td class="num cell-num">{c["expected_notes"]:,}</td>
              <td class="num"><input class="num-input counted" type="number" min="0"
                    value="{c["counted_notes"]}" data-val="{c["value_cents"]}"
                    data-denom="{esc(c["denom_id"])}"></td>
              <td class="num"><input class="num-input residual" type="number" min="0"
                    value="{c["residual_notes"]}"></td>
              <td class="num cell-money" data-amount>{money(c["amount_cents"])}</td>
            </tr>""")
        atm_cass = conn.execute(
            """SELECT c.id, c.code, c.seal_no FROM cassettes c
               WHERE c.location_type='atm' AND c.location_ref=?""", (o["atm_id"],)).fetchall()
        seal_checks = "".join(
            f"""<label class="check"><input type="checkbox" name="broken" value="{esc(c["code"])}">
                {esc(c["code"])}（{esc(c["seal_no"] or "无封签")}）封签破损</label>"""
            for c in atm_cass) or '<span class="muted">该机无在柜钞箱</span>'
        staff = _staff_options(conn, o["branch_id"])
        corrections = conn.execute(
            """SELECT ac.*, d.label denom_label,
                      e1.name operator_name, e2.name approver_name
               FROM amount_corrections ac JOIN denominations d ON d.id=ac.denom_id
               LEFT JOIN employees e1 ON e1.id=ac.operator_id
               LEFT JOIN employees e2 ON e2.id=ac.approver_id
               WHERE ac.order_id=? ORDER BY ac.id""", (order_id,)).fetchall()
        corr_rows = "".join(
            f"""<tr><td>{esc(x["created_at"][5:16].replace("T", " "))}</td>
              <td>{esc(x["denom_label"])}</td>
              <td class="num diff-minus">{money(x["old_cents"])}</td>
              <td class="num diff-plus">{money(x["new_cents"])}</td>
              <td>{esc(x["reason"])}</td>
              <td>{esc(x["operator_name"])} → 审批 {esc(x["approver_name"])}</td></tr>"""
            for x in corrections) or '<tr><td colspan="6" class="empty">尚无修正记录</td></tr>'
        atts = conn.execute("SELECT * FROM attachments WHERE order_id=? ORDER BY id",
                            (order_id,)).fetchall()
        att_rows = "".join(
            f"""<li><a href="/attachments?id={a["id"]}">📎 {esc(a["filename"])}</a>
                <small>{a["size_bytes"]:,} B · {esc(a["created_at"][5:16].replace("T"," "))}</small></li>"""
            for a in atts) or '<li class="empty">暂无附件（清点单照片、封签照片等）</li>'

        readonly = o["status"] == "已关闭"
        inputs_disabled = "disabled" if readonly else ""
        head = f"""
        <div class="main-box">
          <div class="order-head">
            <div><h2>{esc(o["order_no"])} {badge(o["status"])} {online_dot(o["online"])}
                {'<span class="b-gold badge">补传挂起</span>' if o["pending_sync"] else ""}
                {'<span class="b-green badge">已回传</span>' if o["reported"] else ""}</h2>
            <small>{esc(o["branch_name"])} · {esc(o["atm_code"])}</small>
            {'<div class="order-remark">💳 ' + esc(o["remark"]) + '</div>' if o["remark"] else ""}</div>
            <div class="order-amounts">
              <div><span>应点</span><b class="cell-money">{money(o["expected_cents"])}</b></div>
              <div><span>实点</span><b class="cell-money" id="sum-counted">{money(o["counted_cents"])}</b></div>
              <div><span>残钞</span><b class="cell-money">{money(o["residual_cents"])}</b></div>
              <div><span>差额</span><b class="cell-money {"diff-minus" if o["diff_cents"]<0 else "diff-plus" if o["diff_cents"]>0 else ""}"
                 id="sum-diff" data-expected="{o["expected_cents"]}">{money(o["diff_cents"], signed=True)}</b></div>
            </div>
          </div>"""
        form = f"""
          <table class="data-table"><thead><tr>
            <th>券别</th><th class="num">应点张数</th><th class="num">实点张数</th>
            <th class="num">残钞张数</th><th class="num">金额</th></tr></thead>
            <tbody id="count-rows">{''.join(rows)}</tbody></table>
          <div class="clear-form">
            <label>回传批次号 <input id="submit-ref" placeholder="如 BATCH-A1-002"
                value="BATCH-{esc(o['atm_code'])}-{esc(o['order_no'][-4:])}" {inputs_disabled}></label>
            <fieldset><legend>异常封签（清机现场核对）</legend><div class="checks">{seal_checks}</div></fieldset>
            <div class="form-actions">
              <button class="btn-green" onclick="submitCounts({order_id!r})" {inputs_disabled}>录入并回传</button>
              <span class="muted">短款 / 长款 / 残钞 / 封签破损 / 离线 / 重复回传 将自动生成差异记录</span>
            </div>
          </div>"""
        denom_opts = "".join(
            f'<option value="{c["denom_id"]}">{esc(c["denom_label"])}</option>' for c in counts)
        other_staff = "".join(
            f'<option value="{s[0]}">{esc(s[1])}（{esc(s[2])}）</option>'
            for s in conn.execute(
                """SELECT e.id,e.name,e.role FROM employees e
                   JOIN employee_branches eb ON eb.employee_id=e.id
                   WHERE eb.branch_id=? AND e.id<>? ORDER BY e.staff_no""",
                (o["branch_id"], emp)))
        correct_box = f"""
          <div class="sub-box">
            <h3>金额修正 <small class="muted">须填写原因并由他人审批，原值全程留痕</small></h3>
            <div class="form-row">
              <label>券别<select id="cor-denom">{denom_opts}</select></label>
              <label>修正后实点张数<input id="cor-notes" type="number" min="0"></label>
              <label>审批人<select id="cor-approver">{other_staff}</select></label>
            </div>
            <label class="full">修正原因<input id="cor-reason" placeholder="如：串把 / 漏点一捆，需说明复盘过程"></label>
            <button class="btn-gold" onclick="correctAmount({order_id!r})" {inputs_disabled}>提交修正</button>
            <table class="data-table slim"><thead><tr>
              <th>时间</th><th>券别</th><th class="num">原值</th><th class="num">修正为</th>
              <th>原因</th><th>经办 / 审批</th></tr></thead><tbody>{corr_rows}</tbody></table>
          </div>"""
        attach_box = f"""
          <div class="sub-box">
            <h3>清机附件</h3>
            <form id="att-form" enctype="multipart/form-data">
              <input type="hidden" name="order_id" value="{esc(order_id)}">
              <input type="file" name="file" multiple>
              <button class="btn-mini" type="button" onclick="uploadAtt(this)">上传到本地目录</button>
            </form>
            <ul class="att-list">{att_rows}</ul>
          </div>"""
        close_btn = ""
        if o["status"] != "已关闭":
            close_btn = ('<div class="sub-box close-box"><button class="btn-gold" '
                         f'onclick="closeOrder({order_id!r})">复核完成，关闭清机单</button>'
                         '<span class="muted">存在待复核差异或补传未完成时将被拒绝</span></div>')
        main = head + form + correct_box + attach_box + close_btn + "</div>"
        body = page_title("清机录入", "逐券别录入实点金额；修正保留原值、原因与审批人")
        body += flash() + f'<div class="clear-layout">{sidebar}{main}</div>'
        return layout("清机录入", "/clear", emp, body)
    finally:
        conn.close()


# ---------------------------------------------------------------- 钞箱轨迹

def page_trace(emp, cassette_id):
    conn = get_conn()
    try:
        bids = _accessible_branch_ids(conn, emp)
        groups = {}
        for c in conn.execute("""SELECT c.*, d.label denom_label
                                 FROM cassettes c LEFT JOIN denominations d ON d.id=c.denom_id
                                 ORDER BY c.code"""):
            owner = None
            if c["location_type"] == "branch":
                owner = c["location_ref"]
            elif c["location_type"] == "atm":
                row = conn.execute("SELECT branch_id FROM atms WHERE id=?",
                                   (c["location_ref"],)).fetchone()
                owner = row["branch_id"] if row else None
            elif c["location_type"] == "transit":
                owner = "*"  # 在途：所有网点可见
            if owner == "*" or owner in bids:
                groups.setdefault(owner if owner != "*" else "*", []).append(c)
        opt_groups = ""
        for gkey, cs in groups.items():
            if gkey == "*":
                gtitle = "🚚 在途"
            else:
                brow = conn.execute("SELECT name FROM branches WHERE id=?", (gkey,)).fetchone()
                gtitle = f"🏦 {brow['name']}" if brow else gkey
            opt_groups += f'<optgroup label="{gtitle}">' + "".join(
                f'<option value="{c["id"]}" {"selected" if c["id"]==cassette_id else ""}>'
                f'{esc(c["code"])} · {esc(c["denom_label"] or "未配券别")}</option>'
                for c in cs) + "</optgroup>"
        sel = f"""<div class="trace-select"><select id="cas-select" onchange="location.href='/trace?cassette='+this.value">
                  <option value="">选择钞箱…</option>{opt_groups}</select></div>"""
        if not cassette_id:
            body = page_title("钞箱轨迹", "封签状态以连续轨迹呈现，双人签收节点突出显示")
            body += flash() + sel + empty_hint("请选择钞箱查看完整封签与交接轨迹")
            return layout("钞箱轨迹", "/trace", emp, body)

        c = conn.execute(
            """SELECT c.*, d.label denom_label, d.value_cents FROM cassettes c
               LEFT JOIN denominations d ON d.id=c.denom_id WHERE c.id=?""",
            (cassette_id,)).fetchone()
        if c is None:
            raise PermissionError("钞箱不存在")
        # 直接 URL 访问也要校验网点（在途钞箱所有网点可见）
        if c["location_type"] == "branch" and c["location_ref"] not in bids:
            raise PermissionError("无权查看其他网点钞箱")
        if c["location_type"] == "atm":
            a = conn.execute("SELECT branch_id FROM atms WHERE id=?",
                             (c["location_ref"],)).fetchone()
            if not a or a["branch_id"] not in bids:
                raise PermissionError("无权查看其他网点钞箱")
        events = conn.execute(
            """SELECT e.*, emp.name employee_name, emp2.name employee2_name
               FROM cassette_events e
               LEFT JOIN employees emp ON emp.id=e.employee_id
               LEFT JOIN employees emp2 ON emp2.id=e.employee2_id
               WHERE e.cassette_id=? ORDER BY e.id""", (cassette_id,)).fetchall()
        loc_text = {"branch": "网点库", "atm": "ATM 机内", "transit": "在途",
                    "vault": "中心库"}.get(c["location_type"], c["location_type"])
        head = f"""
        <div class="trace-head">
          <div><h2>💳 {esc(c["code"])} {seal_chip(c["seal_no"], c["sealed"])}</h2>
            <small>{esc(c["denom_label"] or "未配券别")} · 容量 {c["capacity"]:,} 张 ·
              当前位置：<b>{loc_text}</b> · 状态 {esc(c["status"])}</small></div>
        </div>"""
        tl = []
        for i, e in enumerate(events):
            dual = ""
            if e["employee2_id"]:
                dual = ('<div class="dual-sign">👥 双人签收：'
                        f'<b>{esc(e["employee_name"])}</b> 与 <b>{esc(e["employee2_name"])}</b></div>')
            seal_html = ""
            if e["seal_no"]:
                ok = e["seal_intact"]
                seal_html = seal_chip(e["seal_no"], 1, ok)
            fromto = ""
            if e["from_loc"] or e["to_loc"]:
                fromto = (f'<div class="trace-loc"><span>{esc(_loc_label(conn, e["from_loc"]))}</span>'
                          f'<i>→</i><span>{esc(_loc_label(conn, e["to_loc"]))}</span></div>')
            tl.append(f"""
            <div class="tl-item">
              <div class="tl-dot {'broken' if not e['seal_intact'] and e['seal_no'] else ''}"></div>
              <div class="tl-card">
                <div class="tl-top"><b>{esc(e["event_type"])}</b>
                  {seal_html}<time>{esc(e["created_at"][5:16].replace("T"," "))}</time></div>
                {fromto}{dual}
                {'<div class="tl-detail">'+esc(e["detail"])+'</div>' if e["detail"] else ""}
                {'<div class="muted">经办：'+esc(e["employee_name"] or "系统")+'</div>' if not e["employee2_id"] and e["employee_name"] else ""}
                {'<div class="muted">关联单据：'+esc(e["ref_no"])+'</div>' if e["ref_no"] else ""}
              </div></div>""")
        timeline = f'<div class="timeline">{"".join(tl) or empty_hint("暂无轨迹")}</div>'
        body = page_title("钞箱轨迹", "封签状态以连续轨迹呈现，双人签收节点突出显示")
        body += flash() + sel + head + timeline
        return layout("钞箱轨迹", "/trace", emp, body)
    finally:
        conn.close()


def _loc_label(conn, raw):
    if not raw:
        return "—"
    kind, _, ref = raw.partition(":")
    if kind == "branch":
        r = conn.execute("SELECT name FROM branches WHERE id=?", (ref,)).fetchone()
        return f"网点库·{r['name']}" if r else raw
    if kind == "atm":
        r = conn.execute("SELECT code FROM atms WHERE id=?", (ref,)).fetchone()
        return f"ATM·{r['code']}" if r else raw
    return {"branch": "网点库", "transit": "在途", "vault": "中心库"}.get(raw, raw)


# ---------------------------------------------------------------- 差额复核

def page_review(emp):
    conn = get_conn()
    try:
        bids = set(_accessible_branch_ids(conn, emp))
        discs = conn.execute(
            """SELECT d.*, o.order_no, o.branch_id, a.code atm_code, b.name branch_name
               FROM discrepancies d JOIN clear_orders o ON o.id=d.order_id
               JOIN atms a ON a.id=o.atm_id JOIN branches b ON b.id=o.branch_id
               ORDER BY CASE d.status WHEN '待复核' THEN 0 WHEN '复核驳回' THEN 1 ELSE 2 END,
                        d.due_date""").fetchall()
        mine = [d for d in discs if d["branch_id"] in bids]
        n_pending = sum(1 for d in mine if d["status"] in ("待复核", "复核驳回"))
        n_overdue = sum(1 for d in mine if d["status"] in ("待复核",) and _overdue(d["due_date"]))
        total_short = sum(d["amount_cents"] for d in mine if d["kind"] == "短款")
        total_long = sum(d["amount_cents"] for d in mine if d["kind"] == "长款")
        cards = []
        for d in mine:
            staff = _staff_options(conn, d["branch_id"])
            overdue = _overdue(d["due_date"])
            due_cls = "due-overdue" if overdue else "due-soon"
            amount_html = (f'<b class="disc-amount {"diff-minus" if d["kind"]=="短款" else "diff-plus" if d["amount_cents"] else ""}">'
                           f'{money(d["amount_cents"], signed=True)}</b>'
                           if d["kind"] in ("短款", "长款", "残钞")
                           else '<b class="disc-amount noncash">实物/状态异常</b>')
            review_box = ""
            if d["status"] in ("待复核", "复核驳回"):
                review_box = f"""
                <div class="review-box">
                  <label>复核人一<select data-r1>{staff}</select></label>
                  <label>复核人二<select data-r2>{staff}</select></label>
                  <input data-comment placeholder="复核意见（必填两人须不同）">
                  <div class="review-btns">
                    <button class="btn-green" onclick="review(this,{d["id"]!r},'通过')">双人复核通过</button>
                    <button class="btn-mini danger" onclick="review(this,{d["id"]!r},'驳回')">驳回</button>
                  </div>
                </div>"""
            elif d["status"] == "复核通过":
                review_box = f"""
                <div class="review-box resolved">
                  <input data-resolve placeholder="处置说明（如挂账追赔/残钞上缴/追责）">
                  <button class="btn-gold" onclick="resolveDisc(this,{d["id"]!r})">登记处置</button>
                </div>"""
            review_log = ""
            revs = conn.execute(
                """SELECT r.*, e1.name n1, e2.name n2 FROM reviews r
                   LEFT JOIN employees e1 ON e1.id=r.reviewer1_id
                   LEFT JOIN employees e2 ON e2.id=r.reviewer2_id
                   WHERE r.discrepancy_id=? ORDER BY r.id""", (d["id"],)).fetchall()
            if revs:
                review_log = '<div class="review-log">' + "".join(
                    f'<div>{"✅" if r["result"]=="通过" else "↩️"} {esc(r["n1"])} + {esc(r["n2"])}：'
                    f'{esc(r["result"])} — {esc(r["comment"] or "无意见")}</div>' for r in revs) + "</div>"
            cards.append(f"""
            <div class="disc-card {d["kind"]}">
              <div class="disc-top">
                <div><b>{esc(d["code"])}</b> {badge(d["kind"], KIND_BADGE.get(d["kind"], ""))}
                  {badge(d["status"])}
                  <a class="muted" href="/clear?order={esc(d["order_id"])}">{esc(d["order_no"])}</a>
                  <small class="muted">{esc(d["branch_name"])} · {esc(d["atm_code"])}</small></div>
                {amount_html}
              </div>
              <div class="disc-desc">{esc(d["description"])}</div>
              <div class="disc-due {due_cls}>⏰ 处理期限 {esc(d["due_date"])}
                {"（已逾期）" if overdue else ""}</div>
              {review_log}{review_box}
              {'<div class="resolution">处置：'+esc(d["resolution"])+'</div>' if d["status"]=="已处置" and d["resolution"] else ""}
            </div>""")

        tasks = conn.execute(
            """SELECT t.*, o.order_no, b.name branch_name FROM sync_tasks t
               JOIN clear_orders o ON o.id=t.order_id JOIN branches b ON b.id=o.branch_id
               WHERE o.branch_id IN (%s) ORDER BY t.created_at DESC"""
            % ",".join("?" * len(bids)), list(bids)).fetchall() if bids else []
        task_rows = "".join(
            f"""<tr><td>{esc(t["order_no"])}</td><td>{esc(t["branch_name"])}</td>
              <td>{badge(t["status"])}</td><td class="num">{t["attempts"]}</td>
              <td>{esc(t["last_error"] or "—")}</td><td>{esc(t["updated_at"][5:16].replace("T"," "))}</td></tr>"""
            for t in tasks) or '<tr><td colspan="6" class="empty">暂无补传任务</td></tr>'
        offline_atms = conn.execute(
            f"""SELECT a.*, b.name branch_name FROM atms a JOIN branches b ON b.id=a.branch_id
                WHERE a.online=0 AND a.branch_id IN ({','.join('?'*len(bids))})""",
            list(bids)).fetchall() if bids else []
        off_html = "".join(
            f'<div class="offline-row"><span>⚠️ {esc(a["code"])}（{esc(a["branch_name"])}）离线</span>'
            f'<button class="btn-mini" onclick="toggleOnline(this,{a["id"]!r},1)">恢复在线</button></div>'
            for a in offline_atms) or '<p class="muted">所有 ATM 均在线。</p>'
        queue_box = f"""
        <div class="queue-box">
          <h3>补传队列 <small class="muted">进程内轮询，离线恢复后自动补传；也可手动触发</small>
            <button class="btn-mini" onclick="pump()">立即补传一轮</button></h3>
          <div class="offline-list">{off_html}</div>
          <table class="data-table slim"><thead><tr>
            <th>清机单</th><th>网点</th><th>状态</th><th class="num">尝试</th>
            <th>最后错误</th><th>更新时间</th></tr></thead><tbody>{task_rows}</tbody></table>
        </div>"""
        body = page_title("差额复核", "短款/长款/残钞/封签破损/离线/重复回传统一复核；关闭清机单前必须清零")
        body += stat_chips([("待复核", n_pending), ("已逾期", n_overdue),
                            ("短款累计", money(-total_short) if total_short else money(0)),
                            ("长款累计", money(total_long))])
        body += flash() + ("".join(cards) or empty_hint("当前网点没有差异记录"))
        body += queue_box
        return layout("差额复核", "/review", emp, body)
    finally:
        conn.close()


def _overdue(due: str) -> bool:
    from datetime import date
    try:
        y, m, d = map(int, due.split("-"))
        return date(y, m, d) < date(2026, 10, 1)
    except Exception:
        return False


# ---------------------------------------------------------------- 审计

def page_audit(emp, qs):
    conn = get_conn()
    try:
        bids = _accessible_branch_ids(conn, emp)
        sql = """SELECT l.*, e.name actor_name, b.name branch_name
                 FROM audit_logs l LEFT JOIN employees e ON e.id=l.actor_id
                 LEFT JOIN branches b ON b.id=l.branch_id
                 WHERE (l.branch_id IS NULL OR l.branch_id IN (%s))""" % ",".join("?" * len(bids))
        args = list(bids)
        kw = (qs.get("q", [""])[0]).strip()
        action = qs.get("action", [""])[0]
        if kw:
            sql += " AND (l.detail LIKE ? OR l.entity_id LIKE ? OR l.action LIKE ?)"
            args += [f"%{kw}%"] * 3
        if action:
            sql += " AND l.action=?"
            args.append(action)
        sql += " ORDER BY l.id DESC LIMIT 300"
        rows = conn.execute(sql, args).fetchall()
        actions = [r["action"] for r in conn.execute(
            "SELECT DISTINCT action FROM audit_logs ORDER BY action")]
        opts = "".join(f'<option {"selected" if a==action else ""}>{esc(a)}</option>'
                       for a in actions)
        kw_attr = esc(kw)
        trs = "".join(
            f"""<tr><td class="nowrap">{esc(r["created_at"][:19].replace("T"," "))}</td>
              <td>{esc(r["actor_name"] or "系统")}</td>
              <td>{esc(r["branch_name"] or "—")}</td>
              <td><b>{esc(r["action"])}</b></td>
              <td>{esc(r["entity"])}</td><td class="mono">{esc(r["entity_id"])}</td>
              <td>{esc(r["detail"])}</td></tr>"""
            for r in rows) or '<tr><td colspan="7" class="empty">无匹配记录</td></tr>'
        body = page_title("审计查询", "全部业务动作留痕，仅可查询本人有权网点的数据")
        body += flash() + f"""
        <form class="filter-bar" method="get" action="/audit">
          <input name="q" value="{kw_attr}" placeholder="搜索明细 / 单据号 / 动作">
          <select name="action"><option value="">全部动作</option>{opts}</select>
          <button class="btn-mini" type="submit">查询</button>
        </form>
        <table class="data-table"><thead><tr>
          <th>时间</th><th>操作人</th><th>网点</th><th>动作</th>
          <th>对象</th><th>对象编号</th><th>明细</th>
        </tr></thead><tbody>{trs}</tbody></table>"""
        return layout("审计查询", "/audit", emp, body)
    finally:
        conn.close()


# ---------------------------------------------------------------- 错误页

def page_error(message, emp=None, code=400):
    box = f"""<div class="error-box"><h2>无法完成操作</h2><p>{esc(message)}</p>
              <a class="btn-green" href="javascript:history.back()">返回上一页</a></div>"""
    if not emp:
        return f'<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/app.css">' \
               f'<div class="login-wrap">{box}</div>'
    return layout("错误", "", emp, box)


def page_404():
    return '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/app.css">' \
           '<div class="login-wrap"><div class="error-box"><h2>404</h2><p>页面不存在</p>' \
           '<a class="btn-green" href="/">返回首页</a></div></div>'
