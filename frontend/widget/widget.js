/* Service Hub embeddable widget.
 *
 * Usage on any product page:
 *   <script src="https://hub.example.com/widget.js"
 *           data-hub-key="pk_..."            (tenant public key)
 *           data-hub-base="https://hub.example.com"  (optional)
 *           async></script>
 *
 * The widget fetches the tenant's branding from the hub, renders a themed
 * chat panel, and talks to the hub API. Conversations resume across page
 * loads via localStorage (keyed by tenant public key).
 */
(function () {
  "use strict";
  var script = document.currentScript;
  var key = script && script.getAttribute("data-hub-key");
  if (!key) { console.error("[service-hub] data-hub-key is required"); return; }
  var base = (script.getAttribute("data-hub-base") || "").replace(/\/$/, "");
  var storageKey = "hub-conversation-" + key;
  var conversationId = null;
  try { conversationId = localStorage.getItem(storageKey); } catch (e) {}
  var config = null;
  var pollTimer = null;
  var seenMessages = {};

  function api(path, options) {
    options = options || {};
    options.headers = Object.assign(
      { "Content-Type": "application/json", "X-Hub-Public-Key": key },
      options.headers || {}
    );
    return fetch(base + path, options).then(function (r) {
      if (!r.ok) throw new Error("hub " + r.status);
      return r.json();
    });
  }

  function el(tag, css, text) {
    var node = document.createElement(tag);
    if (css) node.style.cssText = css;
    if (text != null) node.textContent = text;
    return node;
  }

  function init(cfg) {
    config = cfg;
    var brand = cfg.tenant.branding;
    var primary = brand.primary_color || "#2563eb";

    var launcher = el("button",
      "position:fixed;right:20px;bottom:20px;z-index:99990;border:none;cursor:pointer;" +
      "border-radius:999px;padding:12px 18px;color:#fff;font:600 14px system-ui;" +
      "box-shadow:0 6px 24px rgba(0,0,0,.25);background:" + primary,
      "Chat with us");
    document.body.appendChild(launcher);

    var panel = el("div",
      "position:fixed;right:20px;bottom:76px;z-index:99991;width:340px;max-width:92vw;" +
      "height:480px;max-height:70vh;display:none;flex-direction:column;background:#fff;" +
      "border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.3);overflow:hidden;" +
      "font:14px system-ui;color:#111");
    var header = el("div", "background:" + primary + ";color:#fff;padding:12px 14px;font-weight:600",
      brand.name || cfg.tenant.name);
    var demoTag = cfg.demo ? el("span", "font-weight:400;font-size:11px;opacity:.85", "  · demo AI") : null;
    if (demoTag) header.appendChild(demoTag);
    var messages = el("div", "flex:1;overflow-y:auto;padding:12px;background:#f8fafc");
    var csatBox = el("div", "display:none;padding:10px;background:#fffbeb;border-top:1px solid #fde68a");
    var form = el("form", "display:flex;gap:8px;padding:10px;border-top:1px solid #e5e7eb");
    var input = el("input", "flex:1;padding:9px;border:1px solid #d1d5db;border-radius:8px");
    input.placeholder = "Type your message…";
    var send = el("button",
      "border:none;border-radius:8px;padding:9px 14px;color:#fff;cursor:pointer;background:" + primary,
      "Send");
    form.appendChild(input); form.appendChild(send);
    panel.appendChild(header); panel.appendChild(messages);
    panel.appendChild(csatBox); panel.appendChild(form);
    document.body.appendChild(panel);

    function bubble(msg) {
      if (seenMessages[msg._id]) return;
      seenMessages[msg._id] = true;
      var mine = msg.role === "customer";
      var wrap = el("div", "margin:6px 0;text-align:" + (mine ? "right" : "left"));
      var b = el("div",
        "display:inline-block;max-width:85%;padding:8px 11px;border-radius:12px;white-space:pre-wrap;" +
        (mine ? "background:" + primary + ";color:#fff"
              : msg.role === "system" ? "background:#e5e7eb;color:#374151;font-size:12px"
              : "background:#fff;border:1px solid #e5e7eb"),
        msg.text);
      wrap.appendChild(b);
      messages.appendChild(wrap);
      messages.scrollTop = messages.scrollHeight;
    }

    function showCsat() {
      csatBox.style.display = "block";
      csatBox.innerHTML = "";
      csatBox.appendChild(el("div", "margin-bottom:6px", "How did we do?"));
      [1, 2, 3, 4, 5].forEach(function (n) {
        var star = el("button",
          "border:none;background:none;cursor:pointer;font-size:20px;color:#f59e0b", "★");
        star.title = n + " / 5";
        star.addEventListener("click", function () {
          api("/api/widget/conversations/" + conversationId + "/csat", {
            method: "POST", body: JSON.stringify({ rating: n }),
          }).then(function () {
            csatBox.innerHTML = "Thanks for the feedback!";
          }).catch(function () {
            csatBox.innerHTML = "Thanks! (feedback already recorded)";
          });
        });
        csatBox.appendChild(star);
      });
    }

    function refresh() {
      if (!conversationId) return;
      api("/api/widget/conversations/" + conversationId + "/messages")
        .then(function (data) {
          data.messages.forEach(bubble);
          if (data.conversation.status === "resolved") showCsatOnce();
        })
        .catch(function () {});
    }
    var csatShown = false;
    function showCsatOnce() { if (!csatShown) { csatShown = true; showCsat(); } }

    function ensureConversation() {
      if (conversationId) { refresh(); return; }
      api("/api/widget/conversations", { method: "POST", body: JSON.stringify({}) })
        .then(function (data) {
          conversationId = data.conversation._id;
          try { localStorage.setItem(storageKey, conversationId); } catch (e) {}
          data.messages.forEach(bubble);
        })
        .catch(function () {});
    }

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var text = input.value.trim();
      if (!text || !conversationId) return;
      input.value = "";
      api("/api/widget/conversations/" + conversationId + "/messages", {
        method: "POST", body: JSON.stringify({ text: text }),
      }).then(function (data) {
        data.messages.forEach(bubble);
        if (data.conversation.status === "resolved") showCsatOnce();
      }).catch(function () {});
    });

    launcher.addEventListener("click", function () {
      var open = panel.style.display !== "none";
      panel.style.display = open ? "none" : "flex";
      if (!open) {
        ensureConversation();
        clearInterval(pollTimer);
        pollTimer = setInterval(refresh, 4000);
      }
    });
  }

  api("/api/widget/config").then(init).catch(function (e) {
    console.error("[service-hub] failed to load config", e);
  });
})();
