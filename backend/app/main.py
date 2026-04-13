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

app = FastAPI(title="Slackbot AI Modular")

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
handler = Mangum(app)