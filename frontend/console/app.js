/* Service Hub operator console — vanilla JS, same-origin API calls. */
"use strict";

const state = {
  token: localStorage.getItem("hub-token") || null,
  operator: null,
  tenants: [],
  tenantId: null,
  conversationId: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  options = options || {};
  options.headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  if (state.token) options.headers["Authorization"] = "Bearer " + state.token;
  const resp = await fetch(path, options);
  if (resp.status === 401 && state.token) { logout(); throw new Error("session expired"); }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || ("HTTP " + resp.status));
  }
  return resp.json();
}

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

function logout() {
  state.token = null;
  localStorage.removeItem("hub-token");
  $("loginView").classList.remove("hidden");
  $("appView").classList.add("hidden");
  $("logoutBtn").classList.add("hidden");
}

// --------------------------------------------------------------- bootstrap
async function boot() {
  if (!state.token) return;
  try {
    const me = await api("/api/auth/me");
    state.operator = me.operator;
    const data = await api("/api/tenants");
    state.tenants = data.tenants;
    const select = $("tenantSelect");
    select.innerHTML = "";
    state.tenants.forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t._id; opt.textContent = t.name;
      select.appendChild(opt);
    });
    state.tenantId = state.tenants.length ? state.tenants[0]._id : null;
    $("loginView").classList.add("hidden");
    $("appView").classList.remove("hidden");
    $("logoutBtn").classList.remove("hidden");
    $("availabilityBtn").classList.remove("hidden");
    updateAvailabilityBtn();
    await refreshAll();
  } catch (e) {
    logout();
  }
}

async function refreshAll() {
  await Promise.all([loadStats(), loadConversations(), loadArchive(),
                     loadDrafts(), loadInsights(), loadSlaAlerts()]);
}

function updateAvailabilityBtn() {
  const available = !state.operator || state.operator.available !== false;
  $("availabilityBtn").textContent = available ? "🟢 Available" : "⚪ Away";
  $("availabilityBtn").title = "Toggle whether routing assigns you new escalations";
}

async function loadStats() {
  if (!state.tenantId) return;
  const s = await api("/api/operator/stats?tenant_id=" + encodeURIComponent(state.tenantId));
  const parts = [
    s.conversations_total + " conversations",
    s.escalations_total + " escalations",
    s.csat.average != null ? "CSAT " + s.csat.average + " (" + s.csat.count + ")" : "no CSAT yet",
    s.sla.avg_first_response_seconds != null
      ? "first response ~" + Math.round(s.sla.avg_first_response_seconds) + "s" : "",
    s.learning && s.learning.deflection_rate != null
      ? "AI deflection " + Math.round(s.learning.deflection_rate * 100) + "%" : "",
    s.ai_cost ? "AI cost ~$" + s.ai_cost.est_cost_usd : "",
  ].filter(Boolean);
  $("statsBar").textContent = parts.join(" · ");
}

// ------------------------------------------------------------ conversations
async function loadConversations() {
  if (!state.tenantId) return;
  const status = $("statusFilter").value;
  let url = "/api/operator/conversations?tenant_id=" + encodeURIComponent(state.tenantId);
  if (status) url += "&status=" + status;
  const data = await api(url);
  const list = $("convList");
  list.innerHTML = "";
  if (!data.conversations.length) {
    list.innerHTML = '<li class="muted">No conversations yet.</li>';
    return;
  }
  data.conversations.forEach((c) => {
    const li = document.createElement("li");
    const sentiment = c.last_sentiment ? " · sentiment " + c.last_sentiment.score : "";
    li.innerHTML =
      '<span class="badge ' + esc(c.status) + '">' + esc(c.status.replace("_", " ")) + "</span> " +
      esc(c.customer_name || "Customer") + '<br><span class="muted">' +
      esc(c.last_message ? c.last_message.text : "") + "</span>" +
      '<br><span class="muted">' + c.message_count + " messages" + esc(sentiment) + "</span>";
    li.addEventListener("click", () => openConversation(c._id));
    list.appendChild(li);
  });
}

