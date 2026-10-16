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


def ai_cost_rates() -> dict[str, float]:
    """Estimated AI cost per 1k tokens (USD), per provider. Env-overridable.

    These are *estimates* for cost-per-resolution reporting, not billing.
    """
    def _rate(env_name: str, default: float) -> float:
        try:
            return float(os.environ.get(env_name, default))
        except (TypeError, ValueError):
            return default

    return {
        "openai": _rate("HUB_AI_COST_OPENAI_PER_1K", 0.0006),
        "gemini": _rate("HUB_AI_COST_GEMINI_PER_1K", 0.0003),
        "demo": 0.0,
    }


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def twilio_configured() -> bool:
    return bool(
        os.environ.get("TWILIO_ACCOUNT_SID")
        and os.environ.get("TWILIO_AUTH_TOKEN")
        and os.environ.get("TWILIO_FROM_NUMBER")
    )


def twilio_auth_token() -> str | None:
    return os.environ.get("TWILIO_AUTH_TOKEN") or None


def channel_secret() -> str | None:
    """Optional shared secret for channel webhooks (email/voice intake)."""
    return os.environ.get("HUB_CHANNEL_SECRET") or None


def upload_dir() -> str:
    return os.environ.get("HUB_UPLOAD_DIR", "")


def max_attachment_bytes() -> int:
    try:
        mb = float(os.environ.get("HUB_MAX_ATTACHMENT_MB", "10"))
    except (TypeError, ValueError):
        mb = 10.0
    return int(mb * 1024 * 1024)


def quality_review_threshold() -> int:
    try:
        return int(os.environ.get("HUB_QUALITY_THRESHOLD", "70"))
    except (TypeError, ValueError):
        return 70


def cors_origins() -> list[str]:
    """Extra allowed origins for the JSON API (comma-separated env).

    The operator console is served by this same app (same-origin), and the
    widget enforces per-tenant origins separately, so this defaults to none.
    """
    raw = os.environ.get("HUB_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]
