# Service Hub

A central, multi-tenant, **AI-first customer service hub** for the whole
empire of products. Instead of every product re-solving support, each one
embeds the hub's widget and shares one AI front line, one human escalation
queue, and — most importantly — **one escalation archive**: every
conversation the AI couldn't handle, plus how a human resolved it. That
archive is the learning loop: it is the corpus that makes the AI better and
shrinks the human workload over time.

First embed: the bookkeeping app. Next at launch: Momo. Later products just
add a tenant.

Built fresh (FastAPI + MongoDB/Motor backend, vanilla-JS console + widget),
deliberately mirroring the bookkeeping app's conventions — key-gated
providers, demo mode, `lib/` helpers, `routers/` — so operational knowledge
transfers between the products. Patterns for sentiment scoring and
escalation rules were harvested from Momo's service-excellence work
(conversation intelligence), without the job/dispatch machinery.

## How it works

```
customer ── widget (embedded in a product) ──► hub API ──► AI front line
                                                    │         (tenant KB)
                                                    ▼
                                          escalation engine
                                       (sentiment · low confidence ·
                                        customer request · keywords)
                                                    │
                       ┌────────────────────────────┴───────────────┐
                       ▼                                            ▼
               human queue (console)                     escalation archive
               take over · reply · resolve               + human resolution
                       │                                            │
                       └──────────────► CSAT pulse ◄────────────────┘
                                          (the learning corpus,
                                           JSONL-exportable)
```

### Core pieces

| Piece | Where | Notes |
|---|---|---|
| Tenancy | `backend/routers/tenants.py` | Tenant = product/business context: branding, languages, knowledge scope, escalation config, public widget key + hashed secret key. Isolation enforced on every surface and covered by tests. |
| Conversations | `backend/routers/widget.py` | Widget-facing API: open, message, read. Statuses: `open → ai_handling → escalated → operator_handling → resolved`. |
| AI front line | `backend/lib/ai.py` | Provider-abstracted: OpenAI → Gemini → deterministic **demo** provider. Confidence is grounded in KB retrieval strength, never invented. Replies carry a `demo` flag in demo mode. |
| Knowledge | `backend/lib/knowledge.py` | Per-tenant help articles; deterministic keyword retrieval (title > tags > body). |
| Escalation | `backend/lib/escalation.py`, `lib/sentiment.py` | Rules in priority order: customer request → tenant keyword → negative sentiment (lexicon scorer) → low AI confidence. Thresholds are per-tenant. |
| Archive | `backend/lib/archive.py`, `routers/archive.py` | Escalation snapshot (transcript **PII-masked at write time**) + the human's resolution. Browse in the console or export JSONL. |
| Console | `frontend/console/` | Operator sign-in (PBKDF2 passwords, JWT), conversation queue, take over / reply / resolve, archive browser, per-tenant CSAT + SLA header stats. |
| Widget | `frontend/widget/widget.js` | One script tag; themed by tenant branding; resumes conversations via localStorage; CSAT star prompt appears on resolution. |
| Learning loop (v2) | `backend/lib/learning.py`, `lib/pipeline.py` | Every channel runs one pipeline: language detect → precedent + customer-history context → AI reply → escalation. Resolutions become KB drafts; metrics track deflection. |
| Channels (v2) | `backend/routers/channels.py` | Email (secret-key webhook + SMTP/outbox), SMS (Twilio-shaped, signature-checked), voice transcripts (Momo receptionist handoffs). |
| Integrations (v2) | `backend/lib/webhooks.py`, `routers/sync.py` | Signed tenant webhooks with retry + delivery log; sync-back status API by `external_ref`. |

## v2 — closing the loop

v2 turns the hub from an AI front line with an archive into a learning
system, and from a chat widget into an every-channel service platform.
Everything below follows the same rules as v1: key-gated providers,
demo-safe without keys, per-tenant isolation on every new surface.

**A. The learning loop, closed** (`lib/learning.py`)
- *Resolution → KB draft:* resolving an escalated conversation generates
  a draft knowledge article (LLM-written with a key, deterministic
  template in demo mode) into a per-tenant queue. Operators edit,
  approve (publishes to the live KB) or discard from the console's
  **KB drafts** tab (`/api/tenants/{id}/kb-drafts`).
- *Similar-case retrieval:* new escalations and AI answers pull the
  closest archived cases (keyword overlap + embedding boost when
  configured). The AI prompt gets them as precedent; the operator sees
  "Solved before like this" in the conversation detail.
- *Learning metrics:* deflection rate (resolved without escalation),
  repeat-issue rate, daily trend — in `/api/operator/stats` and
  `/api/operator/analytics`.

**B. Semantic knowledge** (`lib/documents.py`, `lib/embeddings.py`)
- Operators ingest documents (txt/md/html/csv/json content, or one URL)
  via `/api/tenants/{id}/documents`; documents are chunked, embedded
  when a provider is configured, and retrieved *hybrid*: keyword scoring
  (unchanged v1 behavior) plus a semantic boost when embeddings exist.

