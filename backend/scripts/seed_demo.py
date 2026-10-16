"""Seed a demo tenant + admin operator so the hub can be tried instantly.

    cd backend && python scripts/seed_demo.py

Reads MONGO_URL / DB_NAME like the app. Admin credentials come from
HUB_ADMIN_EMAIL / HUB_ADMIN_PASSWORD (defaults are DEMO-ONLY — change them
before any real deployment). The tenant secret key is printed exactly once.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db as db_module  # noqa: E402
from lib import auth  # noqa: E402
from lib.util import now  # noqa: E402

ADMIN_EMAIL = os.environ.get("HUB_ADMIN_EMAIL", "admin@demo.local")
ADMIN_PASSWORD = os.environ.get("HUB_ADMIN_PASSWORD", "demo-admin-pass")

KB_ENTRIES = [
    {
        "title": "Opening hours",
        "content": "We're available Monday to Friday, 8am to 6pm Central. "
                   "The AI assistant (that's me) is here around the clock.",
        "tags": ["hours", "opening", "when", "schedule"],
    },
    {
        "title": "How billing works",
        "content": "Plans are billed monthly. Books Light is $10/mo as a Momo "
                   "add-on or $18.99/mo standalone; Medium is $20/mo add-on or "
                   "$39.99/mo standalone. You can cancel any time from Settings.",
        "tags": ["billing", "price", "pricing", "cost", "plan", "subscription"],
    },
    {
        "title": "Connecting your bank",
        "content": "Bank feeds connect securely through Plaid once your "
                   "Mercury business account is active. Go to Settings → "
                   "Bank Connections and follow the prompts.",
        "tags": ["bank", "plaid", "connect", "mercury", "feed"],
    },
    {
        "title": "Paying an invoice by card",
        "content": "Open the invoice link from your email — the client portal "
                   "accepts card payments through Stripe, no account needed.",
        "tags": ["invoice", "pay", "card", "stripe", "portal"],
    },
]


async def main():
    database = db_module.get_db()

    admin = await database[db_module.OPERATORS].find_one({"email": ADMIN_EMAIL})
    if not admin:
        admin = {
            "_id": "op_admin",
            "email": ADMIN_EMAIL,
            "name": "Hub Admin (demo)",
            "password_hash": auth.hash_password(ADMIN_PASSWORD),
            "role": "admin",
            "tenant_ids": [],
        }
        await database[db_module.OPERATORS].insert_one(admin)
        print(f"Created admin operator {ADMIN_EMAIL} (password from HUB_ADMIN_PASSWORD)")

    tenant = await database[db_module.TENANTS].find_one({"_id": "ten_demo-books"})
    if not tenant:
        secret = auth.generate_secret_key()
        tenant = {
            "_id": "ten_demo-books",
            "slug": "demo-books",
            "name": "Mind at Ease Books",
            "branding": {
                "name": "Mind at Ease Books",
                "primary_color": "#1d4ed8",
                "accent_color": "#f59e0b",
                "greeting": "Hi! You're chatting with Mind at Ease Books support. "
                            "Ask me about hours, billing, bank connections or invoices.",
                "logo_url": None,
            },
            "languages": ["en", "es"],
            "allowed_origins": [],
            "escalation_keywords": ["lawyer", "bbb", "chargeback"],
            "confidence_threshold": 0.55,
            "sentiment_threshold": -0.34,
            "public_key": auth.generate_public_key(),
            "secret_key_hash": auth.hash_secret_key(secret),
            "created_at": now(),
        }
        await database[db_module.TENANTS].insert_one(tenant)
        print(f"Created tenant {tenant['_id']}")
        print(f"  public key (widget): {tenant['public_key']}")
        print(f"  secret key (shown once): {secret}")
        for entry in KB_ENTRIES:
            await database[db_module.KNOWLEDGE].insert_one({
                "_id": "kb_" + entry["title"].lower().replace(" ", "-"),
                "tenant_id": tenant["_id"],
                **entry,
                "created_at": now(),
            })
        print(f"Seeded {len(KB_ENTRIES)} knowledge entries.")
    else:
        print(f"Tenant already exists; public key: {tenant['public_key']}")


if __name__ == "__main__":
    asyncio.run(main())
