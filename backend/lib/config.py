"""Runtime configuration, all from environment variables (secrets never in code).

Provider keys are *key-gated*: when no LLM key is present the hub runs in
demo mode (deterministic, clearly flagged) instead of failing — the same
provider-abstracted demo-mode convention used across the user's products.
"""
import os


def jwt_secret() -> str:
    return os.environ.get("HUB_JWT_SECRET", "demo-secret-change-me")


def jwt_ttl_hours() -> int:
    return int(os.environ.get("HUB_JWT_TTL_HOURS", "12"))


def gemini_api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or None


def openai_api_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY") or None


def ai_provider_name() -> str:
    """Which AI provider is live: 'openai', 'gemini', or 'demo'."""
    if openai_api_key():
        return "openai"
    if gemini_api_key():
        return "gemini"
    return "demo"


def demo_mode() -> bool:
    return ai_provider_name() == "demo"


def cors_origins() -> list[str]:
    """Extra allowed origins for the JSON API (comma-separated env).

    The operator console is served by this same app (same-origin), and the
    widget enforces per-tenant origins separately, so this defaults to none.
    """
    raw = os.environ.get("HUB_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]
