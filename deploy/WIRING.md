# Service Hub — production wiring

How the hub is wired live (done 2026-11-14; full evidence report in the
empire buildout records, `buildout/service-hub-wiring/REPORT.md`).

## Public route

- The hub runs as a Render web service (`service-hub`, free plan, Oregon)
  from this repo's `main` branch — the same pattern as the empire's other
  services. Build: `cd backend && pip install -r requirements.txt`;
  start: `cd backend && uvicorn server:app --host 0.0.0.0 --port $PORT`;
  health check `/api/healthz`.
- Public name: **https://chat.mindateasesolutions.com** (Cloudflare CNAME
  to the service's `onrender.com` URL, custom domain registered on the
  service). The `onrender.com` URL keeps working as a fallback.
- The older Chatwoot desk on the Docker host is a separate system; this
  hub is the AI-first desk the products embed (widget) and write to
  (channels).

## Live environment (names only — values live in the Render dashboard)

| Variable | State |
|---|---|
| `HUB_ENV` | `production` (fail-closed guards on) |
| `HUB_JWT_SECRET` | set (generated at wiring time) |
| `HUB_CHANNEL_SECRET` | set (generated; the email worker carries the same value as a secret binding) |
| `MONGO_URL` | set — the empire's shared Atlas cluster; the hub uses its own `DB_NAME=service_hub` database on it |
| `DB_NAME` | `service_hub` |
| `PYTHON_VERSION` | `3.11.9` |

Key-gated and **not yet set** (honest fallbacks until they are):

- `SMTP_HOST/PORT/USERNAME/PASSWORD/FROM` — the Resend SMTP lane
  (`smtp.resend.com`, user `resend`, password = a Resend API key,
  from `support@mindateasesolutions.com`). Until set, outbound email
  replies queue in the hub's outbox, visible to operators.
- `GEMINI_API_KEY` or `OPENAI_API_KEY` — until set, the AI front line
  runs in demo mode (deterministic KB answers, flagged `demo`).

## Email feed — support@mindateasesolutions.com

1. Cloudflare Email Routing is **enabled** on the `mindateasesolutions.com`
   zone (MX records point at Cloudflare).
2. A routing rule matches `support@mindateasesolutions.com` and hands the
   message to the **service-hub-email** Worker (`deploy/email-worker.js`).
3. The worker parses the message and POSTs it to
   `POST /api/channels/email/inbound?tenant_id=<books tenant id>` with the
   `X-Hub-Channel-Secret` header. The hub creates/continues a conversation
   per sender; the AI reply is recorded on the conversation and queued in
   the outbox (or sent, once SMTP above is set).

Worker bindings: `HUB_BASE_URL`, `HUB_TENANT_ID` (plain), and
`HUB_CHANNEL_SECRET` (secret). To redeploy the worker after editing:
upload `deploy/email-worker.js` as a module worker named
`service-hub-email` with the same bindings (Cloudflare API or dashboard).

The old Workspace address `support@theaiempireproject.com` is **not**
wired into the hub: that mailbox lives in Google Workspace, which is
slated for cancellation, and Workspace mail cannot feed the hub webhook
without a separate bridge. `support@mindateasesolutions.com` is the
support address.

## Bookkeeping connection

- The bookkeeping API carries `SUPPORT_DESK_URL=https://chat.mindateasesolutions.com`.
  The fold-in build's payment-help door (`/c/{token}/payment-help` in
  `backend/routers/invoice_delivery.py`) records its ticket in Books and
  returns this URL as the desk deep link (`?ticket=<id>`). The env var is
  already set on the live API; it takes effect when the fold-in branch
  deploys (the live `main` code does not read the variable yet).

## Phone / voice — prep state

See the wiring report's phone section for the full checklist. In short:
Fonoster (on the Docker host) answers a rented SignalWire number and
hands the call summary + transcript to
`POST /api/channels/voice/transcript` with the channel secret — the hub
lands it escalated in the queue + archive. No number is rented yet;
renting one and the Fonoster credential authorization are owner taps.
