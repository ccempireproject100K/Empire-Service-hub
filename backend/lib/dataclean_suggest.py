"""Offline DataClean suggestion job for the Service Hub (dry-run only).

WHY THIS EXISTS — the hub's identity problem, in its own code:
``lib/customers.py`` recognizes a returning customer by ONE key per
conversation — ``email:`` first, then ``ref:``, then ``phone:`` (last 10
digits) — and ``history()`` matches that key by exact equality, per
tenant. Each channel supplies a different subset of identity:

* widget chat (``routers/widget.py``): name + email + ref — *no phone
  field exists on the widget intake at all*;
* email (``routers/channels.py``): the sender address only;
* SMS (``routers/channels.py``): the sender number only, no name;
* voice (``routers/channels.py``): name + email + phone + ref.

So the same person who chats with an email, then texts from their
phone, exists as two unrelated ``customer_key`` values, and neither
their history nor the AI's context ever joins up. That fragmentation —
not dirty rows inside one list — is the hub's DataClean-shaped problem.

WHAT THIS MODULE DOES: an offline, read-only report job. It projects
conversation identity fields onto DataClean's shapes, runs the REAL
books engine in-process (``lib.dataclean`` from the books tree —
``cleaning.clean_row``, ``linkage.suggest_duplicates``,
``mess.mess_report``, ``schema.validate_rows``) over a COPY of the
identities, and returns a report dict of *suggestions*: duplicate
pairs, review pairs, and phone/email/name standardization changes.
It writes nothing, merges nothing, and never calls the live API.

It also runs a hub-side exact-contact analysis the engine cannot do:
engine linkage needs a name to score a pair, so name-less SMS/email
observations are invisible to it — yet those are exactly the fragments
that split a customer_key. Grouping observations by normalized email /
phone and reporting groups that span >1 ``customer_key`` or >1 channel
proves the fragmentation directly. Both analyses are labeled as such
in the report; neither is ever applied automatically.

MAPPING HONESTY: the cleanup API accepts only ``name, email, phone,
address`` (unknown field = 422). Hub conversations carry no address
field at all — reported as ``address: not stored by the hub``, never
invented. ``customer_ref``, ``customer_key``, ``channel`` and
``external_ref`` are projected OUT of cleanup rows and counted in the
report's ``mapping.dropped_fields``; they are carried on dedupe
records only (the dedupe API accepts arbitrary dicts), where
``customer_key``/``channel`` are evidence, not cleanup input.

ENGINE LOADING: the books engine is loaded as an isolated package
(``hub_dataclean_engine``) so its ``lib.dataclean`` name never collides
with the hub's own ``lib`` package. Path resolution: the
``engine_path`` argument, then ``$DATACLEAN_ENGINE_PATH``, then the
known buildout tree. When the engine (or its third-party deps —
phonenumbers, RapidFuzz, …) is unavailable, ``load_engine`` returns
``None`` and ``build_report`` raises :class:`EngineUnavailable`; the
hub's own runtime never imports this module, so a missing engine can
never break the live service. The engine's Splink tier is used only
when it imports cleanly in the local environment; otherwise the engine
itself falls back to its deterministic tier and says so in
``engine_note``, which the report carries verbatim.
"""
import importlib.util
import os
import sys
from collections import Counter, defaultdict

CLEANUP_FIELDS = ("name", "email", "phone", "address")
#: Conversation identity fields that cleanup cannot accept — projected
#: out and counted, never silently dropped (spec: mapping honesty).
UNMAPPED_FIELDS = ("customer_ref", "customer_key", "channel",
                   "external_ref")

_DEFAULT_ENGINE_BACKEND = os.path.expanduser(
    "~/workspace/buildout/dataclean-charging/work/backend")


class EngineUnavailable(RuntimeError):
    """The books DataClean engine could not be loaded in-process."""


_engine_cache = {}


def load_engine(engine_path: str | None = None):
    """Load books' ``lib.dataclean`` as an isolated package, or None."""
    backend = (engine_path or os.environ.get("DATACLEAN_ENGINE_PATH")
               or _DEFAULT_ENGINE_BACKEND)
    backend = os.path.expanduser(backend)
    if backend in _engine_cache:
        return _engine_cache[backend]
    package_dir = os.path.join(backend, "lib", "dataclean")
    init_file = os.path.join(package_dir, "__init__.py")
    module = None
    if os.path.isfile(init_file):
        try:
            spec = importlib.util.spec_from_file_location(
                "hub_dataclean_engine", init_file,
                submodule_search_locations=[package_dir])
            module = importlib.util.module_from_spec(spec)
            sys.modules["hub_dataclean_engine"] = module
            spec.loader.exec_module(module)
        except Exception:
            module = None
            sys.modules.pop("hub_dataclean_engine", None)
    _engine_cache[backend] = module
    return module


