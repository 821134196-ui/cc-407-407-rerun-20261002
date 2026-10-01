# -*- coding: utf-8 -*-
"""平台业务规则测试：

覆盖 券别汇总 / 钞箱锁定 / 重复回传 / 双人复核 / 金额修正 / 网点权限，
另含 双人签收、并发领取只一人成功、离线补传队列。

运行：python3 -m unittest discover -s tests -v
"""
import importlib
import os
import shutil
import tempfile
import threading
import unittest


def _reload_stack(tmpdir):
    """每个用例独立 SQLite 文件：重载模块以重新读取环境变量。"""
    os.environ["ATM_DB"] = os.path.join(tmpdir, "test.db")
    os.environ["ATM_ATTACH"] = os.path.join(tmpdir, "att")
    import db
    import services
    import queue as syncq
    import seed
    importlib.reload(db)
    importlib.reload(services)
    importlib.reload(syncq)
    importlib.reload(seed)
    db.init_db()
    return db, services, syncq, seed


class PlatformTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atm-test-")
        self.db, self.svc, self.syncq, _ = _reload_stack(self.tmp)
        self._bootstrap()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 最小主数据 ----
    def _bootstrap(self):
        db, svc = self.db, self.svc
        conn = db.get_conn()
        try:
            conn.executescript("""
            INSERT INTO branches(id,name,code,created_at) VALUES
              ('b1','城东支行','B001',datetime('now')),
              ('b2','城西支行','B002',datetime('now'));
            INSERT INTO employees(id,name,staff_no,role,created_at) VALUES
              ('e1','王建国','E1','主管',datetime('now')),
              ('e2','李晓梅','E2','清分员',datetime('now')),
              ('e3','张志强','E3','清分员',datetime('now'));
            INSERT INTO employee_branches VALUES ('e1','b1'),('e2','b1'),('e3','b2');
            INSERT INTO atms(id,code,brand,model,branch_id,online,created_at) VALUES
              ('a1','ATM-A1','广电','GT','b1',1,datetime('now')),
              ('a2','ATM-A2','日立','HT','b1',0,datetime('now'));
            INSERT INTO cassettes(id,code,denom_id,capacity,location_type,location_ref,
                  status,sealed,updated_at) VALUES
              ('c1','C-001','d100',2500,'branch','b1','空闲',0,datetime('now')),
              ('c2','C-002','d50', 2500,'branch','b1','空闲',0,datetime('now')),
              ('c3','C-003','d20', 2500,'branch','b1','空闲',0,datetime('now')),
              ('c4','C-004','d10', 2500,'branch','b1','空闲',0,datetime('now'));
            """)
            conn.commit()
        finally:
            conn.close()

    # ---- 通用流程辅助 ----
    def _full_load(self, atm_id="a1", operator="e1",
                   items=None, leave=("e1", "e2")):
        """建计划 → 封存 → 双人出库 → 入库 → 锁定 → 装柜，返回 plan_id。"""
        svc = self.svc
        items = items or [
            ("c1", "d100", 100), ("c2", "d50", 200),
            ("c3", "d20", 100), ("c4", "d10", 100),
        ]
        pid = svc.create_plan(
            atm_id, operator, "2026-10-01",
            [{"cassette_id": c, "denom_id": d, "notes": n} for c, d, n in items])
        for c, _, _ in items:
            svc.seal_cassette(c, f"SEAL-{c.upper()}", operator)
        for c, _, _ in items:
            svc.leave_branch(c, leave[0], leave[1])
            svc.arrive_branch(c, "b1", leave[0])
        svc.lock_plan(pid, operator)
        svc.execute_load(pid, operator)
        return pid

    def _disc_kinds(self, order_id):
        conn = self.db.get_conn()
        try:
            return {r["kind"]: r for r in conn.execute(
                "SELECT * FROM discrepancies WHERE order_id=?", (order_id,))}
        finally:
            conn.close()

    def _item_id(self, plan_id, cassette_id):
        conn = self.db.get_conn()
        try:
            return conn.execute(
                "SELECT id FROM load_plan_items WHERE plan_id=? AND cassette_id=?",
                (plan_id, cassette_id)).fetchone()["id"]
        finally:
            conn.close()


# ================================================================ 1. 券别汇总