**C. Channels** (`routers/channels.py`, `lib/mailer.py`, `lib/storage.py`)
- *Email:* `POST /api/channels/email/inbound` (tenant secret key or
  `HUB_CHANNEL_SECRET`) creates/continues a conversation by sender;
  replies go out via SMTP (`SMTP_*` env) or queue in the outbox.
- *SMS:* `POST /api/channels/sms/inbound` takes Twilio-shaped form posts,
  resolves the tenant by destination number (`sms_number` on the tenant),
  verifies the Twilio signature when `TWILIO_AUTH_TOKEN` is set, and
  replies via Twilio when configured (outbox-queued otherwise).
- *Voice:* `POST /api/channels/voice/transcript` accepts a call
  transcript + summary (this is how Momo's AI receptionist hands off) and
  lands it escalated, with full context, in the queue + archive.
- *Attachments:* widget + console uploads (base64 JSON, size/type
  limited, PII-safe filenames, local-disk storage under
  `HUB_UPLOAD_DIR`) attach to messages; downloads re-check tenant scope.

**D. Operator depth** (`lib/routing.py`, `lib/sla.py`)
- Round-robin auto-assignment on escalation (least-loaded available
  operator; per-tenant `auto_assign` opt-out), manual reassign, and an
  availability toggle in the console header.
- Internal notes on conversations (never customer-visible) and per-tenant
  canned responses/macros, insertable from the reply box.
- SLA clocks per tenant (first response / resolution minutes); an in-app
  scheduler sweep stamps approaching/breached states, raises console
  alerts (`/api/operator/sla-alerts`), and fires `sla.breached` webhooks.

**E. Two-way integrations** (`lib/webhooks.py`, `routers/sync.py`)
- Tenant webhooks OUT for `escalation.created`, `conversation.resolved`,
  `sla.breached`, `csat.received` — HMAC-SHA256 signed with the tenant's
  `webhook_secret`, retried with backoff (3 attempts), fully logged
  (`webhook_deliveries` collection).
- Sync-back API: products read resolution status by their own
  `external_ref` or hub conversation id with the tenant secret key
  (`/api/sync/status`, `/api/sync/conversations/{id}`).

**F. Analytics & quality** (`lib/analytics.py`, `lib/quality.py`)
- Topic clustering: escalations grouped by dominant keywords — the top
  escalation drivers per tenant (`/api/operator/analytics`).
- Cost-per-resolution: per-message token estimates × configurable
  provider rates → tenant spend + cost per resolved conversation.
- Quality self-review: every resolution is scored (completeness, tone,
  policy flags); low scores queue for operator review in Insights.

**G. Market-leading touches**
- *Proactive widget:* per-tenant triggers (time-on-page, page paths)
  offer help before the customer asks; a tenant status banner (incident
  notice) renders in the widget.
- *Customer memory:* customers are recognized per tenant by email /
  product ref / phone; prior conversations + resolutions feed the AI
  prompt and the operator timeline.
- *Language auto-detect:* each message's language is detected (EN/ES/FR/ZH
  tags); AI replies follow it within the tenant's enabled languages.
  Sentiment lexicons cover EN + ES.
- *Guardrails:* per-tenant sensitive topics (`sensitive_topics`) force
  escalation regardless of confidence; AI replies pass an outbound PII
  check and are masked + flagged if the model echoes personal data.

## Quick start

```bash
cd backend
pip install -r requirements.txt
export MONGO_URL="mongodb://localhost:27017"   # or your Atlas SRV string
python scripts/seed_demo.py                    # demo tenant + admin operator
uvicorn server:app --host 0.0.0.0 --port 8000
```

- Operator console: http://localhost:8000/console
  (seeded admin: `HUB_ADMIN_EMAIL` / `HUB_ADMIN_PASSWORD`, defaults
  `admin@demo.local` / `demo-admin-pass` — **demo-only, change them**)
- Widget demo host page: http://localhost:8000/widget-demo
  (paste the tenant public key printed by the seed script into the page's
  script tag first)
- Health: `/api/healthz`, `/api/health`

No LLM key? The hub runs in **demo mode**: the AI answers deterministically
from the tenant knowledge base with a visible `demo` flag, and sentiment
uses the built-in lexicon. Set `GEMINI_API_KEY` or `OPENAI_API_KEY` to go
live — nothing else changes.

## Environment variables

All secrets come from the environment only. Every provider key is optional
(key-gated); the hub degrades to demo mode rather than failing.