# ------------------------------------------------------------------ mapping
def conversation_to_identity(conversation: dict) -> dict:
    """One conversation -> one identity observation (channel-shaped).

    Fields mirror what each intake actually stores on the conversation
    document (see routers/widget.py + routers/channels.py): SMS
    observations have no name/email, email observations no name/phone,
    widget observations no phone.
    """
    return {
        "id": conversation.get("_id") or conversation.get("id"),
        "tenant_id": conversation.get("tenant_id"),
        "channel": conversation.get("channel"),
        "name": conversation.get("customer_name"),
        "email": conversation.get("customer_email"),
        "phone": conversation.get("customer_phone"),
        "customer_ref": conversation.get("customer_ref"),
        "customer_key": conversation.get("customer_key"),
        "external_ref": conversation.get("external_ref"),
    }


def identity_to_cleanup_row(identity: dict):
    """Project an identity onto the cleanup shape {name,email,phone,address}.

    Returns ``(row, dropped)`` where ``dropped`` lists the unmapped
    fields that carried a value. ``address`` is never set: the hub
    stores no address for any customer on any channel.
    """
    row = {}
    for field in ("name", "email", "phone"):
        value = identity.get(field)
        if value not in (None, ""):
            row[field] = value
    dropped = [f for f in UNMAPPED_FIELDS if identity.get(f) not in (None, "")]
    return row, dropped


def identity_to_dedupe_record(identity: dict) -> dict:
    """Dedupe records are arbitrary dicts; key/channel ride as evidence."""
    record = {"id": identity.get("id")}
    for field in ("name", "email", "phone"):
        if identity.get(field) not in (None, ""):
            record[field] = identity[field]
    record["customer_key"] = identity.get("customer_key")
    record["channel"] = identity.get("channel")
    return record


# ------------------------------------------------------- exact-contact pass
def _norm_email(value) -> str | None:
    value = str(value or "").strip().lower()
    return value or None


def _norm_phone(value) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] or None


def fragmentation_analysis(identities: list[dict]) -> dict:
    """Group observations by normalized email/phone; report the groups
    that span more than one customer_key or more than one channel.

    This is exact-match bookkeeping, NOT fuzzy linkage: a group proves
    the hub holds two identity keys for observations sharing a verified
    contact point. It never merges anything — it counts fragments.
    """
    by_contact: dict[str, list[dict]] = defaultdict(list)
    for identity in identities:
        email = _norm_email(identity.get("email"))
        phone = _norm_phone(identity.get("phone"))
        if email:
            by_contact["email:" + email].append(identity)
        if phone:
            by_contact["phone:" + phone].append(identity)
    groups = []
    fragmented_keys = set()
    for contact, members in sorted(by_contact.items()):
        if len(members) < 2:
            continue
        keys = sorted({m.get("customer_key") for m in members
                       if m.get("customer_key")})
        channels = sorted({m.get("channel") for m in members
                           if m.get("channel")})
        if len(keys) > 1 or len(channels) > 1:
            groups.append({
                "contact": contact,
                "observations": [m.get("id") for m in members],
                "customer_keys": keys,
                "channels": channels,
                "fragmented": len(keys) > 1,
            })
            fragmented_keys.update(keys)
    return {"groups": groups, "group_count": len(groups),
            "fragmented_group_count":
                sum(1 for g in groups if g["fragmented"]),
            "fragmented_customer_keys": sorted(fragmented_keys)}


