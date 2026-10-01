# -*- coding: utf-8 -*-
"""HTTP 服务：会话登录、页面路由、JSON 动作接口、附件上传/下载。

仅用标准库（http.server + ThreadingHTTPServer）。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import uuid
from http import HTTPStatus
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, urlparse

from db import get_conn, ATTACH_DIR, now_iso
import services as svc
import queue as sync_queue
import views

SESSIONS: dict[str, str] = {}   # sid -> employee_id
SESSION_LOCK = threading.Lock()

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def yuan(cents) -> str:
    try:
        cents = int(cents or 0)
    except (TypeError, ValueError):
        cents = 0
    sign = "-" if cents < 0 else ""
    return f"{sign}¥{abs(cents)/100:,.2f}"


views.fmt_yuan = yuan


# ---------------------------------------------------------------- 会话

def get_employee_id(handler) -> str | None:
    cookie = SimpleCookie(handler.headers.get("Cookie", ""))
    if "sid" not in cookie:
        return None
    return SESSIONS.get(cookie["sid"].value)


# ---------------------------------------------------------------- 业务数据读取

def overview_counts(conn):
    return {
        "atms": conn.execute("SELECT COUNT(*) c FROM atms").fetchone()["c"],
        "cassettes": conn.execute("SELECT COUNT(*) c FROM cassettes").fetchone()["c"],
        "pending_disc": conn.execute(
            "SELECT COUNT(*) c FROM discrepancies WHERE status IN ('待复核','复核驳回')"
        ).fetchone()["c"],
        "sync_pending": conn.execute(
            "SELECT COUNT(*) c FROM sync_tasks WHERE status IN ('挂起','失败')"
        ).fetchone()["c"],
    }


# ---------------------------------------------------------------- 动作接口

def api_dispatch(path: str, body: dict, emp_id: str):
    """返回 (status_code, dict)。"""
    p = path
    if p == "/api/login":
        with SESSION_LOCK:
            sid = secrets.token_hex(16)
            SESSIONS[sid] = body["employee_id"]
        return {"set_cookie": sid, "ok": True}

    if not emp_id:
        raise svc.BusinessError("请先登录")

    if p == "/api/logout":
        return {"clear_cookie": True, "ok": True}
    if p == "/api/sync/pump":
        n = sync_queue.pump_once()
        return {"ok": True, "msg": f"已执行一轮补传，成功 {n} 条"}
    if p == "/api/atms/toggle":
        svc.set_atm_online(body["atm_id"], bool(body.get("online")), emp_id)
        return {"ok": True}

    if p == "/api/plans/create":
        items = []
        for row in body.get("items", []):
            items.append({
                "cassette_id": row["cassette_id"],
                "denom_id": row["denom_id"],
                "notes": int(row.get("notes") or 0),
            })
        pid = svc.create_plan(body["atm_id"], emp_id, body.get("planned_date", "2026-10-01"),
                              items, body.get("note", ""))
        return {"ok": True, "id": pid}
    if p == "/api/plans/lock":
        svc.lock_plan(body["plan_id"], emp_id)
        return {"ok": True}
    if p == "/api/plans/load":
        svc.execute_load(body["plan_id"], emp_id)
        return {"ok": True}
    if p == "/api/plans/seal-leave":
        moved = svc.plan_seal_and_leave(body["plan_id"], body["staff1_id"],
                                        body["staff2_id"])
        return {"ok": True, "msg": "已双人签收出库：" + ("、".join(moved) or "无待出库钞箱")}
    if p == "/api/plans/item":
        svc.update_plan_item_notes(body["item_id"], int(body["notes"]), emp_id)
        return {"ok": True}

    if p == "/api/cassettes/seal":
        svc.seal_cassette(body["cassette_id"], body["seal_no"], emp_id)
        return {"ok": True}
    if p == "/api/cassettes/leave":
        svc.leave_branch(body["cassette_id"], body["staff1_id"], body["staff2_id"])
        return {"ok": True}
    if p == "/api/cassettes/arrive":
        svc.arrive_branch(body["cassette_id"], body["branch_id"], emp_id)
        return {"ok": True}

    if p == "/api/clear/create":
        oid = svc.create_clear_order(body["atm_id"], emp_id,
                                    body.get("plan_id") or None,
                                    body.get("remark", ""))
        return {"ok": True, "id": oid}
    if p == "/api/clear/submit":
        res = svc.submit_counts(
            body["order_id"], body.get("counts", []), emp_id,
            submit_ref=body.get("submit_ref", ""),
            broken_seals=body.get("broken_seals", []))
        return {"ok": True, **res}
    if p == "/api/clear/correct":
        svc.correct_amount(body["order_id"], body["denom_id"],
                           int(body["new_counted_notes"]), body.get("reason", ""),
                           emp_id, body["approver_id"])
        return {"ok": True}
    if p == "/api/close":
        svc.close_order(body["order_id"], emp_id)
        return {"ok": True}

    if p == "/api/review":
        svc.review_discrepancy(body["discrepancy_id"], body["reviewer1_id"],
                               body["reviewer2_id"], body["result"],
                               body.get("comment", ""))
        return {"ok": True}
    if p == "/api/resolve":
        svc.resolve_discrepancy(body["discrepancy_id"], emp_id, body.get("resolution", ""))
        return {"ok": True}

    raise svc.BusinessError(f"未知接口：{path}")


# ---------------------------------------------------------------- multipart 解析

def parse_multipart(handler, boundary: bytes) -> list[dict]:
    """极简 multipart/form-data 解析，返回 [{name, filename, data, content_type}]。"""
    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length)
    delim = b"--" + boundary
    parts = []
    for chunk in raw.split(delim):
        if not chunk or chunk in (b"--", b"--\r\n", b"\r\n"):
            continue
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        header_end = chunk.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        header = chunk[:header_end].decode("utf-8", "replace")
        data = chunk[header_end + 4:]
        m_name = re.search(r'name="([^"]*)"', header)
        m_file = re.search(r'filename="([^"]*)"', header)
        m_ct = re.search(r"Content-Type:\s*([^\r\n]+)", header, re.I)
        parts.append({
            "name": m_name.group(1) if m_name else "",
            "filename": m_file.group(1) if m_file else None,
            "content_type": m_ct.group(1).strip() if m_ct else "",
            "data": data,
        })
    return parts


def handle_upload(parts, emp_id: str):
    fields = {p["name"]: p["data"].decode("utf-8", "replace") for p in parts
              if not p["filename"]}
    order_id = fields.get("order_id", "")
    conn = get_conn()
    try:
        order = conn.execute("SELECT * FROM clear_orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise svc.BusinessError("清机单不存在")
        svc.assert_branch(conn, emp_id, order["branch_id"])
    finally:
        conn.close()
    saved = []
    for p in parts:
        if not p["filename"] or not p["data"]:
            continue
        safe = re.sub(r"[^A-Za-z0-9._一-鿿-]", "_", p["filename"])[:120]
        aid = f"att-{uuid.uuid4().hex[:10]}"
        stored = os.path.join(ATTACH_DIR, f"{aid}_{safe}")
        with open(stored, "wb") as f:
            f.write(p["data"])
        conn = get_conn()
        try:
            with svc.tx(conn) as c:
                c.execute(
                    """INSERT INTO attachments(id,order_id,filename,stored_path,
                          content_type,size_bytes,uploaded_by,created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (aid, order_id, p["filename"], stored, p["content_type"],
                     len(p["data"]), emp_id, now_iso()),
                )
                svc.audit(c, emp_id, "上传清机附件", "attachment", aid,
                          f"{p['filename']} ({len(p['data'])} B)", order["branch_id"])
        finally:
            conn.close()
        saved.append(p["filename"])
    if not saved:
        raise svc.BusinessError("未选择文件")
    return {"ok": True, "msg": "已上传：" + "、".join(saved)}


