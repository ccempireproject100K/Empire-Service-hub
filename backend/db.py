"""Database access for the service hub.

Mirrors the bookkeeping app's convention: a single Motor client built lazily
from env (``MONGO_URL`` / ``DB_NAME``), handed to routers through a FastAPI
dependency so tests can substitute an in-memory fake.
"""
import os

from motor.motor_asyncio import AsyncIOMotorClient

_client = None


def get_db():
    """Return the hub database handle (creates the client on first use)."""
    global _client
    if _client is None:
        url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        _client = AsyncIOMotorClient(url, serverSelectionTimeoutMS=5000)
    return _client[os.environ.get("DB_NAME", "service_hub")]


# Collection names (single source of truth).
TENANTS = "tenants"
OPERATORS = "operators"
KNOWLEDGE = "knowledge_entries"
CONVERSATIONS = "conversations"
MESSAGES = "messages"
ARCHIVE = "escalation_archive"
CSAT = "csat_responses"
# v2 collections
KB_DRAFTS = "kb_drafts"
DOCUMENTS = "documents"
DOC_CHUNKS = "document_chunks"
NOTES = "conversation_notes"
MACROS = "macros"
ATTACHMENTS = "attachments"
OUTBOX = "outbox"
WEBHOOK_DELIVERIES = "webhook_deliveries"
SLA_ALERTS = "sla_alerts"
QUALITY_REVIEWS = "quality_reviews"
