/* ATM 现金清分平台前端交互（无框架，fetch + 服务端渲染） */

function fmtYuan(cents) {
  const neg = cents < 0;
  const sign = neg ? "-" : "";
  const v = (Math.abs(cents) / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${sign}¥${v}`;
}

function flash(msg, ok) {
  let el = document.getElementById("flash");
  if (!el) {
    el = document.createElement("div");
    el.id = "flash";
    el.className = "flash";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = "flash " + (ok ? "ok" : "err");
  el.style.display = "block";
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.style.display = "none"), 3500);
}

async function api(url, body, onOk) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      flash(data.error || "操作失败", false);
      return null;
    }
    if (data.msg) flash(data.msg, true);
    if (onOk) await onOk(data);
    return data;
  } catch (e) {
    flash("网络错误：" + e.message, false);
    return null;
  }
}

/* ---------------- 登录 ---------------- */
async function login(employeeId) {
  await api("/api/login", { employee_id: employeeId }, () => location.assign("/atms"));
}

/* ---------------- ATM 台账 ---------------- */
function toggleOnline(btn, atmId, online) {
  api("/api/atms/toggle", { atm_id: atmId, online: !!online }, () => location.reload());
}

/* ---------------- 装钞计划 ---------------- */
function actPlan(btn, planId, url) {
  api(url, { plan_id: planId }, () => setTimeout(() => location.reload(), 500));
}

function savePlanItems(btn, planId) {
  const inputs = btn.closest(".plan-card").querySelectorAll(".num-input[data-item]");
  let chain = Promise.resolve();
  for (const inp of inputs) {
    const notes = parseInt(inp.value || "0", 10);
    const cap = parseInt(inp.dataset.cap || "999999", 10);
    if (notes < 0 || notes > cap) {
      flash(`张数须在 0 ~ ${cap} 之间`, false);
      return;
    }
    chain = chain.then((ok) =>
      ok === null ? null : api("/api/plans/item", { item_id: inp.dataset.item, notes })
        .then((d) => (d ? d : null)));
  }
  chain.then((ok) => { if (ok !== null) location.reload(); });
}

function sealAndLeave(btn, planId) {
  const fs = btn.closest("fieldset.dual");
  const s1 = fs.querySelector("[data-s1]").value;
  const s2 = fs.querySelector("[data-s2]").value;
  if (s1 === s2) { flash("必须选择两名不同的员工签收", false); return; }
  api("/api/plans/seal-leave", { plan_id: planId, staff1_id: s1, staff2_id: s2 },
    () => setTimeout(() => location.reload(), 600));
}

function createPlan() {
  const atmId = document.getElementById("np-atm").value;
  const plannedDate = document.getElementById("np-date").value;
  const note = document.getElementById("np-note").value;
  const items = [];
  const seen = new Set();
  for (const row of document.querySelectorAll("#np-items .np-row")) {
    const cassetteId = row.querySelector(".np-cas").value;
    const denomId = row.querySelector(".np-denom").value;
    const notes = parseInt(row.querySelector(".np-notes").value || "0", 10);
    if (notes === 0) continue;
    if (seen.has(cassetteId)) { flash("同一钞箱不能重复选择", false); return; }
    seen.add(cassetteId);
    items.push({ cassette_id: cassetteId, denom_id: denomId, notes });
  }
  if (!items.length) { flash("请至少录入一行非零张数", false); return; }
  api("/api/plans/create", { atm_id: atmId, planned_date: plannedDate, note, items },
    () => setTimeout(() => location.reload(), 600));
}

/* ---------------- 清机录入 ---------------- */
function createOrder() {
  const atmId = document.getElementById("new-order-atm").value;
  const remark = document.getElementById("new-order-remark").value;
  api("/api/clear/create", { atm_id: atmId, remark },
    (d) => location.assign("/clear?order=" + d.id));
}

function bindCountLive() {
  const rows = document.querySelectorAll("#count-rows tr");
  rows.forEach((row) => {
    const counted = row.querySelector(".counted");
    const residual = row.querySelector(".residual");
    if (!counted) return;
    const recompute = () => {
      const val = parseInt(counted.dataset.val, 10);
      const n = parseInt(counted.value || "0", 10);
      const r = parseInt(residual.value || "0", 10);
      if (r > n) residual.style.borderColor = "#b4433f";
      else residual.style.borderColor = "";
      row.querySelector("[data-amount]").textContent = fmtYuan(n * val);
      let total = 0;
      document.querySelectorAll("#count-rows .counted").forEach((c) => {
        total += parseInt(c.value || "0", 10) * parseInt(c.dataset.val, 10);
      });
      const sumEl = document.getElementById("sum-counted");
      const diffEl = document.getElementById("sum-diff");
      if (sumEl) sumEl.textContent = fmtYuan(total);
      if (diffEl) {
        const expected = parseInt(diffEl.dataset.expected, 10);
        const diff = total - expected;
        diffEl.textContent = (diff > 0 ? "+" : "") + fmtYuan(diff);
        diffEl.className = "cell-money " + (diff < 0 ? "diff-minus" : diff > 0 ? "diff-plus" : "");
      }
    };
    counted.addEventListener("input", recompute);
    residual.addEventListener("input", recompute);
  });
}

function submitCounts(orderId) {
  const counts = [];
  document.querySelectorAll("#count-rows tr").forEach((row) => {
    const counted = row.querySelector(".counted");
    const residual = row.querySelector(".residual");
    counts.push({
      denom_id: counted.dataset.denom,
      counted_notes: parseInt(counted.value || "0", 10),
      residual_notes: parseInt(residual.value || "0", 10),
    });
  });
  for (const c of counts) {
    if (c.residual_notes > c.counted_notes) {
      flash("残钞张数不能大于实点张数", false);
      return;
    }
  }
  const ref = document.getElementById("submit-ref").value.trim();
  if (!ref) { flash("请填写回传批次号（用于重复回传判定）", false); return; }
  const broken = Array.from(document.querySelectorAll('input[name="broken"]:checked'))
    .map((i) => i.value);
  api("/api/clear/submit", {
    order_id: orderId, counts, submit_ref: ref, broken_seals: broken,
  }, (d) => setTimeout(() => location.reload(), 700));
}

function correctAmount(orderId) {
  const denomId = document.getElementById("cor-denom").value;
  const notes = parseInt(document.getElementById("cor-notes").value || "", 10);
  const reason = document.getElementById("cor-reason").value.trim();
  const approver = document.getElementById("cor-approver").value;
  if (isNaN(notes) || notes < 0) { flash("请填写修正后张数", false); return; }
  if (!reason) { flash("修正必须填写原因", false); return; }
  api("/api/clear/correct", {
    order_id: orderId, denom_id: denomId, new_counted_notes: notes,
    reason, approver_id: approver,
  }, () => setTimeout(() => location.reload(), 700));
}

function uploadAtt(btn) {
  const form = document.getElementById("att-form");
  const fileInput = form.querySelector('input[type=file]');
  if (!fileInput.files.length) { flash("请先选择附件", false); return; }
  const fd = new FormData(form);
  btn.disabled = true;
  fetch("/api/attachments", { method: "POST", body: fd })
    .then((r) => r.json())
    .then((d) => {
      btn.disabled = false;
      if (d.ok === false) { flash(d.error, false); return; }
      flash(d.msg || "上传成功", true);
      setTimeout(() => location.reload(), 700);
    })
    .catch((e) => { btn.disabled = false; flash("上传失败：" + e.message, false); });
}

function closeOrder(orderId) {
  if (!confirm("确认关闭清机单？存在未复核差异或补传未完成时会被拒绝。")) return;
  api("/api/close", { order_id: orderId }, () => setTimeout(() => location.reload(), 600));
}

/* ---------------- 差额复核 ---------------- */
function review(btn, discId, result) {
  const box = btn.closest(".review-box");
  const r1 = box.querySelector("[data-r1]").value;
  const r2 = box.querySelector("[data-r2]").value;
  const comment = box.querySelector("[data-comment]").value.trim();
  if (r1 === r2) { flash("双人复核必须由两名不同员工完成", false); return; }
  if (!comment) { flash("请填写复核意见", false); return; }
  api("/api/review", {
    discrepancy_id: discId, reviewer1_id: r1, reviewer2_id: r2, result, comment,
  }, () => setTimeout(() => location.reload(), 600));
}

function resolveDisc(btn, discId) {
  const box = btn.closest(".review-box");
  const resolution = box.querySelector("[data-resolve]").value.trim();
  if (!resolution) { flash("请填写处置说明", false); return; }
  api("/api/resolve", { discrepancy_id: discId, resolution },
    () => setTimeout(() => location.reload(), 600));
}

function pump() {
  api("/api/sync/pump", {}, () => setTimeout(() => location.reload(), 800));
}

document.addEventListener("DOMContentLoaded", bindCountLive);
