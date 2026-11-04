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
    // v2: tenant status banner (incident notices) rides atop the panel.
    var bannerCfg = (cfg.proactive || {}).status_banner;
    var banner = null;
    if (bannerCfg && bannerCfg.active && bannerCfg.text) {
      banner = el("div",
        "background:#fef3c7;color:#92400e;padding:8px 12px;font-size:12px;border-bottom:1px solid #fde68a",
        "⚠ " + bannerCfg.text);
    }
    var messages = el("div", "flex:1;overflow-y:auto;padding:12px;background:#f8fafc");
    var csatBox = el("div", "display:none;padding:10px;background:#fffbeb;border-top:1px solid #fde68a");
    var form = el("form", "display:flex;gap:8px;padding:10px;border-top:1px solid #e5e7eb");
    var input = el("input", "flex:1;padding:9px;border:1px solid #d1d5db;border-radius:8px");
    input.placeholder = "Type your message…";
    var send = el("button",
      "border:none;border-radius:8px;padding:9px 14px;color:#fff;cursor:pointer;background:" + primary,
      "Send");
    // v2: attachments — upload on pick, send the id with the next message.
    var attachBtn = el("button",
      "border:1px solid #d1d5db;border-radius:8px;padding:9px 10px;background:#fff;cursor:pointer",
      "📎");
    attachBtn.type = "button";
    attachBtn.title = "Attach a file";
    var fileInput = document.createElement("input");
    fileInput.type = "file"; fileInput.style.display = "none";
    var pendingAttachmentIds = [];
    attachBtn.addEventListener("click", function () {
      if (!conversationId) { ensureConversation(); }
      fileInput.click();
    });
    fileInput.addEventListener("change", function () {
      var file = fileInput.files[0];
      if (!file || !conversationId) return;
      var reader = new FileReader();
      reader.onload = function () {
        var dataB64 = String(reader.result).split(",")[1] || "";
        api("/api/widget/conversations/" + conversationId + "/attachments", {
          method: "POST",
          body: JSON.stringify({
            filename: file.name, content_type: file.type || null, data_base64: dataB64,
          }),
        }).then(function (data) {
          pendingAttachmentIds.push(data.attachment._id);
          attachBtn.textContent = "📎✓";
        }).catch(function () { attachBtn.textContent = "📎✗"; });
      };
      reader.readAsDataURL(file);
    });
    // Voice standard (2026-11-01): tap-to-talk dictation on the
    // composer — the widget's one public free-text field (CSAT stars
    // and the attach picker stay taps; there are no password fields
    // here). ONE guided "Fill it in by talking" control walks the
    // intake: it asks for the message on screen and aloud (speech
    // synthesis where the browser provides it), listens, fills the
    // composer, and ends — Send stays a manual tap, nothing
    // auto-submits, ever. A small mic beside the composer remains as
    // a correction tool. Audio is processed by the browser (Apple or
    // Google); the hub receives only the text the visitor keeps, and
    // nothing is recorded or stored. Where the browser has no speech
    // recognition, no dictation control renders. Recognition
    // language = the widget's active language: the tenant's first
    // configured language, which the conversation also starts in.
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    var REC_LANG = { en: "en-US", es: "es-ES", fr: "fr-FR", zh: "zh-CN" };
    var DICTATE = {
      en: { walk: "🎤 Fill it in by talking", listening: "Listening… speak now", stop: "Stop",
        prompt: "What do you need help with? Say your message.",
        hint: "Say “next” or “done” to finish — then tap Send.",
        dictate: "Dictate into this field",
        privacy: "Your browser (Apple or Google) turns speech into text. The business only receives the text you keep.",
        denied: "Microphone access is blocked — you can type instead.",
        error: "Voice typing stopped. Tap to try again, or type instead.",
        next: ["next"], skip: ["skip"], back: ["go back", "back"],
        stopCmd: ["stop"], done: ["done", "finished"] },
      es: { walk: "🎤 Complétalo hablando", listening: "Escuchando… habla ahora", stop: "Detener",
        prompt: "¿En qué necesitas ayuda? Di tu mensaje.",
        hint: "Di «listo» para terminar y luego toca Enviar.",
        dictate: "Dictar en este campo",
        privacy: "Tu navegador (Apple o Google) convierte la voz en texto. El negocio solo recibe el texto que conservas.",
        denied: "El acceso al micrófono está bloqueado: puedes escribir en su lugar.",
        error: "El dictado por voz se detuvo. Toca para intentarlo de nuevo, o escribe.",
        next: ["siguiente"], skip: ["saltar", "omitir"], back: ["atrás", "volver"],
        stopCmd: ["detener", "parar"], done: ["listo", "terminé", "hecho"] },
      fr: { walk: "🎤 Remplissez-le en parlant", listening: "Écoute… parlez maintenant", stop: "Arrêter",
        prompt: "De quoi avez-vous besoin ? Dites votre message.",
        hint: "Dites « terminé » pour finir, puis touchez Envoyer.",
        dictate: "Dicter dans ce champ",
        privacy: "Votre navigateur (Apple ou Google) transforme la parole en texte. L'entreprise ne reçoit que le texte que vous conservez.",
        denied: "L'accès au microphone est bloqué — vous pouvez taper à la place.",
        error: "La saisie vocale s'est arrêtée. Touchez pour réessayer, ou tapez.",
        next: ["suivant"], skip: ["passer", "sauter"], back: ["retour", "revenir"],
        stopCmd: ["stop", "arrêter"], done: ["terminé", "fini"] },
      zh: { walk: "🎤 说着填", listening: "正在聆听……请说话", stop: "停止",
        prompt: "您需要什么帮助？请说出您的留言。",
        hint: "说“完成”结束，然后点按发送。",
        dictate: "在此字段中口述",
        privacy: "您的浏览器（Apple 或 Google）将语音转为文字。商家只会收到您保留的文字。",
        denied: "麦克风访问被阻止，您可以改为打字。",
        error: "语音输入已停止。点按重试，或改为打字。",
        next: ["下一个"], skip: ["跳过"], back: ["返回", "上一个"],
        stopCmd: ["停止"], done: ["完成", "好了"] },
    };
    function dictateLang() {
      var langs = (config && config.tenant && config.tenant.languages) || ["en"];
      var first = String(langs[0] || "en").toLowerCase();
      return DICTATE[first] ? first : "en";
    }
    function dstr() { return DICTATE[dictateLang()]; }
    var micBtn = null;
    var dictateBar = null;
    if (SR) {
      micBtn = el("button",
        "border:1px solid #d1d5db;border-radius:8px;padding:9px 10px;background:#fff;cursor:pointer",
        "🎤");
      micBtn.type = "button";
      micBtn.setAttribute("aria-label", dstr().dictate);
      micBtn.title = dstr().dictate;
      dictateBar = el("div",
        "display:flex;flex-direction:column;gap:4px;padding:8px 10px;border-top:1px solid #e5e7eb;background:#fff");
      var guidedBtn = el("button",
        "align-self:flex-start;border:1px solid " + primary + ";border-radius:8px;" +
        "padding:7px 12px;background:#fff;cursor:pointer;font-weight:600;color:" + primary,
        dstr().walk);
      guidedBtn.type = "button";
      var walkStatus = el("div", "display:none;align-items:center;gap:8px");
      var recDot = el("span",
        "width:9px;height:9px;border-radius:999px;background:#dc2626;display:inline-block;flex:none");
      var walkText = el("span", "");
      var walkStop = el("button",
        "border:1px solid #d1d5db;border-radius:8px;padding:5px 10px;background:#fff;cursor:pointer",
        dstr().stop);
      walkStop.type = "button";
      walkStatus.appendChild(recDot); walkStatus.appendChild(walkText);
      walkStatus.appendChild(walkStop);
      var interimLine = el("p", "margin:0;color:#6b7280;font-size:12px", "");
      var noteLine = el("p", "margin:0;color:#6b7280;font-size:11px", dstr().privacy);
      dictateBar.appendChild(guidedBtn); dictateBar.appendChild(walkStatus);
      dictateBar.appendChild(interimLine); dictateBar.appendChild(noteLine);

      var rec = null;
      var recMode = null; // "walk" | "single"
      var walkActive = false;

      function speakPrompt(text) {
        // Spoken prompts degrade silently: no synthesis (or a
        // failure) leaves the on-screen prompt alone in charge.
        try {
          if (!("speechSynthesis" in window)) return;
          window.speechSynthesis.cancel();
          var utterance = new SpeechSynthesisUtterance(text);
          utterance.lang = REC_LANG[dictateLang()] || "en-US";
          window.speechSynthesis.speak(utterance);
        } catch (e) {}
      }
      function stopSpeaking() {
        try { window.speechSynthesis.cancel(); } catch (e) {}
      }
      function appendDictated(text) {
        var value = input.value || "";
        input.value = value && !/\s$/.test(value) ? value + " " + text : value + text;
      }
      function matchCommand(text) {
        var norm = String(text).trim().toLowerCase().replace(/[。.!¡¿?]+$/, "");
        var s = dstr();
        var groups = [["done", s.done], ["stop", s.stopCmd], ["back", s.back],
                      ["skip", s.skip], ["next", s.next]];
        for (var i = 0; i < groups.length; i++) {
          for (var j = 0; j < groups[i][1].length; j++) {
            if (norm === String(groups[i][1][j]).toLowerCase()) return groups[i][0];
          }
        }
        return null;
      }
      function highlightComposer(on) {
        input.style.boxShadow = on ? "0 0 0 2px " + primary : "";
        if (on) { try { input.focus(); } catch (e) {} }
      }
      function endWalk() {
        walkActive = false;
        if (rec) { var r = rec; rec = null; recMode = null; try { r.stop(); } catch (e) {} }
        stopSpeaking();
        highlightComposer(false);
        interimLine.textContent = "";
        walkStatus.style.display = "none";
        guidedBtn.style.display = "";
        noteLine.textContent = dstr().privacy;
      }
      function endSingle() {
        if (rec) { var r = rec; rec = null; recMode = null; try { r.stop(); } catch (e) {} }
        micBtn.textContent = "🎤";
        micBtn.setAttribute("aria-label", dstr().dictate);
      }
      function handleFinal(text, mode) {
        if (mode === "single") { appendDictated(text); return; }
        var command = matchCommand(text);
        if (command === "back") {
          // One field in this walk: "go back" re-asks for it.
          speakPrompt(dstr().prompt);
          walkText.textContent = dstr().prompt + " " + dstr().listening;
          return;
        }
        if (command) { endWalk(); return; } // next/skip/stop/done end the one-field walk
        appendDictated(text);
        endWalk();
      }
      function startRecognition(mode) {
        if (!SR || rec) return;
        var r = new SR();
        rec = r; recMode = mode;
        r.lang = REC_LANG[dictateLang()] || "en-US";
        r.continuous = true;
        r.interimResults = true;
        r.onresult = function (event) {
          var finished = "";
          var pending = "";
          for (var i = event.resultIndex; i < event.results.length; i++) {
            var transcript = event.results[i][0].transcript;
            if (event.results[i].isFinal) finished += transcript;
            else pending += transcript;
          }
          if (mode === "walk") interimLine.textContent = pending;
          if (finished.trim()) handleFinal(finished.trim(), mode);
        };
        r.onerror = function (event) {
          var denied = event.error === "not-allowed" || event.error === "service-not-allowed";
          if (mode === "walk") {
            endWalk();
            noteLine.textContent = denied ? dstr().denied : dstr().error;
          } else {
            endSingle();
          }
        };
        r.onend = function () {
          if (rec !== r) return;
          rec = null; recMode = null;
          if (mode === "walk" && walkActive) {
            // The browser ended listening on its own (a pause): the
            // walk advances gracefully — past this one field, that
            // ends the session rather than trapping the visitor.
            endWalk();
          }
          if (mode === "single") {
            micBtn.textContent = "🎤";
            micBtn.setAttribute("aria-label", dstr().dictate);
          }
        };
        try { r.start(); } catch (e) {
          rec = null; recMode = null;
          if (mode === "walk") endWalk(); else endSingle();
        }
      }
      guidedBtn.addEventListener("click", function () {
        if (walkActive || rec) return;
        walkActive = true;
        guidedBtn.style.display = "none";
        walkStatus.style.display = "flex";
        noteLine.textContent = dstr().hint;
        walkText.textContent = dstr().prompt + " " + dstr().listening;
        highlightComposer(true);
        speakPrompt(dstr().prompt);
        startRecognition("walk");
      });
      walkStop.addEventListener("click", endWalk);
      micBtn.addEventListener("click", function () {
        if (rec && recMode === "single") { endSingle(); return; }
        if (rec) return; // a walk is running; its Stop control ends it
        micBtn.textContent = "⏹";
        micBtn.setAttribute("aria-label", dstr().stop);
        startRecognition("single");
      });
    }
    form.appendChild(attachBtn); form.appendChild(fileInput);
    form.appendChild(input);
    if (micBtn) form.appendChild(micBtn);
    form.appendChild(send);
    panel.appendChild(header);
    if (banner) panel.appendChild(banner);
    panel.appendChild(messages);
    panel.appendChild(csatBox);
    if (dictateBar) panel.appendChild(dictateBar);
    panel.appendChild(form);
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
      (msg.attachments || []).forEach(function (att) {
        wrap.appendChild(el("div", "font-size:12px;margin-top:2px;opacity:.8",
          "📎 " + att.filename));
      });
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
      var attachmentIds = pendingAttachmentIds;
      pendingAttachmentIds = [];
      attachBtn.textContent = "📎";
      api("/api/widget/conversations/" + conversationId + "/messages", {
        method: "POST",
        body: JSON.stringify({ text: text, attachment_ids: attachmentIds }),
      }).then(function (data) {
        data.messages.forEach(bubble);
        if (data.conversation.status === "resolved") showCsatOnce();
      }).catch(function () {});
    });

    function openPanel() {
      if (panel.style.display !== "none") return;
      panel.style.display = "flex";
      ensureConversation();
      clearInterval(pollTimer);
      pollTimer = setInterval(refresh, 4000);
    }

    launcher.addEventListener("click", function () {
      var open = panel.style.display !== "none";
      if (open) { panel.style.display = "none"; } else { openPanel(); }
    });

    // v2: proactive help — offer assistance before the customer asks,
    // per the tenant's configured triggers (time on page / specific paths).
    var proactiveFired = false;
    function fireProactive(trigger) {
      if (proactiveFired) return;
      proactiveFired = true;
      openPanel();
      var note = el("div",
        "margin:6px 0;text-align:left", null);
      var b = el("div",
        "display:inline-block;max-width:85%;padding:8px 11px;border-radius:12px;" +
        "background:#eef2ff;border:1px solid #c7d2fe;white-space:pre-wrap",
        trigger.message || "Hi there — need a hand with anything?");
      note.appendChild(b);
      messages.appendChild(note);
      messages.scrollTop = messages.scrollHeight;
    }
    ((cfg.proactive || {}).triggers || []).forEach(function (trigger) {
      if (trigger.type === "time_on_page") {
        setTimeout(function () { fireProactive(trigger); },
                   (trigger.seconds || 30) * 1000);
      } else if (trigger.type === "page_path" && trigger.path &&
                 window.location.pathname.indexOf(trigger.path) !== -1) {
        setTimeout(function () { fireProactive(trigger); }, 1200);
      }
    });
  }

  api("/api/widget/config").then(init).catch(function (e) {
    console.error("[service-hub] failed to load config", e);
  });
})();
