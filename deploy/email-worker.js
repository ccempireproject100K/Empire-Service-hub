// service-hub-email — Cloudflare Email Worker for the Empire Service Hub.
//
// Cloudflare Email Routing hands mail for support@mindateasesolutions.com
// to this worker. It parses the message (headers + best text/plain body)
// and POSTs it to the hub's email channel webhook:
//
//   POST {HUB_BASE_URL}/api/channels/email/inbound?tenant_id={HUB_TENANT_ID}
//   Header: X-Hub-Channel-Secret: {HUB_CHANNEL_SECRET}
//
// The hub (backend/routers/channels.py) creates/continues a conversation
// per sender and runs the same AI pipeline as the widget. Outbound replies
// leave through the hub's own mailer (SMTP), never through this worker.
//
// Bindings (set at deploy time, values never in this file):
//   HUB_BASE_URL        e.g. https://chat.mindateasesolutions.com
//   HUB_TENANT_ID       the hub tenant that owns the support inbox
//   HUB_CHANNEL_SECRET  secret_text binding; must match the hub env var
//
// Wiring reference: deploy/WIRING.md in this repo.

function decodeTransfer(bytes, encoding) {
  const enc = (encoding || "").trim().toLowerCase();
  if (enc === "base64") {
    let b64 = new TextDecoder("latin1").decode(bytes).replace(/\s+/g, "");
    b64 = b64.padEnd(Math.ceil(b64.length / 4) * 4, "=");
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  if (enc === "quoted-printable") {
    const text = new TextDecoder("latin1").decode(bytes);
    const out = [];
    for (let i = 0; i < text.length; i++) {
      if (text[i] === "=" && i + 2 < text.length + 0 && /^[0-9A-Fa-f]{2}$/.test(text.slice(i + 1, i + 3))) {
        out.push(parseInt(text.slice(i + 1, i + 3), 16));
        i += 2;
      } else if (text[i] === "=" && (text[i + 1] === "\r" || text[i + 1] === "\n")) {
        // soft line break: skip the = and the line break
        if (text[i + 1] === "\r" && text[i + 2] === "\n") i += 2; else i += 1;
      } else {
        out.push(text.charCodeAt(i) & 0xff);
      }
    }
    return new Uint8Array(out);
  }
  return bytes;
}

function decodeCharset(bytes, charset) {
  const cs = (charset || "utf-8").trim().toLowerCase();
  try {
    return new TextDecoder(cs).decode(bytes);
  } catch {
    return new TextDecoder("utf-8").decode(bytes);
  }
}

function decodeEncodedWords(value) {
  return value.replace(/=\?([^?]+)\?([bBqQ])\?([^?]*)\?=/g, (m, charset, enc, text) => {
    let bytes;
    if (enc.toLowerCase() === "b") {
      const bin = atob(text);
      bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    } else {
      const raw = text.replace(/_/g, " ");
      const arr = [];
      for (let i = 0; i < raw.length; i++) {
        if (raw[i] === "=" && /^[0-9A-Fa-f]{2}$/.test(raw.slice(i + 1, i + 3))) {
          arr.push(parseInt(raw.slice(i + 1, i + 3), 16));
          i += 2;
        } else {
          arr.push(raw.charCodeAt(i) & 0xff);
        }
      }
      bytes = new Uint8Array(arr);
    }
    return decodeCharset(bytes, charset);
  });
}

function splitHeadBody(text) {
  const idx = text.search(/\r\n\r\n|\n\n/);
  if (idx === -1) return { head: text, body: "" };
  const sep = text.slice(idx).startsWith("\r\n") ? "\r\n\r\n" : "\n\n";
  return { head: text.slice(0, idx), body: text.slice(idx + sep.length) };
}

function parseHeaders(head) {
  const headers = {};
  const unfolded = head.replace(/\r?\n[ \t]+/g, " ");
  for (const line of unfolded.split(/\r?\n/)) {
    const colon = line.indexOf(":");
    if (colon > 0) {
      headers[line.slice(0, colon).trim().toLowerCase()] = line.slice(colon + 1).trim();
    }
  }
  return headers;
}

function parseContentType(value) {
  const parts = (value || "text/plain").split(";");
  const type = parts[0].trim().toLowerCase();
  const params = {};
  for (const part of parts.slice(1)) {
    const eq = part.indexOf("=");
    if (eq > 0) {
      params[part.slice(0, eq).trim().toLowerCase()] =
        part.slice(eq + 1).trim().replace(/^"|"$/g, "");
    }
  }
  return { type, params };
}

// Walk a MIME entity (already transfer-decoded bytes are NOT assumed:
// `rawText` is the latin1 view of the entity body) and return the best
// text/plain body as a JS string.
function extractText(headers, rawBytes) {
  const { type, params } = parseContentType(headers["content-type"]);
  if (type.startsWith("multipart/") && params.boundary) {
    const boundary = params.boundary;
    const view = new TextDecoder("latin1").decode(rawBytes);
    const segments = view.split("--" + boundary);
    let fallbackHtml = null;
    for (let i = 1; i < segments.length; i++) {
      let seg = segments[i];
      if (seg.startsWith("--")) break; // closing delimiter
      if (seg.startsWith("\r\n")) seg = seg.slice(2);
      else if (seg.startsWith("\n")) seg = seg.slice(1);
      const { head, body } = splitHeadBody(seg);
      const segHeaders = parseHeaders(head);
      // body is a latin1 view (char code == byte value); recover the bytes
      const segBytes = new Uint8Array([...body].map((c) => c.charCodeAt(0) & 0xff));
      const found = extractText(segHeaders, segBytes);
      if (found && found.kind === "text") return found;
      if (found && found.kind === "html" && !fallbackHtml) fallbackHtml = found;
    }
    return fallbackHtml;
  }
  const decoded = decodeTransfer(rawBytes, headers["content-transfer-encoding"]);
  if (type === "text/plain" || type === "") {
    return { kind: "text", text: decodeCharset(decoded, params.charset) };
  }
  if (type === "text/html") {
    const html = decodeCharset(decoded, params.charset);
    const text = html
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/p>/gi, "\n\n")
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;/g, " ")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/[ \t]+/g, " ")
      .trim();
    return { kind: "html", text };
  }
  return null;
}