async function openConversation(id) {
  state.conversationId = id;
  const data = await api("/api/operator/conversations/" + id);
  const c = data.conversation;
  const detail = $("convDetail");
  const msgs = data.messages.map((m) =>
    '<div class="msg ' + esc(m.role) + '"><div class="meta">' + esc(m.role) +
    (m.confidence != null ? " · confidence " + m.confidence : "") +
    (m.demo ? " · demo AI" : "") + "</div>" + esc(m.text) +
    (m.attachments || []).map((a) =>
      '<div class="muted">📎 <a href="#" data-att="' + esc(a.id) + '">' +
      esc(a.filename) + "</a></div>").join("") + "</div>"
  ).join("");
  const escInfo = c.escalation
    ? '<p><span class="badge escalated">escalated: ' + esc(c.escalation.rule) + "</span> " +
      esc(c.escalation.detail || "") + "</p>"
    : "";
  const slaInfo = c.sla
    ? '<p class="muted">SLA — first response: ' + esc(c.sla.first_response.state) +
      " · resolution: " + esc(c.sla.resolution.state) + "</p>"
    : "";
  const similarHtml = (data.similar_cases || []).length
    ? '<div class="panel"><h4>Solved before like this</h4>' +
      data.similar_cases.map((s) =>
        '<div class="case"><strong>' + esc(s.question) + "</strong>" +
        (s.resolution ? '<br><span class="muted">Resolution: ' + esc(s.resolution) + "</span>" : "") +
        "</div>").join("") + "</div>"
    : "";
  const historyHtml = (data.customer_history || []).length
    ? '<div class="panel"><h4>This customer before</h4>' +
      data.customer_history.map((h) =>
        '<div class="case">' + esc(h.created_at || "") + " · " + esc(h.status) +
        " — " + esc(h.question) +
        (h.resolution ? '<br><span class="muted">Resolved: ' + esc(h.resolution) + "</span>" : "") +
        "</div>").join("") + "</div>"
    : "";
  const notesHtml =
    '<div class="panel"><h4>Internal notes (never shown to the customer)</h4>' +
    (data.notes || []).map((n) =>
      '<div class="case"><span class="muted">' + esc(n.operator_name) + ":</span> " +
      esc(n.text) + "</div>").join("") +
    '<div class="replyrow"><input id="noteText" placeholder="Add an internal note…" size="48">' +
    '<button id="noteBtn" class="ghost">Add note</button></div></div>';
  detail.innerHTML =
    "<h3>" + esc(c.customer_name || "Customer") + ' <span class="badge ' + esc(c.status) + '">' +
    esc(c.status.replace("_", " ")) + "</span></h3>" + escInfo + slaInfo +
    '<div id="msgThread">' + msgs + "</div>" +
    similarHtml + historyHtml + notesHtml +
    (c.status !== "resolved"
      ? '<div class="replyrow"><textarea id="replyText" rows="2" ' +
        'placeholder="Reply as the human on this conversation…"></textarea></div>' +
        '<div class="actions">' +
        '<button id="takeoverBtn">Take over</button>' +
        '<select id="macroSelect"><option value="">Insert macro…</option></select>' +
        '<button id="replyBtn">Send reply</button>' +
        '<button id="resolveBtn">Resolve</button>' +
        '<input id="resolutionText" placeholder="Resolution summary (goes to the archive)" size="42">' +
        "</div>" +
        '<div class="actions"><label class="muted">Assign to ' +
        '<select id="assignSelect"></select></label>' +
        '<button id="assignBtn" class="ghost">Reassign</button></div>'
      : '<p class="muted">Resolved ' + esc(c.resolved_at || "") + "</p>");
  const thread = $("msgThread");
  if (thread) thread.scrollTop = thread.scrollHeight;

  const takeoverBtn = $("takeoverBtn");
  if (takeoverBtn) takeoverBtn.addEventListener("click", async () => {
    await api("/api/operator/conversations/" + id + "/takeover", { method: "POST" });
    await openConversation(id); await loadConversations();
  });
  const replyBtn = $("replyBtn");
  if (replyBtn) replyBtn.addEventListener("click", async () => {
    const text = $("replyText").value.trim();
    if (!text) return;
    await api("/api/operator/conversations/" + id + "/reply", {
      method: "POST", body: JSON.stringify({ text }),
    });
    await openConversation(id); await loadConversations();
  });
  const resolveBtn = $("resolveBtn");
  if (resolveBtn) resolveBtn.addEventListener("click", async () => {
    const resolution = $("resolutionText").value.trim();
    if (!resolution) { alert("Add a one-line resolution summary — it feeds the learning archive."); return; }
    await api("/api/operator/conversations/" + id + "/resolve", {
      method: "POST", body: JSON.stringify({ resolution }),
    });
    await openConversation(id); await refreshAll();
  });
  const noteBtn = $("noteBtn");
  if (noteBtn) noteBtn.addEventListener("click", async () => {
    const text = $("noteText").value.trim();
    if (!text) return;
    await api("/api/operator/conversations/" + id + "/notes", {
      method: "POST", body: JSON.stringify({ text }),
    });
    await openConversation(id);
  });
  detail.querySelectorAll("[data-att]").forEach((link) => {
    link.addEventListener("click", async (ev) => {
      ev.preventDefault();
      const resp = await fetch("/api/attachments/" + link.dataset.att, {
        headers: { Authorization: "Bearer " + state.token },
      });
      const blob = await resp.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = link.textContent.replace("📎 ", "") || "attachment";
      a.click();
      URL.revokeObjectURL(a.href);
    });
  });
  // Macros + assignment (only while the conversation is live).
  if ($("macroSelect")) {
    try {
      const m = await api("/api/tenants/" + c.tenant_id + "/macros");
      const select = $("macroSelect");
      m.macros.forEach((macro) => {
        const opt = document.createElement("option");
        opt.value = macro.body; opt.textContent = macro.name;
        select.appendChild(opt);
      });
      select.addEventListener("change", () => {
        if (select.value) {
          const box = $("replyText");
          box.value = (box.value ? box.value + "\n" : "") + select.value;
          select.value = "";
        }
      });
    } catch (e) { /* macros are optional */ }
    const ops = await api("/api/operator/operators");
    const assignSelect = $("assignSelect");
    ops.operators.forEach((op) => {
      const opt = document.createElement("option");
      opt.value = op._id;
      opt.textContent = (op.name || op.email) + (op.available === false ? " (away)" : "");
      if (op._id === c.assigned_operator_id) opt.selected = true;
      assignSelect.appendChild(opt);
    });
    $("assignBtn").addEventListener("click", async () => {
      await api("/api/operator/conversations/" + id + "/assign", {
        method: "POST", body: JSON.stringify({ operator_id: assignSelect.value }),
      });
      await openConversation(id); await loadConversations();
    });
  }
}

