# Deploying the Service Hub in production

The hub is **fail-closed in production**: with `HUB_ENV=production` the
app refuses to boot on the demo JWT secret, and the seed script refuses
to create an operator with the demo credentials. Both defaults are
public (they are printed in the code and the README), so a deployment
that skipped them would be open to anyone. Set the environment below
*before* the first boot and the first seed.

## Required — the app will not run production without these

| Variable | Value |
|---|---|
| `HUB_ENV` | `production` — turns on the fail-closed guards below. Any other value (or unset) is the local/demo posture: the app boots with a loud warning instead of refusing. |
| `HUB_JWT_SECRET` | A long random value of your own (e.g. `openssl rand -hex 32`). In production the app **refuses to boot** if this is unset or still `demo-secret-change-me`. |
| `MONGO_URL` | Your MongoDB Atlas connection string. The code default is `mongodb://localhost:27017`, which does not exist on a hosted deploy. |
| `DB_NAME` | Optional — defaults to `service_hub`. |
| `PORT` | Set by the host (Render sets it). Defaults to `8000` for `python server.py`. |

## Seed the operator account (one time)

Operators exist **only** via the seed script — there is no signup or
registration flow anywhere in the product.

```bash
cd backend
HUB_ENV=production \
HUB_ADMIN_EMAIL=<the operator's real email> \
HUB_ADMIN_PASSWORD=<a real password, chosen by the owner> \
MONGO_URL=<same as above> \
python scripts/seed_demo.py
```

In production the script **refuses to run** unless `HUB_ADMIN_EMAIL` and
`HUB_ADMIN_PASSWORD` are explicitly set to real, non-demo values — the
defaults `admin@demo.local` / `demo-admin-pass` are public. In demo mode
the defaults still work for local trials. The seed also creates the demo
tenant and prints its tenant secret key exactly once; store it then.

## Optional provider keys — what happens without each

Everything else is key-gated: unset means an honest fallback, never a
crash.

| Variables | Without them |
|---|---|
| `OPENAI_API_KEY` **or** `GEMINI_API_KEY` (plus optional `OPENAI_MODEL` / `GEMINI_MODEL`) | The AI front line runs in **demo mode** — deterministic knowledge-base answers, visibly flagged `demo` in API responses and `/api/healthz`. OpenAI wins if both keys are set. |
| `OPENAI_EMBED_MODEL` / `GEMINI_EMBED_MODEL` | Only relevant with an AI key: semantic retrieval falls back to exact keyword matching. |
| `SMTP_HOST` + `SMTP_FROM` (plus `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_TLS`) | Outbound email is **queued in the outbox** collection instead of sent; operators can see exactly what would have gone out. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | SMS is **queued in the outbox** instead of sent, and inbound Twilio signature verification is not enforced. |
| `HUB_CHANNEL_SECRET` | Optional shared secret for the email/voice channel webhooks (alternative to per-tenant secret keys). |

## Tuning (safe defaults in code)

`HUB_JWT_TTL_HOURS` (12) · `HUB_CORS_ORIGINS` (none — the console is
same-origin) · `HUB_UPLOAD_DIR` (local disk under `backend/uploads/` —
attachments are stored on local disk, so use a host with a persistent
disk or accept that redeploys can drop them) · `HUB_MAX_ATTACHMENT_MB`
(10) · `HUB_QUALITY_THRESHOLD` (70) · `HUB_SLA_SWEEP_SECONDS` (60;
`HUB_SCHEDULER=off` disables the in-app scheduler) ·
`HUB_AI_COST_OPENAI_PER_1K` / `HUB_AI_COST_GEMINI_PER_1K` (reporting
estimates only).

## Smoke check after deploy

- `GET /api/healthz` → `{"ok": true, "service": "service-hub", "demo_ai": <bool>}`
- `GET /api/health` → database `ok` and the live `ai_provider` name
  (`demo` until an AI key is set).
- Sign in at `/console` with the seeded operator credentials.
