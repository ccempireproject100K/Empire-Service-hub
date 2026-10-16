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

36 tests: auth, tenants/keys/branding/CORS, knowledge CRUD + secret-key
path, conversation lifecycle, demo-AI determinism, **one test per
escalation rule**, archive completion + PII masking + JSONL export, CSAT
gating/validation/aggregation, tenant isolation, rate limiting. They run
against an in-memory Motor-style fake DB, so no MongoDB is needed.

## What's stubbed / later

- **Channels:** chat only in v1. Email and voice channels plug into the
  same conversation/escalation core next.
- **Sentiment:** lexicon scorer (demo-grade). A real model can replace
  `lib/sentiment.py:score_text` behind the same return shape.
- **AI confidence:** retrieval-grounded heuristic; provider-reported
  confidence/logprobs can be blended in later.
- **Rate limiting:** in-memory; Redis fast-follow.
- **Archive → learning:** the JSONL export is the training/eval corpus
  format; automated fine-tune/eval jobs consume it downstream (refinery).