// ------------------------------------------------------------------- drafts
async function loadDrafts() {
  if (!state.tenantId) return;
  const data = await api("/api/tenants/" + encodeURIComponent(state.tenantId) + "/kb-drafts");
  const list = $("draftList");
  $("draftCount").textContent = data.drafts.length ? "(" + data.drafts.length + ")" : "";
  list.innerHTML = "";
  if (!data.drafts.length) {
    list.innerHTML = '<li class="muted">No pending drafts — resolve an escalation and one appears here.</li>';
    return;
  }
  data.drafts.forEach((d) => {
    const li = document.createElement("li");
    li.innerHTML =
      "<strong>" + esc(d.title) + '</strong> <span class="muted">from escalation · ' +
      esc(d.source) + " draft</span>" +
      '<br><span class="muted">Customer asked: ' + esc(d.question) + "</span>" +
      '<textarea rows="4" class="draftBody">' + esc(d.content) + "</textarea>" +
      '<div class="actions"><button class="draftApprove">Approve & publish</button>' +
      '<button class="ghost draftDiscard">Discard</button></div>';
    li.querySelector(".draftApprove").addEventListener("click", async () => {
      const edited = li.querySelector(".draftBody").value;
      await api("/api/tenants/" + state.tenantId + "/kb-drafts/" + d._id, {
        method: "PATCH", body: JSON.stringify({ content: edited }),
      });
      await api("/api/tenants/" + state.tenantId + "/kb-drafts/" + d._id + "/approve",
                { method: "POST" });
      await loadDrafts();
    });
    li.querySelector(".draftDiscard").addEventListener("click", async () => {
      await api("/api/tenants/" + state.tenantId + "/kb-drafts/" + d._id + "/discard",
                { method: "POST" });
      await loadDrafts();
    });
    list.appendChild(li);
  });
}

// ----------------------------------------------------------------- insights
async function loadInsights() {
  if (!state.tenantId) return;
  const data = await api("/api/operator/analytics?tenant_id=" +
                         encodeURIComponent(state.tenantId));
  const topics = $("topicList");
  topics.innerHTML = "";
  if (!data.topics.length) {
    topics.innerHTML = '<li class="muted">No escalations yet.</li>';
  }
  data.topics.forEach((t) => {
    const li = document.createElement("li");
    li.innerHTML = "<strong>" + esc(t.topic) + "</strong> — " + t.count +
      " escalations (" + Math.round(t.share * 100) + "%), " + t.resolved + " resolved";
    topics.appendChild(li);
  });
  const l = data.learning;
  $("learningStats").innerHTML =
    "<p>Deflection rate: <strong>" +
    (l.deflection_rate != null ? Math.round(l.deflection_rate * 100) + "%" : "—") +
    "</strong> (" + l.ai_resolved + " AI-resolved of " + l.conversations + ")</p>" +
    "<p>Repeat-issue rate: <strong>" +
    (l.repeat_issue_rate != null ? Math.round(l.repeat_issue_rate * 100) + "%" : "—") +
    "</strong></p>" +
    "<p>Trend: " + l.trend.map((d) =>
      esc(d.date) + ": " + d.total + " conv, " + d.escalated + " esc").join(" · ") + "</p>";
  const c = data.ai_cost;
  $("costStats").innerHTML =
    "<p>" + c.ai_messages + " AI messages · ~" + c.est_tokens + " tokens · ~$" +
    c.est_cost_usd + " total" +
    (c.est_cost_per_resolution_usd != null
      ? " · ~$" + c.est_cost_per_resolution_usd + " per resolution" : "") + "</p>";
  const q = await api("/api/operator/quality-reviews?tenant_id=" +
                      encodeURIComponent(state.tenantId) + "&status=queued");
  const qlist = $("qualityList");
  qlist.innerHTML = "";
  if (!q.reviews.length) qlist.innerHTML = '<li class="muted">Queue is empty.</li>';
  q.reviews.forEach((r) => {
    const li = document.createElement("li");
    li.innerHTML = "Score <strong>" + r.score + "</strong> · " +
      esc((r.flags || []).join(", ")) +
      ' <button class="ghost">Mark reviewed</button>';
    li.querySelector("button").addEventListener("click", async () => {
      await api("/api/operator/quality-reviews/" + r._id + "/review", { method: "POST" });
      await loadInsights();
    });
    qlist.appendChild(li);
  });
}