# ---------------------------------------------------------------- Handler

class Handler(__import__("http.server", fromlist=["BaseHTTPRequestHandler"]).BaseHTTPRequestHandler):
    server_version = "ATMCash/1.0"

    def log_message(self, fmt, *args):
        pass  # 静默；错误走审计/页面提示

    # ---- 输出 ----
    def _html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _static(self, name):
        path = os.path.join(static_dir, name)
        if not os.path.isfile(path) or os.path.commonpath([path, static_dir]) != static_dir:
            self._html(views.page_404(), 404)
            return
        ctype = {"app.css": "text/css; charset=utf-8",
                 "app.js": "application/javascript; charset=utf-8"}.get(
            name, "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- 路由 ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        emp_id = get_employee_id(self)

        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/attachments":
            return self._download(qs.get("id", [""])[0], emp_id)
        if not emp_id:
            return self._html(views.page_login())

        try:
            if path == "/":
                return self._redirect("/atms")
            if path == "/atms":
                return self._html(views.page_atms(emp_id))
            if path == "/plans":
                return self._html(views.page_plans(emp_id))
            if path == "/clear":
                oid = qs.get("order", [""])[0]
                return self._html(views.page_clear(emp_id, oid))
            if path == "/trace":
                cid = qs.get("cassette", [""])[0]
                return self._html(views.page_trace(emp_id, cid))
            if path == "/review":
                return self._html(views.page_review(emp_id))
            if path == "/audit":
                return self._html(views.page_audit(emp_id, qs))
            self._html(views.page_404(), 404)
        except svc.BusinessError as e:
            self._html(views.page_error(str(e), emp_id), 400)
        except PermissionError as e:
            self._html(views.page_error(str(e), emp_id), 403)

    def _download(self, att_id, emp_id):
        if not emp_id:
            return self._html(views.page_login(), 403)
        conn = get_conn()
        try:
            row = conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
            if not row or not os.path.isfile(row["stored_path"]):
                return self._html(views.page_error("附件不存在", emp_id), 404)
            order = conn.execute("SELECT branch_id FROM clear_orders WHERE id=?",
                                 (row["order_id"],)).fetchone()
            if not svc.can_access_branch(conn, emp_id, order["branch_id"]):
                return self._html(views.page_error("无权下载其他网点附件", emp_id), 403)
            with open(row["stored_path"], "rb") as f:
                data = f.read()
        finally:
            conn.close()
        self.send_response(200)
        self.send_header("Content-Type", row["content_type"] or "application/octet-stream")
        encoded = quote(os.path.basename(row["filename"]))
        ascii_fallback = quote(row["filename"], safe=".-_")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=attachment; filename*=UTF-8''{encoded or ascii_fallback}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlparse(self.path).path
        emp_id = get_employee_id(self)
        ctype = self.headers.get("Content-Type", "")
        try:
            if "multipart/form-data" in ctype:
                m = re.search(r"boundary=([^;]+)", ctype)
                parts = parse_multipart(self, m.group(1).strip().encode())
                if not emp_id:
                    raise svc.BusinessError("请先登录")
                result = handle_upload(parts, emp_id)
                self._json(result)
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            result = api_dispatch(path, body, emp_id)
            headers = None
            if result.get("set_cookie"):
                headers = {"Set-Cookie": f"sid={result['set_cookie']}; Path=/; HttpOnly"}
            if result.get("clear_cookie"):
                headers = {"Set-Cookie": "sid=; Path=/; Max-Age=0"}
            self._json(result, extra_headers=headers)
        except svc.BusinessError as e:
            self._json({"ok": False, "error": str(e)}, status=400)
        except KeyError as e:
            self._json({"ok": False, "error": f"缺少参数：{e}"}, status=400)
        except Exception as e:  # 防御：界面不白屏
            self._json({"ok": False, "error": f"服务器错误：{e}"}, status=500)


def serve(host="127.0.0.1", port=8000):
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"  平台地址: http://{host}:{port}")
    httpd.serve_forever()