class TestDenominationSummary(PlatformTestBase):
    def test_expected_and_counted_by_denomination(self):
        svc = self.svc
        self._full_load()
        oid = svc.create_clear_order("a1", "e2")
        conn = self.db.get_conn()
        try:
            order = conn.execute("SELECT * FROM clear_orders WHERE id=?",
                                 (oid,)).fetchone()
            # 100×10000 + 200×5000 + 100×2000 + 100×1000 = 2,300,000 分
            self.assertEqual(order["expected_cents"], 2_300_000)
            by_denom = {r["denom_id"]: r for r in conn.execute(
                "SELECT * FROM clear_counts WHERE order_id=?", (oid,))}
            self.assertEqual(by_denom["d100"]["expected_notes"], 100)
            self.assertEqual(by_denom["d50"]["expected_notes"], 200)
        finally:
            conn.close()

        res = svc.submit_counts(oid, [
            {"denom_id": "d100", "counted_notes": 100, "residual_notes": 0},
            {"denom_id": "d50", "counted_notes": 198, "residual_notes": 0},
            {"denom_id": "d20", "counted_notes": 100, "residual_notes": 5},
            {"denom_id": "d10", "counted_notes": 100, "residual_notes": 0},
        ], "e2", submit_ref="B-1")
        # 少 2 张 50 元 → 短款 10000；残钞 5 张 20 元 → 10000
        self.assertEqual(res["diff_cents"], -10_000)
        conn = self.db.get_conn()
        try:
            order = conn.execute("SELECT * FROM clear_orders WHERE id=?",
                                 (oid,)).fetchone()
            self.assertEqual(order["counted_cents"], 2_290_000)
            self.assertEqual(order["residual_cents"], 10_000)
        finally:
            conn.close()
        kinds = self._disc_kinds(oid)
        self.assertEqual(kinds["短款"]["amount_cents"], 10_000)
        self.assertEqual(kinds["残钞"]["amount_cents"], 10_000)

    def test_overage_is_positive_long_difference(self):
        svc = self.svc
        self._full_load(items=[
            ("c1", "d100", 10), ("c2", "d50", 0),
            ("c3", "d20", 0), ("c4", "d10", 0)])
        oid = svc.create_clear_order("a1", "e2")
        svc.submit_counts(oid, [
            {"denom_id": "d100", "counted_notes": 12, "residual_notes": 0},
            {"denom_id": "d50", "counted_notes": 0, "residual_notes": 0},
            {"denom_id": "d20", "counted_notes": 0, "residual_notes": 0},
            {"denom_id": "d10", "counted_notes": 0, "residual_notes": 0},
        ], "e2", submit_ref="B-2")
        kinds = self._disc_kinds(oid)
        self.assertEqual(kinds["长款"]["amount_cents"], 20_000)


# ================================================================ 2. 钞箱锁定

