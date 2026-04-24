import os
import json
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from mangum import Mangum

from app.logger import logger
from app.constants import CORS_ORIGINS
from app.routes import router
from app.db import init_db
from contextlib import asynccontextmanager

import threading

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run schema init in a background thread so it doesn't block
    # the first request during Lambda cold start
    threading.Thread(target=init_db, daemon=True).start()
    yield

app = FastAPI(title="Slackbot AI Modular", lifespan=lifespan)

# ── CORS ──────────────────────────────────────────────────────────────────────
origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()] if CORS_ORIGINS else []
if not origins:
    logger.warning("CORS_ORIGINS not set — cross-origin requests may be blocked")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── ERROR HANDLING ────────────────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.warning("Validation error", extra={"errors": exc.errors(), "body": await request.body()})
    return JSONResponse(status_code=422, content={"ok": False, "error": exc.errors()})

# ── ROUTES ────────────────────────────────────────────────────────────────────
app.include_router(router)

# ── LAMBDA HANDLER ────────────────────────────────────────────────────────────
handler = Mangum(app, lifespan="on")

# ── KEEP-WARM PING HANDLER ────────────────────────────────────────────────────
# CloudWatch Events calls Lambda directly (not via HTTP) with a scheduled event.
# Mangum wraps HTTP only, so we intercept the raw Lambda event here.
_http_handler = handler

def handler(event, context):  # noqa: F811 — intentional override
    # CloudWatch scheduled warm-up ping — return immediately without processing
    if event.get("source") == "aws.events" or event.get("detail-type") == "Scheduled Event":
        return {"statusCode": 200, "body": "warm"}
    return _http_handler(event, context)