// --------------------------------------------------------------- SLA alerts
async function loadSlaAlerts() {
  if (!state.tenantId) return;
  const data = await api("/api/operator/sla-alerts?tenant_id=" +
                         encodeURIComponent(state.tenantId) + "&acknowledged=false");
  const box = $("slaAlerts");
  if (!data.alerts.length) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  box.innerHTML = "<strong>⚠ SLA alerts</strong>" + data.alerts.map((a) =>
    '<div class="case">' + esc(a.kind.replace(/_/g, " ")) + " · conversation " +
    esc(a.conversation_id) + ' <button class="ghost" data-ack="' + esc(a._id) +
    '">Acknowledge</button></div>').join("");
  box.querySelectorAll("[data-ack]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api("/api/operator/sla-alerts/" + btn.dataset.ack + "/ack", { method: "POST" });
      await loadSlaAlerts();
    });
  });
}

// ------------------------------------------------------------------ archive
async function loadArchive() {
  if (!state.tenantId) return;
  const data = await api("/api/archive?tenant_id=" + encodeURIComponent(state.tenantId));
  const list = $("archiveList");
  list.innerHTML = "";
  if (!data.records.length) {
    list.innerHTML = '<li class="muted">No escalations yet — the archive grows as the AI hands off.</li>';
    return;
  }
  data.records.forEach((r) => {
    const li = document.createElement("li");
    const transcript = r.transcript.map((m) => m.role + ": " + m.text).join("\n");
    li.innerHTML =
      '<span class="badge ' + (r.status === "resolved" ? "resolved" : "escalated") + '">' +
      esc(r.status) + '</span> <strong>' + esc(r.reason.rule) + "</strong> — " +
      esc(r.reason.detail || "") + '<br><span class="muted">' +
      esc(r.escalated_at || "") + "</span>" +
      "<pre class='transcript'>" + esc(transcript) + "</pre>" +
      (r.resolution
        ? '<div><strong>Resolution:</strong> ' + esc(r.resolution.text) +
          ' <span class="muted">by ' + esc(r.resolution.resolved_by) + "</span></div>"
        : '<div class="muted">Awaiting human resolution.</div>');
    list.appendChild(li);
  });
}

async function exportArchive() {
  if (!state.tenantId) return;
  const resp = await fetch("/api/archive/export?tenant_id=" + encodeURIComponent(state.tenantId), {
    headers: { Authorization: "Bearer " + state.token },
  });
  const blob = await resp.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "archive-" + state.tenantId + ".jsonl";
  a.click();
  URL.revokeObjectURL(a.href);
}

// ------------------------------------------------------------------- wiring
$("loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email: $("loginEmail").value, password: $("loginPassword").value,
      }),
    });
    state.token = data.token;
    localStorage.setItem("hub-token", data.token);
    $("loginError").textContent = "";
    await boot();
  } catch (e) {
    $("loginError").textContent = e.message;
  }
});
$("logoutBtn").addEventListener("click", logout);
$("availabilityBtn").addEventListener("click", async () => {
  const available = !(state.operator && state.operator.available !== false);
  const data = await api("/api/operator/me/availability", {
    method: "POST", body: JSON.stringify({ available }),
  });
  state.operator = data.operator;
  updateAvailabilityBtn();
});
$("tenantSelect").addEventListener("change", (ev) => {
  state.tenantId = ev.target.value;
  refreshAll();
});
$("statusFilter").addEventListener("change", loadConversations);
$("refreshBtn").addEventListener("click", refreshAll);
$("exportBtn").addEventListener("click", exportArchive);
$("draftRefreshBtn").addEventListener("click", loadDrafts);
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".tabpane").forEach((p) => p.classList.add("hidden"));
    $("tab-" + btn.dataset.tab).classList.remove("hidden");
  });
});

boot();