# ------------------------------------------------------------------- report
def build_report(identities: list[dict], source_label: str,
                 engine=None, engine_path: str | None = None) -> dict:
    """Run the full offline suggestion pass. Pure: inputs are not mutated,
    nothing is written anywhere, and no network call is made."""
    if engine is None:
        engine = load_engine(engine_path)
    if engine is None:
        raise EngineUnavailable(
            "books DataClean engine (lib.dataclean) not loadable — set "
            "DATACLEAN_ENGINE_PATH to the books backend tree.")
    cleaning, linkage = engine.cleaning, engine.linkage
    mess, schema = engine.mess, engine.schema

    by_tenant: dict[str, list[dict]] = defaultdict(list)
    for identity in identities:
        by_tenant[identity.get("tenant_id") or "(no tenant)"].append(identity)

    tenants: dict[str, dict] = {}
    totals = {"identities_in": 0, "suggested_pairs": 0, "review_pairs": 0,
              "changes_total": 0, "changes_by_rule": Counter(),
              "fragmented_groups": 0, "fragmented_customer_keys": set(),
              "dropped_field_values": Counter()}
    engine_note = None
    for tenant_id, group in sorted(by_tenant.items()):
        rows, records = [], []
        for identity in group:
            row, dropped = identity_to_cleanup_row(identity)
            rows.append(row)
            totals["dropped_field_values"].update(dropped)
            records.append(identity_to_dedupe_record(identity))

        verdict = schema.validate_rows(rows, "customers")
        mess_columns = mess.mess_report(rows).get("columns", [])

        cleaned_rows, changes = [], []
        for row in rows:
            cleaned, row_changes = cleaning.clean_row(row, "customers")
            cleaned_rows.append(cleaned)
            changes.extend(row_changes)
        changes_by_rule = Counter(c["rule"] for c in changes)
        normalize_changes = [c for c in changes
                             if c["rule"].startswith(("phone:", "email:"))]

        dedupe = linkage.suggest_duplicates(records)
        if dedupe.get("engine_note"):
            engine_note = dedupe["engine_note"]
        frag = fragmentation_analysis(group)

        tenants[tenant_id] = {
            "identities_in": len(group),
            "channels": dict(Counter(i.get("channel") or "(unknown)"
                                     for i in group)),
            "schema": {"blocking": verdict.get("blocking", []),
                       "row_error_count": len(verdict.get("row_errors", []))},
            "mess_columns": mess_columns,
            "cleanup": {
                "rows": len(rows),
                "changes_total": len(changes),
                "changes_by_rule": dict(changes_by_rule),
                "normalize_changes": len(normalize_changes),
                "normalize_changes_by_field": dict(Counter(
                    c["field"] for c in normalize_changes)),
                "cleaned_rows": cleaned_rows,
                "changes": changes,
            },
            "dedupe": {"engine": dedupe.get("engine"),
                       "pairs": dedupe.get("pairs", []),
                       "review_pairs": dedupe.get("review_pairs", []),
                       "suggested_count": len(dedupe.get("pairs", [])),
                       "review_count": len(dedupe.get("review_pairs", []))},
            "fragmentation": frag,
        }
        totals["identities_in"] += len(group)
        totals["suggested_pairs"] += len(dedupe.get("pairs", []))
        totals["review_pairs"] += len(dedupe.get("review_pairs", []))
        totals["changes_total"] += len(changes)
        totals["changes_by_rule"].update(changes_by_rule)
        totals["fragmented_groups"] += frag["fragmented_group_count"]
        totals["fragmented_customer_keys"].update(
            frag["fragmented_customer_keys"])

    return {
        "service": "service-hub",
        "mode": "dry-run",
        "suggestions_only": True,
        "auto_merge": False,
        "auto_overwrite": False,
        "source": source_label,
        "engine": {"package": "books lib.dataclean (in-process)",
                   "engine_note": engine_note},
        "mapping": {
            "cleanup_fields": list(CLEANUP_FIELDS),
            "dropped_fields": list(UNMAPPED_FIELDS),
            "dropped_field_values": dict(totals["dropped_field_values"]),
            "address": "not stored by the hub on any channel — never "
                       "sent, never invented",
        },
        "live_api_chunking": {
            "cleanup": "5,000-row chunks",
            "dedupe": "500-record blocked windows (see dataclean_client)",
            "normalize": "1,000-value chunks",
            "note": "dry-run runs the engine in-process over the full "
                    "list, so no chunking limit applies to these numbers",
        },
        "tenants": tenants,
        "totals": {
            "identities_in": totals["identities_in"],
            "tenants": len(tenants),
            "suggested_pairs": totals["suggested_pairs"],
            "review_pairs": totals["review_pairs"],
            "changes_total": totals["changes_total"],
            "changes_by_rule": dict(totals["changes_by_rule"]),
            "fragmented_groups": totals["fragmented_groups"],
            "fragmented_customer_keys":
                len(totals["fragmented_customer_keys"]),
        },
    }


