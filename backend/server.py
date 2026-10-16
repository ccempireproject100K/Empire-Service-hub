"""Service Hub API — central, multi-tenant, AI-first customer service.

One hub serves every product: the bookkeeping app embeds the widget first,
Momo plugs in at launch, later products just add a tenant. Run:

    uvicorn server:app --host 0.0.0.0 --port 8000

Operator console:  /console        Embeddable widget: /widget.js
Widget demo page:  /widget-demo    Health: /api/healthz
"""
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import db as db_module
from lib import config, pii
from routers import archive, attachments, auth, channels, operator, sync, tenants
from routers import widget

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("service-hub")

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"

app = FastAPI(title="Service Hub", version="2.0.0")

# Extra API origins (the console itself is same-origin; the widget enforces
# per-tenant origins in the middleware below).
if config.cors_origins():
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def log_and_widget_cors(request: Request, call_next):
    """PII-masked access log + per-tenant CORS for widget endpoints."""
    response = await call_next(request)
    logger.info(
        "%s %s -> %s",
        request.method,
        pii.mask_text(str(request.url.path)),
        response.status_code,
    )
    if request.url.path.startswith("/api/widget"):
        origin = request.headers.get("origin")
        if origin:
            allowed = await _widget_origin_allowed(request)
            if allowed is None or origin in allowed or not allowed:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
                response.headers["Access-Control-Allow-Headers"] = (
                    "Content-Type, X-Hub-Public-Key"
                )
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


async def _widget_origin_allowed(request: Request):
    """Return the tenant's allowed_origins for this request, or None.

    Empty list = tenant hasn't restricted origins (allow any). When the list
    is non-empty and the Origin isn't in it, no CORS header is emitted and
    the browser blocks the call.
    """
    key = request.headers.get("x-hub-public-key") or request.query_params.get("key")
    if not key:
        return None
    try:
        tenant = await db_module.get_db()[db_module.TENANTS].find_one(
            {"public_key": key}
        )
    except Exception:
        return None
    if not tenant:
        return None
    return tenant.get("allowed_origins") or []


@app.options("/api/widget/{path:path}")
async def widget_preflight(path: str):
    return JSONResponse(content={})


@app.get("/api/healthz")
async def healthz():
    return {"ok": True, "service": "service-hub", "demo_ai": config.demo_mode()}


@app.get("/api/health")
async def health():
    try:
        await db_module.get_db().command("ping")
        db_status = "ok"
    except Exception:
        db_status = "unavailable"
    return {"db": db_status, "ai_provider": config.ai_provider_name()}


app.include_router(auth.router)
app.include_router(tenants.router)
app.include_router(widget.router)
app.include_router(operator.router)
app.include_router(archive.router)
app.include_router(channels.router)
app.include_router(sync.router)
app.include_router(attachments.router)


# ------------------------------------------------------- v2: SLA scheduler
import asyncio  # noqa: E402

_scheduler_task: asyncio.Task | None = None


async def _sla_scheduler_loop():
    """Periodically stamp SLA states, raise alerts, retry webhooks."""
    from lib import sla as sla_lib

    try:
        interval = float(os.environ.get("HUB_SLA_SWEEP_SECONDS", "60"))
    except ValueError:
        interval = 60.0
    while True:
        try:
            await asyncio.sleep(interval)
            await sla_lib.run_sweep(db_module.get_db())
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad sweep must never kill the API
            logger.exception("SLA sweep failed")


@app.on_event("startup")
async def _start_scheduler():
    global _scheduler_task
    if os.environ.get("HUB_SCHEDULER", "on") == "off":
        return
    _scheduler_task = asyncio.create_task(_sla_scheduler_loop())


@app.on_event("shutdown")
async def _stop_scheduler():
    if _scheduler_task:
        _scheduler_task.cancel()


# ------------------------------------------------------------ static assets
@app.get("/widget.js")
async def widget_js():
    return FileResponse(FRONTEND_DIR / "widget" / "widget.js",
                        media_type="application/javascript")


@app.get("/widget-demo")
async def widget_demo():
    return FileResponse(FRONTEND_DIR / "widget" / "demo.html", media_type="text/html")


if (FRONTEND_DIR / "console").exists():
    app.mount("/console", StaticFiles(directory=FRONTEND_DIR / "console", html=True),
              name="console")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
