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
    await refreshAll();
  } catch (e) {
    logout();
  }
}

async function refreshAll() {
  await Promise.all([loadStats(), loadConversations(), loadArchive()]);
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
    (m.demo ? " · demo AI" : "") + "</div>" + esc(m.text) + "</div>"
  ).join("");
  const escInfo = c.escalation
    ? '<p><span class="badge escalated">escalated: ' + esc(c.escalation.rule) + "</span> " +
      esc(c.escalation.detail || "") + "</p>"
    : "";
  detail.innerHTML =
    "<h3>" + esc(c.customer_name || "Customer") + ' <span class="badge ' + esc(c.status) + '">' +
    esc(c.status.replace("_", " ")) + "</span></h3>" + escInfo +
    '<div id="msgThread">' + msgs + "</div>" +
    (c.status !== "resolved"
      ? '<div class="replyrow"><textarea id="replyText" rows="2" ' +
        'placeholder="Reply as the human on this conversation…"></textarea></div>' +
        '<div class="actions">' +
        '<button id="takeoverBtn">Take over</button>' +
        '<button id="replyBtn">Send reply</button>' +
        '<button id="resolveBtn">Resolve</button>' +
        '<input id="resolutionText" placeholder="Resolution summary (goes to the archive)" size="42">' +
        "</div>"
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
$("tenantSelect").addEventListener("change", (ev) => {
  state.tenantId = ev.target.value;
  refreshAll();
});
$("statusFilter").addEventListener("change", loadConversations);
$("refreshBtn").addEventListener("click", refreshAll);
$("exportBtn").addEventListener("click", exportArchive);
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".tabpane").forEach((p) => p.classList.add("hidden"));
    $("tab-" + btn.dataset.tab).classList.remove("hidden");
  });
});

boot();