function parseMessage(rawBytes) {
  // Headers are ASCII-safe; view the whole message as latin1 to split
  // head/body byte-faithfully, then re-encode the body for decoding.
  const view = new TextDecoder("latin1").decode(rawBytes);
  const { head, body } = splitHeadBody(view);
  const headers = parseHeaders(head);
  const bodyBytes = new Uint8Array([...body].map((c) => c.charCodeAt(0) & 0xff));
  const found = extractText(headers, bodyBytes);
  const fromHeader = decodeEncodedWords(headers["from"] || "");
  const addrMatch = fromHeader.match(/<([^>]+)>/);
  return {
    from: (addrMatch ? addrMatch[1] : fromHeader).trim(),
    subject: decodeEncodedWords(headers["subject"] || ""),
    text: (found ? found.text : "").trim().slice(0, 20000),
    messageId: headers["message-id"] || null,
  };
}

export default {
  async email(message, env, ctx) {
    const raw = new Uint8Array(await new Response(message.raw).arrayBuffer());
    let parsed;
    try {
      parsed = parseMessage(raw);
    } catch (err) {
      console.log("service-hub-email: parse failed:", String(err));
      message.setReject("Could not parse message");
      return;
    }
    const payload = {
      from: parsed.from || message.from,
      subject: parsed.subject || null,
      body: parsed.text && parsed.text.trim() ? parsed.text : "(no readable text body)",
      external_ref: parsed.messageId,
    };
    const url =
      env.HUB_BASE_URL.replace(/\/$/, "") +
      "/api/channels/email/inbound?tenant_id=" +
      encodeURIComponent(env.HUB_TENANT_ID);
    let resp;
    try {
      resp = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Hub-Channel-Secret": env.HUB_CHANNEL_SECRET,
        },
        body: JSON.stringify(payload),
      });
    } catch (err) {
      console.log("service-hub-email: hub unreachable:", String(err));
      message.setReject("Support hub unreachable, please retry");
      return;
    }
    if (!resp.ok) {
      console.log("service-hub-email: hub returned", resp.status);
      message.setReject("Support hub error, please retry");
      return;
    }
    const data = await resp.json().catch(() => ({}));
    console.log(
      "service-hub-email: delivered to hub, conversation",
      data.conversation && data.conversation.id ? data.conversation.id : "(unknown)",
      "created:", data.created
    );
  },
};
