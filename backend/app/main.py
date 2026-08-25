from __future__ import annotations

import logging
import os
import sys
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Sentry SDK is imported eagerly but only initialized when SENTRY_DSN
# is set in the environment. Empty DSN = SDK is a complete no-op, so
# dev / local boxes don't ship phantom errors to a Sentry project.
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from app.db import Base, SessionLocal, engine
from app.models import ErrorLog, UserSession
from app.routes import admin as admin_routes, support as support_routes
from app.routes import auth as auth_routes
from app.routes import billing as billing_routes
from app.routes import dev as dev_routes
from app.routes import errors as errors_routes
from app.routes import youtube as youtube_routes
from app.security import SESSION_COOKIE_NAME


# Wire stdlib logging to stderr so systemd's journal picks it up.
# Uvicorn doesn't add a root handler for app loggers by default — without
# this, log.exception() calls in our routes go nowhere.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stderr,
    force=True,  # override anything uvicorn put in place
)


# Initialize Sentry only when a DSN is configured. The SDK is a no-op
# when init() is never called, so absence of SENTRY_DSN in /opt/aether/.env
# means nothing happens (no phantom errors, no startup cost). When the
# DSN is set we hook FastAPI + SQLAlchemy so request context + DB span
# data attaches to each event automatically.
_sentry_dsn = os.environ.get("SENTRY_DSN", "").strip()
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        # Capture 100% of errors. Tracing samples kept low to stay
        # comfortably inside Sentry's free tier (5k errors/month). Bump
        # traces_sample_rate later when we have real traffic + want APM.
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
        # Drop request bodies from events - they may contain PII or
        # OAuth tokens. Headers + path + method are still attached.
        send_default_pii=False,
        integrations=[
            FastApiIntegration(),
            SqlalchemyIntegration(),
        ],
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Simple create_all for MVP — we'll add Alembic when the schema starts evolving.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="ARCHIVE336", lifespan=lifespan)

app.include_router(auth_routes.router, prefix="/api/auth", tags=["auth"])
app.include_router(billing_routes.router, prefix="/api/billing", tags=["billing"])
app.include_router(youtube_routes.router, prefix="/api/youtube", tags=["youtube"])
app.include_router(admin_routes.router, prefix="/api/admin", tags=["admin"])
app.include_router(dev_routes.router, prefix="/api/dev", tags=["dev"])
app.include_router(support_routes.router)
app.include_router(errors_routes.router, prefix="/api", tags=["errors"])


_log = logging.getLogger("archive336.unhandled")


@app.exception_handler(Exception)
async def log_uncaught_exceptions(request: Request, exc: Exception):
    """Catch any uncaught exception leaking out of a route, write an
    ErrorLog row with traceback + request context, then return the
    standard 500 JSON. Errors land in the /dev admin page so we can
    debug user issues without needing to reproduce them.

    Lookup of the user is best-effort and synchronous - we never want
    THIS handler to throw and lose the error. If anything fails inside,
    the exception is swallowed so the original 500 still goes out.
    """
    tb = traceback.format_exc()
    _log.error("unhandled exception on %s %s\n%s", request.method, request.url.path, tb)
    # Explicitly forward to Sentry. Our custom exception handler returns
    # a JSON 500 instead of re-raising, which means Sentry's FastAPI
    # integration may not auto-capture the original exception. A direct
    # capture_exception ensures it lands in the dashboard either way.
    # No-op when Sentry isn't initialized.
    sentry_sdk.capture_exception(exc)

    try:
        with SessionLocal() as db:
            user_id = None
            session_token = request.cookies.get(SESSION_COOKIE_NAME)
            if session_token:
                sess = db.get(UserSession, session_token)
                if sess is not None:
                    user_id = sess.user_id
            db.add(
                ErrorLog(
                    user_id=user_id,
                    source="server",
                    message=f"{type(exc).__name__}: {exc}"[:4000],
                    stack=tb[:20000],
                    request_path=str(request.url.path)[:2000],
                    request_method=request.method,
                    status_code=500,
                    user_agent=request.headers.get("user-agent"),
                )
            )
            db.commit()
    except Exception:  # noqa: BLE001
        _log.exception("failed to persist server error to ErrorLog")

    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}