| Variable | Default | Purpose |
|---|---|---|
| `MONGO_URL` | `mongodb://localhost:27017` | MongoDB connection string |
| `DB_NAME` | `service_hub` | Database name |
| `HUB_JWT_SECRET` | `demo-secret-change-me` | Operator JWT signing secret — **set in production** |
| `HUB_JWT_TTL_HOURS` | `12` | Operator session length |
| `GEMINI_API_KEY` | — | Enables the Gemini AI provider |
| `GEMINI_MODEL` | `gemini-1.5-flash` | Gemini model name |
| `OPENAI_API_KEY` | — | Enables the OpenAI AI provider (wins over Gemini) |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model name |
| `HUB_CORS_ORIGINS` | *(none)* | Extra comma-separated API origins; the console is same-origin and the widget enforces per-tenant `allowed_origins` |
| `HUB_ADMIN_EMAIL` / `HUB_ADMIN_PASSWORD` | demo values | Seed-script admin operator only |
| `PORT` | `8000` | Listen port when run as `python server.py` |
| `OPENAI_EMBED_MODEL` / `GEMINI_EMBED_MODEL` | provider defaults | Embedding models for hybrid retrieval (v2 §B) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_TLS` | — | Outbound email; unset → replies queue in the outbox (v2 §C) |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | — | Outbound SMS + inbound signature verification; unset → outbox queue (v2 §C) |
| `HUB_CHANNEL_SECRET` | — | Optional shared secret for email/voice channel webhooks (alternative to per-tenant secret keys) |
| `HUB_UPLOAD_DIR` | `backend/uploads/` | Attachment storage directory (v2 §C) |
| `HUB_MAX_ATTACHMENT_MB` | `10` | Attachment size limit |
| `HUB_SLA_SWEEP_SECONDS` | `60` | SLA scheduler interval; `HUB_SCHEDULER=off` disables the in-app scheduler |
| `HUB_AI_COST_OPENAI_PER_1K`, `HUB_AI_COST_GEMINI_PER_1K` | `0.0006` / `0.0003` | Estimated AI cost per 1k tokens (reporting only) |
| `HUB_QUALITY_THRESHOLD` | `70` | Quality scores below this queue for operator review |

## Embedding the widget in a product

1. Create a tenant (console sign-in → admin API `POST /api/tenants`) and
   note its **public key**. Add help articles via
   `POST /api/tenants/{tenant_id}/knowledge` (operator token, or the
   tenant's secret key from a server — never ship the secret key to a
   browser).
2. Drop this into the product's pages:

```html
<script src="https://YOUR-HUB-HOST/widget.js"
        data-hub-key="pk_the_tenants_public_key" async></script>
```

3. Optionally set the tenant's `allowed_origins` to the product's domains —
   the hub then only reflects CORS headers for those origins.

**Bookkeeping app:** add the script tag to the app shell (e.g. the root
layout rendered for signed-in shop users), with the Books tenant's public
key. Customers of the shop use the same widget in the client portal.
**Momo:** same tag in the shop dashboard and the homeowner hub, using
Momo's tenant key (its own branding + knowledge scope).

Machine-to-machine calls (e.g. a product backend syncing knowledge or
reading stats) use the tenant secret key in the `X-Hub-Secret-Key` header;
only its SHA-256 hash is stored, and it can be rotated from the admin API.

## Security conventions

- Rate limiting on every public endpoint (sliding window, per IP/tenant;
  in-memory in v1 — Redis-backed is a drop-in fast-follow, as in the
  bookkeeping app).
- PII (emails, phone numbers, long digit runs) masked in logs and in the
  archive corpus at write time; live conversations keep originals for the
  operator handling them.
- Passwords PBKDF2-HMAC-SHA256; JWT sessions; tenant secret keys hashed.
- Per-tenant CORS scoping for the widget; security headers on all responses.
- Tenant isolation is tested on every surface (widget, operator, archive,
  knowledge).

## Tests

```bash
cd backend && python -m pytest -q
```

79 tests: the 36 v1 tests (auth, tenants/keys/branding/CORS, knowledge
CRUD + secret-key path, conversation lifecycle, demo-AI determinism,
**one test per escalation rule**, archive completion + PII masking +
JSONL export, CSAT gating/validation/aggregation, tenant isolation, rate
limiting) plus 43 v2 tests: KB-draft lifecycle + learning metrics,
document ingestion + semantic retrieval, email/SMS/voice channels
(incl. Twilio signatures), routing/availability/notes/macros, SLA sweep
+ signed webhooks with retry, sync-back API, topic clusters, cost
accounting, quality queue, proactive config, sensitive topics, language
detection, customer memory, attachments, and outbound-PII masking —
with per-tenant isolation asserted on every new surface. They run
against an in-memory Motor-style fake DB, so no MongoDB is needed.

## What's stubbed / later

- **Sentiment:** lexicon scorer (demo-grade; EN + ES). A real model can
  replace `lib/sentiment.py:score_text` behind the same return shape.
- **AI confidence:** retrieval-grounded heuristic; provider-reported
  confidence/logprobs can be blended in later.
- **Rate limiting:** in-memory; Redis fast-follow.
- **Archive → training:** the JSONL export remains the training/eval
  corpus format; automated fine-tune/eval jobs consume it downstream
  (refinery). v2's in-product loop (drafts + precedent) is the live
  learning path.
- **Attachments:** local-disk storage; an S3-compatible backend can slot
  into `lib/storage.py` behind the same functions.
- **Document formats:** ingestion takes text-native formats
  (txt/md/html/csv/json) + single-page URL fetch; PDF/DOCX should be
  exported to text/markdown first.