# ------------------------------------------------------------- synthetic set
_FIRST = ["Robert", "William", "Elizabeth", "Katherine", "Margaret",
          "Joseph", "Michael", "James", "Richard", "Patricia",
          "Jennifer", "Christopher", "Daniel", "Matthew", "Andrew",
          "Joshua", "David", "Thomas", "Charles", "Daniela",
          "Alicia", "Marcus", "Priya", "Kenji", "Fatima", "Olga",
          "Sven", "Lucia", "Mateo", "Ingrid", "Hassan", "Nadia",
          "Piotr", "Greta", "Emeka", "Yuki"]
_NICK = {"Robert": "Bob", "William": "Bill", "Elizabeth": "Liz",
         "Katherine": "Kate", "Margaret": "Peggy", "Joseph": "Joe",
         "Michael": "Mike", "James": "Jim", "Richard": "Rick",
         "Patricia": "Pat", "Jennifer": "Jenny", "Christopher": "Chris",
         "Daniel": "Dan", "Matthew": "Matt", "Andrew": "Andy",
         "Joshua": "Josh", "David": "Dave", "Thomas": "Tom",
         "Charles": "Chuck"}
_LAST = ["Smith", "Garcia", "Johnson", "Sharma", "Brown", "Lee",
         "Patel", "Nguyen", "Kim", "Novak", "Haddad", "Berg",
         "Rossi", "Costa", "Lindqvist", "Moreau", "Kowalski", "Fischer",
         "Okafor", "Tanaka", "Novakova", "Silva", "Meyer", "Ali",
         "Petrov", "Dubois", "Santos", "Weber", "Ndiaye", "Sato",
         "Larsen", "Vidal", "Osei", "Kaur", "Mensah", "Iversen"]


def synthetic_identities(tenant_id: str = "ten_synthetic") -> list[dict]:
    """SYNTHETIC dry-run sample — NOT real customers, NOT repo fixtures.

    The hub's test fixtures seed conversations through the API but do
    not carry a reusable customer dataset, so the dry-run uses this
    labeled synthetic set instead: 36 people x 4 channel observations
    (widget / email / SMS / voice, shaped exactly like each intake
    stores them) + 12 single-observation people = 156 identities.
    Planted per multi-channel person: the same email in different
    case/whitespace on widget vs email, the same phone in national vs
    E.164-ish formatting on SMS vs voice, and a nickname/initial name
    variant on voice — i.e. one person deliberately fragmented across
    an ``email:`` key and a ``phone:`` key, the hub's real failure mode.
    """
    from lib.customers import customer_key

    out: list[dict] = []
    for i, first in enumerate(_FIRST):
        last = _LAST[i % len(_LAST)]
        email = f"{first.lower()}.{last.lower()}@example.com"
        phone_national = f"(214) 555-01{i:02d}"
        phone_intl = f"+1 214-555-01{i:02d}"
        nick = _NICK.get(first, first)
        out.append({  # widget chat: name + email (messy case), no phone
            "id": f"syn-{i:02d}-widget", "tenant_id": tenant_id,
            "channel": "chat", "name": f"{first}  {last}",
            "email": f"  {email.upper()}", "phone": None,
            "customer_ref": None, "external_ref": None,
            "customer_key": customer_key(email=email),
        })
        out.append({  # email channel: sender address only
            "id": f"syn-{i:02d}-email", "tenant_id": tenant_id,
            "channel": "email", "name": None, "email": email,
            "phone": None, "customer_ref": None, "external_ref": None,
            "customer_key": customer_key(email=email),
        })
        out.append({  # SMS: sender number only, no name, no email
            "id": f"syn-{i:02d}-sms", "tenant_id": tenant_id,
            "channel": "sms", "name": None, "email": None,
            "phone": phone_national, "customer_ref": None,
            "external_ref": None,
            "customer_key": customer_key(phone=phone_national),
        })
        out.append({  # voice handoff: nickname variant + phone + email
            "id": f"syn-{i:02d}-voice", "tenant_id": tenant_id,
            "channel": "voice", "name": f"{nick} {last}",
            "email": email, "phone": phone_intl, "customer_ref": None,
            "external_ref": None,
            "customer_key": customer_key(email=email, phone=phone_intl),
        })
    for j in range(12):  # singletons: one clean widget observation each
        first, last = _FIRST[(j * 3 + 5) % len(_FIRST)], _LAST[(j * 5 + 2) % len(_LAST)]
        email = f"single{j}.{last.lower()}@example.org"
        out.append({
            "id": f"syn-single-{j:02d}", "tenant_id": tenant_id,
            "channel": "chat", "name": f"Singleton{j} {last}",
            "email": email, "phone": None, "customer_ref": None,
            "external_ref": None, "customer_key": customer_key(email=email),
        })
    return out