class TestCassetteLock(PlatformTestBase):
    def _draft(self):
        return self.svc.create_plan("a1", "e1", "2026-10-01", [
            {"cassette_id": "c1", "denom_id": "d100", "notes": 100}])

    def test_draft_editable_locked_not(self):
        svc = self.svc
        pid = self._draft()
        item = self._item_id(pid, "c1")
        svc.update_plan_item_notes(item, 120, "e1")
        svc.lock_plan(pid, "e1")
        with self.assertRaises(svc.BusinessError):
            svc.update_plan_item_notes(item, 130, "e1")
        with self.assertRaises(svc.BusinessError):
            svc.lock_plan(pid, "e1")  # 不能重复锁定

    def test_sealed_cassette_quantity_frozen(self):
        svc = self.svc
        svc.seal_cassette("c1", "SF-LOCK", "e1")
        pid = self._draft()
        item = self._item_id(pid, "c1")
        with self.assertRaises(svc.BusinessError):
            svc.update_plan_item_notes(item, 200, "e1")

    def test_load_requires_locked_plan_and_seal(self):
        svc = self.svc
        pid = self._draft()
        with self.assertRaises(svc.BusinessError):
            svc.execute_load(pid, "e1")          # 未锁定
        svc.lock_plan(pid, "e1")
        with self.assertRaises(svc.BusinessError):
            svc.execute_load(pid, "e1")          # 未封存
        svc.seal_cassette("c1", "SF-X", "e1")
        svc.leave_branch("c1", "e1", "e2")
        svc.arrive_branch("c1", "b1", "e1")
        svc.execute_load(pid, "e1")              # 成功
        with self.assertRaises(svc.BusinessError):
            svc.execute_load(pid, "e1")          # 已完成不可重复

    def test_capacity_enforced(self):
        svc = self.svc
        with self.assertRaises(svc.BusinessError):
            svc.create_plan("a1", "e1", "2026-10-01", [
                {"cassette_id": "c1", "denom_id": "d100", "notes": 99999}])

    def test_leave_requires_two_different_employees(self):
        svc = self.svc
        svc.seal_cassette("c1", "SF-T", "e1")
        with self.assertRaises(svc.BusinessError):
            svc.leave_branch("c1", "e1", "e1")   # 同一人不可
        with self.assertRaises(svc.BusinessError):
            svc.leave_branch("c1", "e1", "e3")   # e3 无 b1 权限
        svc.leave_branch("c1", "e1", "e2")       # 两名本网点员工

    def test_concurrent_claim_only_one_wins(self):
        svc = self.svc
        svc.seal_cassette("c1", "SF-C", "e1")
        outcomes = []

        def claim(staff):
            try:
                svc.leave_branch("c1", staff, "e2" if staff == "e1" else "e1")
                outcomes.append("ok")
            except svc.BusinessError:
                outcomes.append("fail")

        threads = [threading.Thread(target=claim, args=(s,))
                   for s in ("e1", "e2", "e1", "e2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("fail"), 3)
        conn = self.db.get_conn()
        try:
            cas = conn.execute("SELECT * FROM cassettes WHERE id='c1'").fetchone()
            self.assertEqual(cas["location_type"], "transit")
            n_sign = conn.execute(
                "SELECT COUNT(*) c FROM cassette_events WHERE cassette_id='c1' AND event_type='出库'"
            ).fetchone()["c"]
            self.assertEqual(n_sign, 1)         # 只留下一条双人签收轨迹
        finally:
            conn.close()


# ================================================================ 3. 重复回传

class TestDuplicateSubmission(PlatformTestBase):
    def test_same_batch_ref_rejected_and_recorded(self):
        svc = self.svc
        self._full_load(items=[
            ("c1", "d100", 50), ("c2", "d50", 0),
            ("c3", "d20", 0), ("c4", "d10", 0)])
        oid = svc.create_clear_order("a1", "e2")
        counts = [
            {"denom_id": "d100", "counted_notes": 50, "residual_notes": 0},
            {"denom_id": "d50", "counted_notes": 0, "residual_notes": 0},
            {"denom_id": "d20", "counted_notes": 0, "residual_notes": 0},
            {"denom_id": "d10", "counted_notes": 0, "residual_notes": 0},
        ]
        first = svc.submit_counts(oid, counts, "e2", submit_ref="BATCH-9")
        self.assertFalse(first["offline"])
        with self.assertRaises(svc.BusinessError):
            svc.submit_counts(oid, counts, "e2", submit_ref="BATCH-9")
        kinds = self._disc_kinds(oid)
        self.assertIn("重复回传", kinds)
        # 换批次号可以再次提交（修正实点）
        counts[0]["counted_notes"] = 49
        svc.submit_counts(oid, counts, "e2", submit_ref="BATCH-10")


# ================================================================ 4. 双人复核与关单

class TestDualReview(PlatformTestBase):
    def _short_order(self):
        svc = self.svc
        self._full_load(items=[
            ("c1", "d100", 10), ("c2", "d50", 0),
            ("c3", "d20", 0), ("c4", "d10", 0)])
        oid = svc.create_clear_order("a1", "e2")
        svc.submit_counts(oid, [
            {"denom_id": "d100", "counted_notes": 9, "residual_notes": 0},
            {"denom_id": "d50", "counted_notes": 0, "residual_notes": 0},
            {"denom_id": "d20", "counted_notes": 0, "residual_notes": 0},
            {"denom_id": "d10", "counted_notes": 0, "residual_notes": 0},
        ], "e2", submit_ref="R-1")
        return oid

    def _disc_id(self, oid, kind):
        return self._disc_kinds(oid)[kind]["id"]

    def test_close_blocked_until_reviewed(self):
        svc = self.svc
        oid = self._short_order()
        with self.assertRaises(svc.BusinessError):
            svc.close_order(oid, "e1")               # 有待复核差异
        did = self._disc_id(oid, "短款")
        with self.assertRaises(svc.BusinessError):
            svc.review_discrepancy(did, "e1", "e1", "通过", "同一人不行")
        with self.assertRaises(svc.BusinessError):
            svc.review_discrepancy(did, "e1", "e3", "通过", "跨网点不行")
        # 先驳回：仍不能关单
        svc.review_discrepancy(did, "e1", "e2", "驳回", "依据不足")
        with self.assertRaises(svc.BusinessError):
            svc.close_order(oid, "e1")
        # 再双人复核通过：复核完成，允许关闭（处置可在关单前后登记）
        svc.review_discrepancy(did, "e1", "e2", "通过", "补材料后双人复盘一致")
        svc.close_order(oid, "e1")
        conn = self.db.get_conn()
        try:
            status = conn.execute("SELECT status FROM clear_orders WHERE id=?",
                                  (oid,)).fetchone()["status"]
            self.assertEqual(status, "已关闭")
            n_review_rows = conn.execute(
                "SELECT COUNT(*) c FROM reviews WHERE discrepancy_id=?", (did,)
            ).fetchone()["c"]
            self.assertEqual(n_review_rows, 2)
        finally:
            conn.close()

    def test_reject_then_pass(self):
        svc = self.svc
        oid = self._short_order()
        did = self._disc_id(oid, "短款")
        svc.review_discrepancy(did, "e1", "e2", "驳回", "依据不足")
        with self.assertRaises(svc.BusinessError):
            svc.resolve_discrepancy(did, "e1", "挂账")   # 驳回不可处置
        svc.review_discrepancy(did, "e1", "e2", "通过", "补材料后通过")
        svc.resolve_discrepancy(did, "e1", "短款挂账追赔")
        conn = self.db.get_conn()
        try:
            d = conn.execute("SELECT * FROM discrepancies WHERE id=?",
                             (did,)).fetchone()
            self.assertEqual(d["status"], "已处置")
            self.assertIn("追赔", d["resolution"])
            n_reviews = conn.execute(
                "SELECT COUNT(*) c FROM reviews WHERE discrepancy_id=?",
                (did,)).fetchone()["c"]
            self.assertEqual(n_reviews, 2)
        finally:
            conn.close()


# ================================================================ 5. 金额修正

class TestAmountCorrection(PlatformTestBase):
    def _order(self):
        svc = self.svc
        self._full_load(items=[
            ("c1", "d100", 10), ("c2", "d50", 0),
            ("c3", "d20", 0), ("c4", "d10", 0)])
        oid = svc.create_clear_order("a1", "e2")
        svc.submit_counts(oid, [
            {"denom_id": "d100", "counted_notes": 8, "residual_notes": 0},
            {"denom_id": "d50", "counted_notes": 0, "residual_notes": 0},
            {"denom_id": "d20", "counted_notes": 0, "residual_notes": 0},
            {"denom_id": "d10", "counted_notes": 0, "residual_notes": 0},
        ], "e2", submit_ref="C-1")
        return oid

    def test_correction_rules_and_audit_trail(self):
        svc = self.svc
        oid = self._order()
        with self.assertRaises(svc.BusinessError):
            svc.correct_amount(oid, "d100", 9, "串把", "e2", "e2")  # 审批人=经办人
        with self.assertRaises(svc.BusinessError):
            svc.correct_amount(oid, "d100", 9, "  ", "e2", "e1")    # 原因必填
        with self.assertRaises(svc.BusinessError):
            svc.correct_amount(oid, "d100", 8, "无变化", "e2", "e1")
        svc.correct_amount(oid, "d100", 9, "两捆串把，复盘更正", "e2", "e1")
        conn = self.db.get_conn()
        try:
            row = conn.execute("SELECT * FROM amount_corrections WHERE order_id=?",
                               (oid,)).fetchone()
            self.assertEqual(row["old_cents"], 80_000)
            self.assertEqual(row["new_cents"], 90_000)
            self.assertEqual(row["reason"], "两捆串把，复盘更正")
            self.assertEqual(row["operator_id"], "e2")
            self.assertEqual(row["approver_id"], "e1")
            order = conn.execute("SELECT * FROM clear_orders WHERE id=?",
                                 (oid,)).fetchone()
            self.assertEqual(order["counted_cents"], 90_000)
            self.assertEqual(order["diff_cents"], -10_000)       # 短款 200→100
            disc = conn.execute(
                "SELECT * FROM discrepancies WHERE order_id=? AND kind='短款'",
                (oid,)).fetchone()
            self.assertEqual(disc["amount_cents"], 10_000)
        finally:
            conn.close()

    def test_correction_removes_diff(self):
        svc = self.svc
        oid = self._order()
        svc.correct_amount(oid, "d100", 10, "漏点一捆", "e2", "e1")
        conn = self.db.get_conn()
        try:
            order = conn.execute("SELECT * FROM clear_orders WHERE id=?",
                                 (oid,)).fetchone()
            self.assertEqual(order["diff_cents"], 0)
            disc = conn.execute(
                "SELECT * FROM discrepancies WHERE order_id=? AND kind IN ('短款','长款')",
                (oid,)).fetchone()
            self.assertEqual(disc["amount_cents"], 0)
        finally:
            conn.close()


# ================================================================ 6. 网点权限

class TestBranchPermission(PlatformTestBase):
    def test_other_branch_employee_blocked(self):
        svc = self.svc
        with self.assertRaises(svc.BusinessError):
            svc.create_plan("a1", "e3", "2026-10-01", [
                {"cassette_id": "c1", "denom_id": "d100", "notes": 1}])
        with self.assertRaises(svc.BusinessError):
            svc.create_clear_order("a1", "e3")
        with self.assertRaises(svc.BusinessError):
            svc.seal_cassette("c1", "SF-1", "e3")

    def test_branch_scoped_review(self):
        svc = self.svc
        self._full_load(items=[
            ("c1", "d100", 5), ("c2", "d50", 0),
            ("c3", "d20", 0), ("c4", "d10", 0)])
        oid = svc.create_clear_order("a1", "e2")
        svc.submit_counts(oid, [
            {"denom_id": "d100", "counted_notes": 4, "residual_notes": 0},
            {"denom_id": "d50", "counted_notes": 0, "residual_notes": 0},
            {"denom_id": "d20", "counted_notes": 0, "residual_notes": 0},
            {"denom_id": "d10", "counted_notes": 0, "residual_notes": 0},
        ], "e2", submit_ref="P-1")
        did = self._disc_kinds(oid)["短款"]["id"]
        with self.assertRaises(svc.BusinessError):
            svc.review_discrepancy(did, "e3", "e1", "通过", "越权复核")


# ================================================================ 7. 离线补传队列

class TestSyncQueue(PlatformTestBase):
    def test_offline_then_online_replay_and_dedup(self):
        svc, syncq, db = self.svc, self.syncq, self.db
        self._full_load(atm_id="a2", items=[
            ("c1", "d100", 10), ("c2", "d50", 0),
            ("c3", "d20", 0), ("c4", "d10", 0)])
        oid = svc.create_clear_order("a2", "e2")
        res = svc.submit_counts(oid, [
            {"denom_id": "d100", "counted_notes": 10, "residual_notes": 0},
            {"denom_id": "d50", "counted_notes": 0, "residual_notes": 0},
            {"denom_id": "d20", "counted_notes": 0, "residual_notes": 0},
            {"denom_id": "d10", "counted_notes": 0, "residual_notes": 0},
        ], "e2", submit_ref="OFF-1")
        self.assertTrue(res["offline"])
        conn = db.get_conn()
        try:
            order = conn.execute("SELECT * FROM clear_orders WHERE id=?",
                                 (oid,)).fetchone()
            self.assertEqual(order["pending_sync"], 1)
            self.assertEqual(order["reported"], 0)
            task = conn.execute("SELECT * FROM sync_tasks WHERE order_id=?",
                                (oid,)).fetchone()
            self.assertEqual(task["status"], "挂起")
        finally:
            conn.close()
        # 离线期间重复批次同样拦截
        with self.assertRaises(svc.BusinessError):
            svc.submit_counts(oid, [
                {"denom_id": "d100", "counted_notes": 10, "residual_notes": 0},
                {"denom_id": "d50", "counted_notes": 0, "residual_notes": 0},
                {"denom_id": "d20", "counted_notes": 0, "residual_notes": 0},
                {"denom_id": "d10", "counted_notes": 0, "residual_notes": 0},
            ], "e2", submit_ref="OFF-1")
        # 未恢复在线：补传失败
        self.assertEqual(syncq.pump_once(), 0)
        svc.set_atm_online("a2", True)
        # 恢复在线：补传成功
        self.assertEqual(syncq.pump_once(), 1)
        conn = db.get_conn()
        try:
            order = conn.execute("SELECT * FROM clear_orders WHERE id=?",
                                 (oid,)).fetchone()
            self.assertEqual(order["pending_sync"], 0)
            self.assertEqual(order["reported"], 1)
            task = conn.execute("SELECT * FROM sync_tasks WHERE order_id=?",
                                (oid,)).fetchone()
            self.assertEqual(task["status"], "成功")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
