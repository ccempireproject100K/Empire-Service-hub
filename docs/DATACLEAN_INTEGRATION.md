# DataClean integration — Service Hub

Status: **WIRED-DRY-RUN (at build time)** — the hub is not deployed, so
this integration ships *with* the hub when it deploys. Nothing here is
live, and nothing in this pass touches a request path.

## What problem it solves here

The hub recognizes a returning customer by one key per conversation —
`email:` first, then `ref:`, then `phone:` (`backend/lib/customers.py`)
— matched by exact equality, per tenant. Each channel supplies a
different subset of identity:

| Channel | Intake stores | Identity key it can produce |
|---|---|---|
| Widget chat (`routers/widget.py`) | name, email, ref — **no phone field exists** | `email:` / `ref:` |
| Email (`routers/channels.py`) | sender address only | `email:` |
| SMS (`routers/channels.py`) | sender number only, no name | `phone:` |
| Voice (`routers/channels.py`) | name, email, phone, ref | `email:` / `ref:` / `phone:` |

The same person chatting by email and later texting is therefore two
unrelated customers to the hub: their history never joins, the AI
answers without their context, and repeat-issue metrics undercount.
That cross-channel identity fragmentation — not dirty rows — is the
hub's DataClean-shaped problem, and the dry-run numbers in the
integration report (`REPORT-hub.md`, 2026-11-05) prove it on a labeled
synthetic sample shaped exactly like these intakes.

## What is built

| Piece | Where | Notes |
|---|---|---|
| API client | `backend/lib/dataclean_client.py` | Spec contract: `preview` / `clean` / `dedupe` / `normalize`, API-cap chunking (5,000 / 500-blocked / 1,000). Inert unless `DATACLEAN_ENABLED` is truthy; key from `DATACLEAN_API_KEY`, base from `DATACLEAN_API_BASE` (default the books DataClean API). |
| Offline suggestion job | `backend/lib/dataclean_suggest.py` | Projects conversations → identities → cleanup/dedupe shapes, runs the **real books engine in-process**, returns a report dict: dupe pairs + review pairs, standardization changes by rule, schema/mess verdicts, and an exact-contact fragmentation analysis (engine linkage needs a name, so name-less SMS/email fragments are counted by exact normalized email/phone grouping instead — labeled as such). |
| Operator script — the ONLY hook point | `backend/scripts/dataclean_report.py` | `--synthetic` / `--input FILE` / `--mongo` (read-only). No scheduler, no request-path hooks. `--live` is reserved and inert in this pass. |

Safety invariants (from the empire integration spec): suggestions
only — no auto-merge, no auto-overwrite, ever; batch + dry-run only;
mapping is explicit (cleanup gets only `name/email/phone/address`;
`customer_ref`, `customer_key`, `channel`, `external_ref` are counted
as dropped, and `address` is reported as *not stored by the hub*,
never invented); no secrets in the repo.

## Designed, NOT wired (future live hook points)

1. **Channel-intake normalization** — at conversation creation in
   `routers/widget.py` / `routers/channels.py`, normalize the incoming
   email (trim/lowercase — the widget path currently stores it
   verbatim) and phone (E.164) *before* `customers.customer_key(...)`
   derives the key, so formatting variants stop minting new keys.
   Requires a `customer_phone` field on the widget intake, which does
   not exist today.
2. **Nightly identity report** — run `scripts/dataclean_report.py
   --mongo` per tenant on a schedule; land the report in an operator
   review surface (a staging store + console tab), where an operator —
   never the system — decides whether two fragments are one customer.
   A real fix also needs a key-alias mechanism in `lib/customers.py`
   (one person, several keys, history unioned); DataClean supplies
   the suggestions, the alias store is hub work.
3. **Live API mode** — set `DATACLEAN_ENABLED=1` + a console-minted
   `DATACLEAN_API_KEY` in the hub env, and swap the report job's
   in-process engine calls for `lib/dataclean_client.py` calls. Blocked
   on: key minting in the books operator console (owner action, key
   shows once) and the hub's own deployment gates.
