import os
import re
import json
import uuid
import time
import hmac
import hashlib
import logging
import sys
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

import boto3
import requests
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key, Attr
from fastapi import FastAPI, Request, Query, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from mangum import Mangum
from pydantic import BaseModel, Field, field_validator

# ── STRUCTURED LOGGING ────────────────────────────────────────────────────────
class StructuredLogger(logging.Logger):
    """Logger that emits JSON lines for easy CloudWatch querying."""
    def _log_json(self, level: str, msg: str, **extra):
        record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": level,
            "message": msg,
            **extra,
        }
        print(json.dumps(record, default=str), file=sys.stdout, flush=True)

    def info(self, msg, *args, **kwargs):      # type: ignore[override]
        extra = kwargs.pop("extra", {})
        super().info(msg, *args, **kwargs)
        self._log_json("INFO",  str(msg), **extra)

    def warning(self, msg, *args, **kwargs):   # type: ignore[override]
        extra = kwargs.pop("extra", {})
        super().warning(msg, *args, **kwargs)
        self._log_json("WARNING", str(msg), **extra)

    def error(self, msg, *args, **kwargs):     # type: ignore[override]
        extra = kwargs.pop("extra", {})
        super().error(msg, *args, **kwargs)
        self._log_json("ERROR", str(msg), **extra)


logging.setLoggerClass(StructuredLogger)
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger: StructuredLogger = logging.getLogger(__name__)  # type: ignore[assignment]

# ── REQUEST SIZE LIMITS ───────────────────────────────────────────────────────
MAX_BODY_BYTES   = 64 * 1024   # 64 KB hard limit for all POST bodies
MAX_QUESTION_LEN = 1_000       # chars
MAX_CHANNEL_IDS  = 20          # max channels in multi-chat/search
MAX_QUERY_LEN    = 200         # chars for keyword search
MAX_USERNAME_LEN = 50          # chars for username filter

# ── ENV CONFIG ────────────────────────────────────────────────────────────────
AWS_REGION           = os.getenv("AWS_REGION", "ap-south-1").strip()
SECRET_PREFIX        = os.getenv("SECRET_PREFIX", "slackbot").strip()
CLIENT_ID            = os.getenv("SLACK_CLIENT_ID", "").strip()
CLIENT_SECRET        = os.getenv("SLACK_CLIENT_SECRET", "").strip()
REDIRECT_URI         = os.getenv("SLACK_REDIRECT_URI", "").strip()
SLACK_SCOPES         = os.getenv(
    "SLACK_SCOPES",
    "channels:history,chat:write,users:read,groups:history,channels:read,groups:read,channels:join",
).strip()
CORS_ORIGINS         = os.getenv("CORS_ORIGINS", "").strip()
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "").strip()
DDB_TABLE            = os.getenv("DDB_TABLE", "").strip()
SESSIONS_TABLE       = os.getenv("SESSIONS_TABLE", "slackbot_sessions").strip()
GROQ_API_KEY         = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL           = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct").strip()
GROQ_URL             = "https://api.groq.com/openai/v1/chat/completions"
UI_BASE_URL          = os.getenv("UI_BASE_URL", "").rstrip("/")
SESSION_COOKIE_NAME  = "sb_session"
SESSION_TTL_HOURS    = 72
IS_PROD              = os.getenv("ENV", "dev").strip().lower() == "prod"

# ── CORS ORIGINS ──────────────────────────────────────────────────────────────
# Set CORS_ORIGINS env var to your CloudFront domain(s), comma-separated.
# Example: https://xxxx.cloudfront.net  or  https://xxxx.cloudfront.net,https://yourdomain.com
def _build_cors_origins() -> list[str]:
    if not CORS_ORIGINS:
        logger.warning("CORS_ORIGINS not set — all cross-origin requests will be blocked")
        return []
    return [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]

origins = _build_cors_origins()

app = FastAPI(title="Slackbot Full MVP")

# ── REQUEST SIZE LIMIT MIDDLEWARE ─────────────────────────────────────────────
@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        logger.warning("Request body too large", extra={
            "path": request.url.path,
            "content_length": content_length,
            "limit_bytes": MAX_BODY_BYTES,
        })
        return JSONResponse(
            status_code=413,
            content={"ok": False, "error": f"Request body exceeds {MAX_BODY_BYTES // 1024} KB limit"},
        )
    return await call_next(request)

# ── SECURITY HEADERS MIDDLEWARE ───────────────────────────────────────────────
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    # Prevent MIME-type sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Prevent clickjacking via iframes
    response.headers["X-Frame-Options"] = "DENY"
    # Don't leak referrer info to third parties
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Disable browser features you don't use
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # Force HTTPS in production
    if IS_PROD:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Content Security Policy
    # 'unsafe-inline' is required because index.html uses inline <style> and <script>
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "connect-src 'self' https://api.groq.com; "
        "img-src 'self' data:; "
        "frame-ancestors 'none';"
    )
    return response

# ── GLOBAL EXCEPTION HANDLERS ─────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    logger.warning("Validation error", extra={
        "path": request.url.path,
        "errors": [{"field": ".".join(str(l) for l in e["loc"]), "msg": e["msg"]} for e in errors],
    })
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "error": "Validation failed",
            "details": [
                {"field": ".".join(str(l) for l in e["loc"]), "message": e["msg"]}
                for e in errors
            ],
        },
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code >= 500:
        logger.error("HTTP error", extra={"path": request.url.path, "status": exc.status_code, "detail": exc.detail})
    else:
        logger.warning("HTTP error", extra={"path": request.url.path, "status": exc.status_code, "detail": exc.detail})
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": exc.detail},
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception", extra={"path": request.url.path, "error": str(exc), "type": type(exc).__name__})
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "An internal server error occurred. Please try again."},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Requested-With"],
    max_age=600,
)

secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)
dynamodb       = boto3.resource("dynamodb", region_name=AWS_REGION)
ddb_table      = dynamodb.Table(DDB_TABLE)      if DDB_TABLE      else None
sessions_table = dynamodb.Table(SESSIONS_TABLE) if SESSIONS_TABLE else None

frontend_default = Path(__file__).with_name("index.html")
FRONTEND_PATH    = Path(os.getenv("FRONTEND_PATH", str(frontend_default)))


# ═══════════════════════════════════════════════════════════════════════════════
# RATE LIMITING
# ═══════════════════════════════════════════════════════════════════════════════
# In-memory sliding window. Resets on Lambda cold start — good enough for
# protecting against abuse. Chat and search have separate limits.

_rate_store: dict = defaultdict(list)
RATE_LIMIT_CHAT   = 30   # max Ask AI requests per session per minute
RATE_LIMIT_SEARCH = 60   # max Search requests per session per minute
RATE_WINDOW_SECS  = 60   # rolling window in seconds

def _check_rate_limit(session_id: str, action: str, limit: int) -> None:
    key = f"{session_id}:{action}"
    now = time.time()
    _rate_store[key] = [t for t in _rate_store[key] if now - t < RATE_WINDOW_SECS]
    if len(_rate_store[key]) >= limit:
        logger.warning("Rate limit hit", extra={
            "session_id": session_id[:8],
            "action": action,
            "count": len(_rate_store[key]),
            "limit": limit,
        })
        raise HTTPException(429, "Too many requests — please wait a moment before trying again")
    _rate_store[key].append(now)


# ═══════════════════════════════════════════════════════════════════════════════
# GENERAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def no_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"]  = "no-cache"
    response.headers["Expires"] = "0"
    return response


def secret_name(team_id: str) -> str:
    return f"{SECRET_PREFIX}/{team_id}"


def upsert_secret(name: str, payload: dict) -> None:
    body = json.dumps(payload)
    try:
        secrets_client.create_secret(Name=name, SecretString=body)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            secrets_client.put_secret_value(SecretId=name, SecretString=body)
        else:
            raise


def read_secret(name: str) -> Optional[dict]:
    try:
        resp = secrets_client.get_secret_value(SecretId=name)
        return json.loads(resp.get("SecretString", "{}"))
    except ClientError as e:
        return {"_error": e.response["Error"]["Code"], "_message": str(e)}


def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 10:
        return token[:2] + "..." + token[-2:]
    return token[:4] + "..." + token[-4:]


def verify_slack_signature(signing_secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > 300:
        return False
    base   = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest("v0=" + digest, signature)


def require_ddb():
    if ddb_table is None:
        raise HTTPException(500, "DDB_TABLE environment variable is not set")


def _date_to_sk(date_str: str, end_of_day: bool = False) -> str:
    epoch = int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())
    return str(epoch + 86399 if end_of_day else epoch)


def _ts_human(ts: str) -> str:
    try:
        return datetime.utcfromtimestamp(float(str(ts).split(".")[0])).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT SANITIZATION & VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

# Prompt injection patterns — questions matching these are rejected
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|above|prior)\s+instructions",
    r"disregard\s+(your\s+)?(previous\s+)?rules",
    r"you\s+are\s+now\s+a",
    r"act\s+as\s+(a\s+|an\s+)?(?!slack)",
    r"new\s+system\s+prompt",
    r"forget\s+(everything|all|your)",
    r"reveal\s+(your\s+)?(system\s+)?prompt",
    r"print\s+(your\s+)?(system\s+)?prompt",
    r"what\s+(are\s+)?your\s+instructions",
    r"override\s+(your\s+)?instructions",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def _sanitize_text(v: str, field_name: str, max_len: int) -> str:
    """Strip whitespace, remove null bytes, enforce length limit."""
    if v is None:
        return v
    v = v.strip().replace("\x00", "")
    if len(v) > max_len:
        raise ValueError(f"{field_name} is too long (max {max_len} characters)")
    return v


def _check_prompt_injection(question: str) -> None:
    """Raise 400 if the question looks like a prompt injection attempt."""
    if _INJECTION_RE.search(question):
        logger.warning("Prompt injection attempt blocked", extra={
            "question_preview": question[:80]
        })
        raise HTTPException(400, "Invalid question format")


def _validate_search_query(v: Optional[str]) -> Optional[str]:
    """Validate and sanitize the keyword search query."""
    if v is None:
        return v
    return _sanitize_text(v, "Search query", MAX_QUERY_LEN)


def _validate_username_param(v: Optional[str]) -> Optional[str]:
    """Validate username filter — allow only safe characters."""
    if v is None:
        return v
    v = _sanitize_text(v, "Username", MAX_USERNAME_LEN)
    if v and not re.match(r"^[A-Za-z0-9._\- ]+$", v):
        raise ValueError("Username contains invalid characters")
    return v


# ═══════════════════════════════════════════════════════════════════════════════
# USER CACHE  (pk = "{team_id}#__users__",  sk = user_id)
# Stores display_name + real_name so we can look up user_id by username.
# ═══════════════════════════════════════════════════════════════════════════════

def _user_pk(team_id: str) -> str:
    return f"{team_id}#__users__"


def get_cached_user(team_id: str, user_id: str) -> Optional[dict]:
    """Return the cached user record for a user_id, or None."""
    if ddb_table is None:
        return None
    try:
        resp = ddb_table.get_item(Key={"pk": _user_pk(team_id), "sk": user_id})
        return resp.get("Item")
    except Exception:
        return None


def upsert_cached_user(team_id: str, user_id: str, display_name: str, real_name: str) -> None:
    """Store / update a user record in the cache table."""
    if ddb_table is None:
        return
    try:
        ddb_table.put_item(Item={
            "pk":           _user_pk(team_id),
            "sk":           user_id,
            "user_id":      user_id,
            "display_name": display_name,
            "real_name":    real_name,
            "cached_at":    datetime.utcnow().isoformat() + "Z",
        })
    except Exception as e:
        logger.warning(f"[user-cache] upsert failed for {user_id}: {e}")


def resolve_user_id(team_id: str, username: str, bot_token: str) -> Optional[str]:
    """
    Given a display name / real name (e.g. 'vrisha'), return the matching
    Slack user_id.  Checks the DynamoDB cache first; falls back to the
    Slack users.list API and populates the cache.
    Returns None if no match is found.
    """
    if not username or not bot_token:
        return None

    needle = username.strip().lower()

    # ── 1. Check cache (scan the user-cache partition for this team) ──────────
    if ddb_table is not None:
        try:
            resp = ddb_table.query(
                KeyConditionExpression=Key("pk").eq(_user_pk(team_id)),
            )
            for item in resp.get("Items", []):
                dn = (item.get("display_name") or "").lower()
                rn = (item.get("real_name")    or "").lower()
                if needle in dn or needle in rn or dn.startswith(needle) or rn.startswith(needle):
                    logger.info(f"[user-cache] resolved '{username}' → {item['user_id']} (cache hit)")
                    return item["user_id"]
        except Exception as e:
            logger.warning(f"[user-cache] cache query failed: {e}")

    # ── 2. Fetch full user list from Slack and populate cache ─────────────────
    logger.info(f"[user-cache] cache miss for '{username}', fetching users.list from Slack")
    cursor = None
    matched_id: Optional[str] = None

    while True:
        params: dict = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r    = requests.get(
                "https://slack.com/api/users.list",
                headers={"Authorization": f"Bearer {bot_token}"},
                params=params,
                timeout=20,
            )
            data = r.json()
        except Exception as e:
            logger.warning(f"[user-cache] users.list request failed: {e}")
            break

        if not data.get("ok"):
            logger.warning(f"[user-cache] users.list error: {data.get('error')}")
            break

        for member in data.get("members", []):
            uid     = member.get("id", "")
            profile = member.get("profile") or {}
            dn      = (profile.get("display_name") or member.get("name") or "").strip()
            rn      = (profile.get("real_name")    or "").strip()
            if uid:
                upsert_cached_user(team_id, uid, dn, rn)
                if matched_id is None:
                    dn_l = dn.lower()
                    rn_l = rn.lower()
                    if needle in dn_l or needle in rn_l or dn_l.startswith(needle) or rn_l.startswith(needle):
                        matched_id = uid
                        logger.info(f"[user-cache] resolved '{username}' → {uid} (display='{dn}', real='{rn}')")

        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break

    return matched_id


def resolve_username_for_message(team_id: str, user_id: str, bot_token: str) -> str:
    """
    Return display_name for a user_id.  Uses cache; falls back to
    users.info API for a single user if not cached yet.
    """
    if not user_id:
        return ""
    cached = get_cached_user(team_id, user_id)
    if cached:
        return cached.get("display_name") or cached.get("real_name") or user_id

    try:
        r    = requests.get(
            "https://slack.com/api/users.info",
            headers={"Authorization": f"Bearer {bot_token}"},
            params={"user": user_id},
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            profile = (data.get("user") or {}).get("profile") or {}
            dn = (profile.get("display_name") or data["user"].get("name") or "").strip()
            rn = (profile.get("real_name")    or "").strip()
            upsert_cached_user(team_id, user_id, dn, rn)
            return dn or rn or user_id
    except Exception:
        pass
    return user_id


_AT_MENTION = re.compile(r"@([A-Za-z][A-Za-z0-9._-]{1,30})")


def extract_username_from_question(question: str) -> Optional[str]:
    """
    Extract a username only if the user explicitly typed @name in their question.
    Returns the name without the @ sign, or None.
    e.g. "what did @vrisha say last?" → "vrisha"
    """
    m = _AT_MENTION.search(question)
    if m:
        name = m.group(1).strip()
        logger.info(f"[name-extract] @mention extracted '{name}' from question: {question!r}")
        return name
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# SESSION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════
#
# DynamoDB table: slackbot_sessions
#   PK  = session_id  (string, UUID)
#   Attributes:
#     team_ids   (list of strings)  — workspaces this session owns
#     created_at (string ISO)
#     expires_at (number, epoch)    — used as DynamoDB TTL attribute
#
# FLOW:
#  1. User visits the page → GET /api/session auto-creates a session + sets
#     an HttpOnly cookie named "sb_session".
#  2. User clicks "Connect Slack" → OAuth popup opens.
#  3. OAuth callback fires → team_id is bound to the session via bind_team_to_session().
#  4. Every protected endpoint calls require_team_access(request, team_id) which:
#       a. Reads the cookie
#       b. Looks up the session in DynamoDB
#       c. Checks that team_id is in session.team_ids
#       d. Raises 403 if not

def _require_sessions_table():
    if sessions_table is None:
        raise HTTPException(500, "SESSIONS_TABLE not configured")


def create_session() -> str:
    _require_sessions_table()
    session_id = str(uuid.uuid4())
    expires_at = int((datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)).timestamp())
    sessions_table.put_item(Item={
        "session_id": session_id,
        "team_ids":   [],
        "created_at": datetime.utcnow().isoformat() + "Z",
        "expires_at": expires_at,
    })
    logger.info(f"[session] created {session_id}")
    return session_id


def get_session(session_id: str) -> Optional[dict]:
    if not session_id or sessions_table is None:
        return None
    try:
        resp = sessions_table.get_item(Key={"session_id": session_id})
        item = resp.get("Item")
        if not item:
            return None
        if item.get("expires_at", 0) < int(time.time()):
            return None
        return item
    except Exception as e:
        logger.warning(f"[session] get error: {e}")
        return None


def bind_team_to_session(session_id: str, team_id: str) -> None:
    _require_sessions_table()
    sess = get_session(session_id)
    if not sess:
        return
    current = sess.get("team_ids", [])
    if team_id not in current:
        current.append(team_id)
    sessions_table.update_item(
        Key={"session_id": session_id},
        UpdateExpression="SET team_ids = :tids",
        ExpressionAttributeValues={":tids": current},
    )
    logger.info(f"[session] bound team {team_id} to session {session_id}")


def unbind_team_from_session(session_id: str, team_id: str) -> None:
    if not session_id or sessions_table is None:
        return
    sess = get_session(session_id)
    if not sess:
        return
    updated = [t for t in sess.get("team_ids", []) if t != team_id]
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET team_ids = :tids",
            ExpressionAttributeValues={":tids": updated},
        )
    except Exception as e:
        logger.warning(f"[session] unbind error: {e}")


def get_or_create_session(request: Request, response: Response) -> tuple[str, dict]:
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    sess       = get_session(cookie_val) if cookie_val else None
    if not sess:
        session_id = create_session()
        sess       = get_session(session_id) or {}
        _set_session_cookie(response, session_id)
        return session_id, sess
    return cookie_val, sess


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key      = SESSION_COOKIE_NAME,
        value    = session_id,
        httponly = True,
        secure   = IS_PROD,
        samesite = "lax",
        max_age  = SESSION_TTL_HOURS * 3600,
        path     = "/",
    )


def require_session(request: Request) -> dict:
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_val:
        raise HTTPException(401, "No session — connect a Slack workspace first")
    sess = get_session(cookie_val)
    if not sess:
        raise HTTPException(401, "Session expired — please reconnect")
    return sess


def require_team_access(request: Request, team_id: str) -> dict:
    sess    = require_session(request)
    allowed = sess.get("team_ids", [])
    if team_id not in allowed:
        logger.warning(f"[auth] DENIED team={team_id} session={sess.get('session_id')} allowed={allowed}")
        raise HTTPException(403, "Access denied — this workspace does not belong to your session")
    return sess


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE RETRIEVAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

# Words that signal "give me recent/latest messages" — NOT content to search for
# Words stripped before keyword scoring — temporal words, question words, and stop words
# that never appear as meaningful content in message bodies.
_RECENCY_WORDS = frozenset([
    # temporal / recency
    "last", "latest", "recent", "newest", "today", "yesterday",
    "just", "now", "current", "recently", "new",
    "next", "week", "soon", "tomorrow", "upcoming", "future",
    # question words — never content keywords
    "what", "who", "whose", "whom", "where", "when", "why", "how",
    # stop words that score noisily against message bodies
    "about", "said", "say", "says", "did", "does", "from",
    "the", "and", "for", "with", "its", "this", "that", "tell",
])

def _is_recency_query(q: str) -> bool:
    """True if the question is asking about recency/time, not specific content."""
    words = set(re.findall(r"\w+", q.lower()))
    return bool(words & _RECENCY_WORDS)

def _content_keywords(q: str) -> list[str]:
    """Return only the meaningful content keywords — strips recency words and short noise."""
    return [w for w in re.findall(r"\w+", q.lower())
            if w not in _RECENCY_WORDS and len(w) > 2]

def _score_messages(items: list[dict], q: str) -> list[dict]:
    keywords = _content_keywords(q)

    if not keywords:
        return [i for i in items
                if not re.search(r"<@\w+> has (joined|left)", (i.get("text") or "").lower())]

    scored = []
    for item in items:
        text = (item.get("text") or "").lower()
        if re.search(r"<@\w+> has (joined|left)", text):
            continue
        score  = sum(text.count(kw) for kw in keywords)
        score += sum(2 for kw in keywords if kw in text[:80])
        if len(keywords) > 1 and " ".join(keywords) in text:
            score += 5
        if len(text) > 800:
            score = score * 800 / len(text)
        if len(text) < 20:
            score *= 0.5
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    result = [item for score, item in scored if score > 0]

    if _is_recency_query(q) and result:
        result.sort(key=lambda m: m.get("sk") or m.get("ts") or "", reverse=True)

    return result


def _format_messages(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        text = (item.get("text") or "").strip()
        out.append({
            "message_ts":      item.get("ts") or item.get("sk", ""),
            "user_id":         item.get("user_id", "unknown"),
            "username":        item.get("username", ""),
            "text":            text,
            "snippet":         text[:1200] + ("…" if len(text) > 1200 else ""),
            "channel_id":      item.get("channel_id", ""),
            "team_id":         item.get("team_id", ""),
            "timestamp_human": _ts_human(item.get("ts") or item.get("sk", "")),
        })
    return out


def retrieve_messages(
    team_id: str, channel_id: str,
    q: Optional[str] = None, from_date: Optional[str] = None,
    to_date: Optional[str] = None, user_id: Optional[str] = None,
    limit: int = 200, top_k: int = 10,
    username: Optional[str] = None, bot_token: Optional[str] = None,
) -> list[dict]:
    require_ddb()

    if username and not user_id and bot_token:
        resolved = resolve_user_id(team_id, username, bot_token)
        if resolved:
            user_id = resolved
        else:
            logger.info(f"[retrieve] username '{username}' not found in workspace {team_id}")
            return []

    pk       = f"{team_id}#{channel_id}"
    key_expr = Key("pk").eq(pk)
    if from_date and to_date:
        key_expr = key_expr & Key("sk").between(_date_to_sk(from_date), _date_to_sk(to_date, end_of_day=True))
    elif from_date:
        key_expr = key_expr & Key("sk").gte(_date_to_sk(from_date))
    elif to_date:
        key_expr = key_expr & Key("sk").lte(_date_to_sk(to_date, end_of_day=True))
    kwargs = {"KeyConditionExpression": key_expr, "Limit": limit, "ScanIndexForward": False}
    if user_id:
        kwargs["FilterExpression"] = Attr("user_id").eq(user_id)
    try:
        response = ddb_table.query(**kwargs)
        items    = response.get("Items", [])
    except Exception as e:
        raise RuntimeError(f"DynamoDB query failed: {e}")
    items = [i for i in items if not re.search(r"<@\w+> has (joined|left)", (i.get("text") or "").lower())]
    if not q or not q.strip():
        return _format_messages(items[:top_k])

    if not _content_keywords(q):
        return _format_messages(items[:top_k])

    if user_id:
        return _format_messages(items[:top_k])

    matched = _score_messages(items, q)
    return _format_messages(matched[:top_k])


def retrieve_messages_multi(
    team_id: str, channel_ids: list[str],
    q: Optional[str] = None, from_date: Optional[str] = None,
    to_date: Optional[str] = None, user_id: Optional[str] = None,
    limit: int = 200, top_k: int = 10,
    username: Optional[str] = None, bot_token: Optional[str] = None,
) -> list[dict]:
    if username and not user_id and bot_token:
        resolved = resolve_user_id(team_id, username, bot_token)
        if resolved:
            user_id = resolved
        else:
            logger.info(f"[retrieve_multi] username '{username}' not found in workspace {team_id}")
            return []

    all_raw: list[dict] = []
    for channel_id in channel_ids:
        pk       = f"{team_id}#{channel_id}"
        key_expr = Key("pk").eq(pk)
        if from_date and to_date:
            key_expr = key_expr & Key("sk").between(_date_to_sk(from_date), _date_to_sk(to_date, end_of_day=True))
        elif from_date:
            key_expr = key_expr & Key("sk").gte(_date_to_sk(from_date))
        elif to_date:
            key_expr = key_expr & Key("sk").lte(_date_to_sk(to_date, end_of_day=True))
        kwargs = {"KeyConditionExpression": key_expr, "Limit": limit, "ScanIndexForward": False}
        if user_id:
            kwargs["FilterExpression"] = Attr("user_id").eq(user_id)
        try:
            resp = ddb_table.query(**kwargs)
            items = resp.get("Items", [])
            items = [i for i in items if not re.search(
                r"<@\w+> has (joined|left)", (i.get("text") or "").lower())]
            all_raw.extend(items)
        except Exception as e:
            logger.warning(f"[retrieve_multi] DDB query failed for {channel_id}: {e}")

    if not all_raw:
        return []

    if user_id:
        all_raw.sort(key=lambda m: m.get("sk") or m.get("ts") or "", reverse=True)
        return _format_messages(all_raw[:top_k])

    content_kws = _content_keywords(q) if q and q.strip() else []

    if content_kws:
        scored_pool = []
        for item in all_raw:
            text = (item.get("text") or "").lower()
            score  = sum(text.count(kw) for kw in content_kws)
            score += sum(2 for kw in content_kws if kw in text[:80])
            if len(content_kws) > 1 and " ".join(content_kws) in text:
                score += 5
            if len(text) > 800:
                score = score * 800 / len(text)
            if len(text) < 20:
                score *= 0.5
            if score > 0:
                scored_pool.append((score, item))

        scored_pool.sort(key=lambda x: x[0], reverse=True)
        if _is_recency_query(q):
            scored_pool.sort(key=lambda x: x[1].get("sk") or x[1].get("ts") or "", reverse=True)

        top_items = [item for _, item in scored_pool[:top_k]]
    else:
        all_raw.sort(key=lambda m: m.get("sk") or m.get("ts") or "", reverse=True)
        top_items = all_raw[:top_k]

    return _format_messages(top_items)


def _build_context(messages: list[dict], channel_prefix: bool = False) -> tuple[str, int]:
    """
    Build the LLM context string from retrieved messages.
    Uses full message text and stops at CONTEXT_MAX_CHARS to control token cost.
    Returns (context_string, messages_included_count).
    """
    lines: list[str] = []
    total = 0
    for i, m in enumerate(messages):
        text = (m.get("text") or "").strip()
        who  = m.get("username") or m.get("user_id") or "unknown"
        ch   = f" | #{m.get('channel_id','')}" if channel_prefix and m.get("channel_id") else ""
        line = f"[{i+1}] {m.get('timestamp_human','')} | {who}{ch}: {text}"
        if total + len(line) > CONTEXT_MAX_CHARS:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines), len(lines)


def _augment_question_with_senders(question: str, messages: list[dict]) -> str:
    """
    If the question asks WHO, extract unique sender names from the
    retrieved messages and inject them directly into the question.
    """
    who_words = {"who", "whose", "whom"}
    q_words = set(question.lower().split())
    if not (q_words & who_words):
        return question

    senders = []
    seen = set()
    for m in messages:
        name = (m.get("username") or m.get("user_id") or "").strip()
        if name and name not in seen:
            senders.append(name)
            seen.add(name)

    if not senders:
        return question

    sender_str = ", ".join(senders)
    return f"{question} [NOTE: The message(s) were sent by: {sender_str}. You MUST name them in your answer.]"


GROQ_TIMEOUT_CONNECT = 5   # seconds to establish connection
GROQ_TIMEOUT_READ    = 30  # seconds to read response

# ── TOKEN / COST CONTROL ─────────────────────────────────────────────────────
CONTEXT_MAX_CHARS = 8_000  # hard cap on total context string sent to LLM
MAX_TOKENS_SINGLE = 768    # max output tokens for /api/chat
MAX_TOKENS_MULTI  = 900    # max output tokens for /api/chat/multi
MAX_TOKENS_BOT    = 600    # max output tokens for Slack bot replies

# ── POSITIONAL QUERY DETECTION ───────────────────────────────────────────────
_POSITIONAL_RE = re.compile(
    r"\b(first|last|latest|earliest|recent|newest|oldest)\b.{0,30}(message|msg|post|said|sent|thing|text)",
    re.IGNORECASE,
)
_FIRST_RE = re.compile(r"\b(first|earliest|oldest)\b", re.IGNORECASE)


def _is_positional_query(q: str) -> bool:
    """True if user is asking for the first or last message by position/time."""
    return bool(_POSITIONAL_RE.search(q))


def retrieve_first_or_last(
    team_id: str, channel_id: str, position: str = "last", top_k: int = 3,
) -> list[dict]:
    """
    Fetch the chronologically first or last messages directly from DynamoDB,
    bypassing semantic scoring entirely.
    position='last'  → ScanIndexForward=False (newest first)
    position='first' → ScanIndexForward=True  (oldest first)
    """
    require_ddb()
    forward = position == "first"
    try:
        resp = ddb_table.query(
            KeyConditionExpression=Key("pk").eq(f"{team_id}#{channel_id}"),
            ScanIndexForward=forward,
            Limit=top_k * 3,  # over-fetch to allow join/left filtering
        )
        items = resp.get("Items", [])
    except Exception as e:
        raise RuntimeError(f"DynamoDB positional query failed: {e}")

    items = [i for i in items if not re.search(
        r"<@\w+> has (joined|left)", (i.get("text") or "").lower()
    )]
    return _format_messages(items[:top_k])


def _groq_complete(
    prompt: str,
    max_tokens: int = 1024,
    system: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
) -> str:
    """
    Call Groq API with explicit connect + read timeouts.
    Accepts an optional system prompt and optional multi-turn conversation history.
    conversation_history: list of {"role": "user"|"assistant", "content": "..."}
    Returns a safe fallback message instead of raising on timeout/5xx.
    """
    request_id = str(uuid.uuid4())[:8]
    if not GROQ_API_KEY:
        logger.error("Groq API key missing", extra={"request_id": request_id})
        raise HTTPException(500, "GROQ_API_KEY not set")

    messages_payload: list[dict] = []
    if system:
        messages_payload.append({"role": "system", "content": system})
    # Inject conversation history (prior turns) before the current prompt
    if conversation_history:
        messages_payload.extend(conversation_history)
    messages_payload.append({"role": "user", "content": prompt})

    payload = {
        "model": GROQ_MODEL,
        "messages": messages_payload,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    start = time.time()
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=(GROQ_TIMEOUT_CONNECT, GROQ_TIMEOUT_READ),
        )
    except requests.exceptions.ConnectTimeout:
        elapsed = round(time.time() - start, 2)
        logger.error("Groq connect timeout", extra={"request_id": request_id, "elapsed_s": elapsed})
        return "⚠️ The AI service took too long to connect. Please try again in a moment."
    except requests.exceptions.ReadTimeout:
        elapsed = round(time.time() - start, 2)
        logger.error("Groq read timeout", extra={"request_id": request_id, "elapsed_s": elapsed})
        return "⚠️ The AI service timed out while generating a response. Try a shorter question or smaller date range."
    except requests.exceptions.RequestException as exc:
        logger.error("Groq network error", extra={"request_id": request_id, "error": str(exc)})
        return "⚠️ Could not reach the AI service due to a network error. Please try again."

    elapsed = round(time.time() - start, 2)

    try:
        data = resp.json()
    except ValueError:
        logger.error("Groq non-JSON response", extra={"request_id": request_id, "status": resp.status_code})
        return "⚠️ Received an unexpected response from the AI service."

    if resp.status_code == 429:
        logger.warning("Groq rate limited", extra={"request_id": request_id})
        return "⚠️ The AI service is currently rate-limited. Please wait a few seconds and try again."

    if resp.status_code >= 500:
        logger.error("Groq 5xx error", extra={"request_id": request_id, "status": resp.status_code})
        return "⚠️ The AI service returned a server error. Please try again shortly."

    if resp.status_code != 200:
        logger.error("Groq unexpected status", extra={"request_id": request_id, "status": resp.status_code, "body": str(data)[:200]})
        raise HTTPException(502, f"Groq error {resp.status_code}: {data}")

    answer = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    logger.info("Groq call succeeded", extra={"request_id": request_id, "elapsed_s": elapsed, "tokens": data.get("usage", {}).get("total_tokens")})
    return answer


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def home():
    if FRONTEND_PATH.exists():
        return no_cache(FileResponse(str(FRONTEND_PATH)))
    return HTMLResponse(f"<h3>UI not found at {FRONTEND_PATH}</h3>", status_code=500)


@app.get("/health")
@app.get("/api/health")
def health(response: Response):
    no_cache(response)
    return {
        "status": "ok", "region": AWS_REGION,
        "ddb_table": DDB_TABLE, "sessions_table": SESSIONS_TABLE,
        "client_id_present": bool(CLIENT_ID),
    }


# ── SESSION ENDPOINTS ─────────────────────────────────────────────────────────

@app.get("/api/session")
def api_get_session(request: Request, response: Response):
    """Called on page load. Creates session if needed. Returns owned team_ids."""
    no_cache(response)
    session_id, sess = get_or_create_session(request, response)
    return {"ok": True, "session_id": session_id, "team_ids": sess.get("team_ids", [])}


@app.post("/api/logout")
def api_logout(request: Request, response: Response):
    no_cache(response)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


# ── OAUTH ─────────────────────────────────────────────────────────────────────

@app.get("/install")
@app.get("/api/install")
def install():
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        return HTMLResponse("<h3>Missing ENV</h3>", status_code=500)
    params = {"client_id": CLIENT_ID, "scope": SLACK_SCOPES,
              "redirect_uri": REDIRECT_URI, "state": "slackbot_mvp"}
    return RedirectResponse("https://slack.com/oauth/v2/authorize?" + urlencode(params))


@app.get("/oauth/callback")
@app.get("/api/oauth/callback")
def oauth_callback(
    request: Request, response: Response,
    code: str | None = None, error: str | None = None, state: str | None = None,
):
    def _err(msg):
        return HTMLResponse(f"""<html><body><script>
        if(window.opener)window.opener.postMessage({{"type":"slack_oauth_error","error":{json.dumps(msg)}}},"*");
        window.close();</script><p>Failed.</p></body></html>""", status_code=400)

    if error:   return _err(error)
    if not code: return _err("missing_code")

    r    = requests.post("https://slack.com/api/oauth.v2.access",
                         data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                               "code": code, "redirect_uri": REDIRECT_URI}, timeout=20)
    data = r.json()
    if not data.get("ok"):
        return _err(json.dumps(data))

    team      = data.get("team") or {}
    team_id   = team.get("id")
    team_name = team.get("name")
    bot_token = data.get("access_token")

    if not team_id or not bot_token:
        return _err("missing_team_or_token")

    try:
        upsert_secret(secret_name(team_id), {
            "team_id": team_id, "team_name": team_name,
            "bot_user_id": data.get("bot_user_id"),
            "bot_token": bot_token, "scope": data.get("scope"),
        })
    except Exception as e:
        return _err(str(e))

    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_val and get_session(cookie_val):
        bind_team_to_session(cookie_val, team_id)
    else:
        new_sid = create_session()
        bind_team_to_session(new_sid, team_id)
        _set_session_cookie(response, new_sid)

    return HTMLResponse(f"""<html><body><script>
    if(window.opener)window.opener.postMessage({{"type":"slack_oauth_success","team_id":{json.dumps(team_id)},"team_name":{json.dumps(team_name or "")}}},"*");
    window.close();</script><p>Connected.</p></body></html>""")


# ── WORKSPACES ────────────────────────────────────────────────────────────────

@app.get("/workspaces")
@app.get("/api/workspaces")
def list_workspaces(request: Request, response: Response):
    """Returns ONLY workspaces owned by the current session."""
    no_cache(response)
    session_id, sess = get_or_create_session(request, response)
    allowed          = sess.get("team_ids", [])
    workspaces       = []
    for team_id in allowed:
        sec = read_secret(secret_name(team_id))
        if not sec or "_error" in sec or not sec.get("bot_token"):
            continue
        workspaces.append({"team_id": team_id, "team_name": sec.get("team_name")})
    workspaces.sort(key=lambda x: ((x.get("team_name") or "").lower(), x["team_id"].lower()))
    return {"ok": True, "workspaces": workspaces}


@app.delete("/workspaces/{team_id}")
@app.delete("/api/workspaces/{team_id}")
def disconnect_workspace(team_id: str, request: Request, response: Response):
    no_cache(response)
    require_team_access(request, team_id)
    name = secret_name(team_id)
    sec  = read_secret(name)
    if not sec or "_error" in sec:
        return {"ok": False, "message": "Secret not found"}
    revoke_data = None
    if sec.get("bot_token"):
        try:
            revoke_data = requests.post("https://slack.com/api/auth.revoke",
                headers={"Authorization": f"Bearer {sec['bot_token']}"},
                data={"test": "false"}, timeout=20).json()
        except Exception as e:
            revoke_data = {"ok": False, "error": str(e)}
    try:
        secrets_client.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
    except Exception as e:
        return {"ok": False, "detail": str(e), "revoked": revoke_data}
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_val:
        unbind_team_from_session(cookie_val, team_id)
    return {"ok": True, "team_id": team_id, "revoked": revoke_data}


# ── TOKEN STATUS ──────────────────────────────────────────────────────────────

@app.get("/token/status")
@app.get("/api/token/status")
def token_status(team_id: str, request: Request, response: Response):
    no_cache(response)
    require_team_access(request, team_id)
    s     = read_secret(secret_name(team_id))
    if not s or "_error" in s:
        return {"ok": True, "team_id": team_id, "has_token": False}
    token = s.get("bot_token", "")
    return {"ok": True, "team_id": team_id, "team_name": s.get("team_name"),
            "has_token": bool(token), "bot_token_masked": mask_token(token), "scope": s.get("scope")}


# ── CHANNELS ──────────────────────────────────────────────────────────────────

@app.get("/channels")
@app.get("/api/channels")
def list_channels(team_id: str, request: Request, response: Response):
    no_cache(response)
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or "_error" in sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    r    = requests.get("https://slack.com/api/conversations.list",
                        headers={"Authorization": f"Bearer {sec['bot_token']}"},
                        params={"limit": 200, "types": "public_channel,private_channel", "exclude_archived": "true"},
                        timeout=20)
    data = r.json()
    if not data.get("ok"):
        return {"ok": False, "slack_error": data}
    channels = sorted([{"id": c["id"], "name": c["name"]} for c in data.get("channels", [])],
                      key=lambda c: c["name"].lower())
    return {"ok": True, "channels": channels}


# ── FETCH MESSAGES ────────────────────────────────────────────────────────────

@app.get("/fetch-messages")
@app.get("/api/fetch-messages")
def fetch_messages(team_id: str, channel_id: str, request: Request, response: Response):
    no_cache(response)
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    r    = requests.get("https://slack.com/api/conversations.history",
                        headers={"Authorization": f"Bearer {sec['bot_token']}"},
                        params={"channel": channel_id, "limit": 50}, timeout=20)
    data = r.json()
    if not data.get("ok"):
        return {"ok": False, "slack_error": data}
    return {"ok": True, "messages": [{"ts": m.get("ts"), "text": m.get("text"), "user": m.get("user")}
                                      for m in data.get("messages", [])]}


# ── JOIN CHANNEL ──────────────────────────────────────────────────────────────

@app.post("/join-channel")
@app.post("/api/join-channel")
def join_channel(team_id: str, channel_id: str, request: Request):
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    data = requests.post("https://slack.com/api/conversations.join",
                         headers={"Authorization": f"Bearer {sec['bot_token']}"},
                         data={"channel": channel_id}, timeout=20).json()
    if not data.get("ok") and data.get("error") != "already_in_channel":
        return {"ok": False, "slack_error": data}
    return {"ok": True, "joined": True, "channel_id": channel_id}


# ── JOIN ALL PUBLIC ───────────────────────────────────────────────────────────

@app.post("/join-all-public")
@app.post("/api/join-all-public")
def join_all_public(team_id: str, request: Request):
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    joined, failed, cursor = [], [], None
    while True:
        params = {"limit": 200, "types": "public_channel", "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        lst = requests.get("https://slack.com/api/conversations.list",
                           headers={"Authorization": f"Bearer {sec['bot_token']}"},
                           params=params, timeout=20).json()
        if not lst.get("ok"):
            return {"ok": False, "slack_error": lst}
        for ch in lst.get("channels", []):
            ch_id = ch["id"]
            j = requests.post("https://slack.com/api/conversations.join",
                              headers={"Authorization": f"Bearer {sec['bot_token']}"},
                              data={"channel": ch_id}, timeout=20).json()
            if j.get("ok") or j.get("error") == "already_in_channel":
                joined.append(ch_id)
            else:
                failed.append({"channel": ch_id, "error": j.get("error")})
        cursor = (lst.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return {"ok": True, "joined_count": len(joined), "failed_count": len(failed), "failed": failed}


# ── BACKFILL ──────────────────────────────────────────────────────────────────

@app.post("/backfill-channel")
@app.post("/api/backfill-channel")
def backfill_channel(team_id: str, channel_id: str, request: Request, limit: int = 200, cursor: str | None = None):
    require_ddb()
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    params = {"channel": channel_id, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    data = requests.get("https://slack.com/api/conversations.history",
                        headers={"Authorization": f"Bearer {sec['bot_token']}"},
                        params=params, timeout=20).json()
    if not data.get("ok"):
        return {"ok": False, "slack_error": data}
    msgs = data.get("messages", []) or []
    pk   = f"{team_id}#{channel_id}"
    stored = 0
    for m in msgs:
        ts_msg = str(m.get("ts"))
        if not ts_msg:
            continue
        uid      = m.get("user")
        username = resolve_username_for_message(team_id, uid, sec["bot_token"]) if uid else ""
        item = {
            "pk": pk, "sk": ts_msg,
            "team_id": team_id, "channel_id": channel_id, "ts": ts_msg,
            "user_id": uid, "username": username, "text": m.get("text", ""),
            "thread_ts": m.get("thread_ts"), "reply_count": m.get("reply_count", 0),
            "subtype": m.get("subtype"), "type": m.get("type"),
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        }
        try:
            ddb_table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
            stored += 1
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    next_cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
    return {"ok": True, "channel_id": channel_id, "fetched": len(msgs),
            "stored_new": stored, "next_cursor": next_cursor, "has_more": bool(next_cursor)}


# ── BACKFILL ALL PUBLIC ───────────────────────────────────────────────────────

@app.post("/backfill-all-public")
@app.post("/api/backfill-all-public")
def backfill_all_public(team_id: str, request: Request):
    require_ddb()
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    all_channels, cursor = [], None
    while True:
        params = {"limit": 200, "types": "public_channel", "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        lst = requests.get("https://slack.com/api/conversations.list",
                           headers={"Authorization": f"Bearer {sec['bot_token']}"},
                           params=params, timeout=20).json()
        if not lst.get("ok"):
            return {"ok": False, "slack_error": lst}
        for ch in lst.get("channels", []):
            if ch.get("is_member"):
                all_channels.append(ch["id"])
        cursor = (lst.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    total_stored, results = 0, []
    for ch_id in all_channels:
        bf_cursor, stored, ok = "", 0, True
        while True:
            bf = backfill_channel(team_id=team_id, channel_id=ch_id, request=request,
                                   limit=200, cursor=bf_cursor if bf_cursor else None)
            if not bf.get("ok"):
                ok = False
                break
            stored += bf.get("stored_new", 0)
            if not bf.get("has_more"):
                break
            bf_cursor = bf.get("next_cursor", "")
        results.append({"channel": ch_id, "ok": ok, "stored": stored})
        if ok:
            total_stored += stored
    return {"ok": True, "total_stored": total_stored, "results": results}


# ── BACKFILL ALL PRIVATE ──────────────────────────────────────────────────────

@app.post("/backfill-all-private")
@app.post("/api/backfill-all-private")
def backfill_all_private(team_id: str, request: Request):
    require_ddb()
    require_team_access(request, team_id)
    sec = read_secret(secret_name(team_id))
    if not sec or not sec.get("bot_token"):
        return {"ok": False, "message": "bot_token missing"}
    all_channels, cursor = [], None
    while True:
        params = {"limit": 200, "types": "private_channel", "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        lst = requests.get("https://slack.com/api/conversations.list",
                           headers={"Authorization": f"Bearer {sec['bot_token']}"},
                           params=params, timeout=20).json()
        if not lst.get("ok"):
            return {"ok": False, "slack_error": lst}
        for ch in lst.get("channels", []):
            if ch.get("is_member"):
                all_channels.append(ch["id"])
        cursor = (lst.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    total_stored, results = 0, []
    for ch_id in all_channels:
        bf_cursor, stored = "", 0
        ok = True
        while True:
            bf = backfill_channel(team_id=team_id, channel_id=ch_id, request=request,
                                   limit=200, cursor=bf_cursor if bf_cursor else None)
            if not bf.get("ok"):
                ok = False
                break
            stored += bf.get("stored_new", 0)
            if not bf.get("has_more"):
                break
            bf_cursor = bf.get("next_cursor", "")
        results.append({"channel": ch_id, "ok": ok, "stored": stored})
        if ok:
            total_stored += stored
    return {"ok": True, "total_stored": total_stored, "results": results}


# ── SLACK EVENTS WEBHOOK ──────────────────────────────────────────────────────
# Handles two responsibilities in one endpoint:
#   1. Stores every incoming channel message into DynamoDB (existing behaviour)
#   2. If the message is a DM to the bot or an @mention, generates an AI reply
#      with channel-aware routing, summarize support, and conversation history

BOT_CONVO_MAX_HISTORY = 10   # max prior thread turns to send to Groq
BOT_RATE_LIMIT        = 20   # max bot AI replies per user per minute
_bot_rate_store: dict = defaultdict(list)

# ── COMMAND PARSER ─────────────────────────────────────────────────────────────
# Users talk to the bot in DM or by @mentioning it in a channel:
#
#   ask #general what did John say about the deadline?
#   search #general for standup updates
#   summarize #general
#   summarize #general last 50
#   ask all what was discussed about the budget?   ← all channels the bot is in
#
# The #channel or "all" is REQUIRED. Without it the bot asks the user to specify.
# "all" searches across every channel the bot has stored messages from.

_CMD_RE = re.compile(
    r"^(?P<cmd>search|ask|summarize|summary|sum)\s+"
    r"(?:(?:#(?P<channel>[a-z0-9_\-]+)|(?P<all>all))\s*)?"
    r"(?:for\s+)?(?P<query>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_SUMMARIZE_LIMIT_RE = re.compile(r"\blast\s+(\d+)\b", re.IGNORECASE)
_SUMMARIZE_CMDS = {"summarize", "summary", "sum"}


def _check_bot_rate_limit(user_id: str) -> bool:
    """True if the user is within the bot rate limit, False if exceeded."""
    now = time.time()
    _bot_rate_store[user_id] = [t for t in _bot_rate_store[user_id] if now - t < RATE_WINDOW_SECS]
    if len(_bot_rate_store[user_id]) >= BOT_RATE_LIMIT:
        return False
    _bot_rate_store[user_id].append(now)
    return True


def _post_slack_message(bot_token: str, channel: str, text: str, thread_ts: Optional[str] = None) -> None:
    """Post a message back to a Slack channel, optionally in a thread."""
    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        data = r.json()
        if not data.get("ok"):
            logger.warning("chat.postMessage failed", extra={"error": data.get("error"), "channel": channel})
    except Exception as e:
        logger.error("Slack post failed", extra={"error": str(e)})


def _get_thread_history(bot_token: str, channel: str, thread_ts: str) -> list[dict]:
    """
    Fetch prior thread turns from Slack conversations.replies.
    Returns list of {role, content} dicts ready for Groq multi-turn.
    """
    try:
        r = requests.get(
            "https://slack.com/api/conversations.replies",
            headers={"Authorization": f"Bearer {bot_token}"},
            params={"channel": channel, "ts": thread_ts, "limit": BOT_CONVO_MAX_HISTORY * 2},
            timeout=10,
        )
        data = r.json()
        if not data.get("ok"):
            return []
        msgs = data.get("messages", [])
        history = []
        for m in msgs[:-1]:   # exclude the current (last) message
            text = re.sub(r"<@[A-Z0-9]+>\s*", "", (m.get("text") or "")).strip()
            if not text:
                continue
            role = "assistant" if m.get("bot_id") else "user"
            history.append({"role": role, "content": text})
        return history[-BOT_CONVO_MAX_HISTORY:]
    except Exception as e:
        logger.warning("Thread history fetch failed", extra={"error": str(e)})
        return []


def _resolve_channel_id_by_name(bot_token: str, channel_name: str) -> Optional[str]:
    """
    Look up a Slack channel ID from its name (without #).
    Uses conversations.list — results are cached in-process.
    """
    try:
        cursor = None
        while True:
            params: dict = {"limit": 200, "types": "public_channel,private_channel", "exclude_archived": "true"}
            if cursor:
                params["cursor"] = cursor
            r = requests.get(
                "https://slack.com/api/conversations.list",
                headers={"Authorization": f"Bearer {bot_token}"},
                params=params, timeout=10,
            )
            data = r.json()
            if not data.get("ok"):
                break
            for ch in data.get("channels", []):
                if ch.get("name", "").lower() == channel_name.lower():
                    return ch["id"]
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
    except Exception as e:
        logger.warning("Channel name lookup failed", extra={"error": str(e)})
    return None


def _get_all_bot_channel_ids(team_id: str) -> list[str]:
    """
    Return all distinct channel IDs the bot has stored messages for in DynamoDB.
    Used when user asks to search "all" channels.
    """
    if ddb_table is None:
        return []
    try:
        # Scan for distinct pk values matching this team
        resp = ddb_table.scan(
            FilterExpression=Attr("team_id").eq(team_id),
            ProjectionExpression="pk",
        )
        pks = {item["pk"] for item in resp.get("Items", [])}
        # pk format is "team_id#channel_id" — extract channel_id part
        prefix = f"{team_id}#"
        ids = [pk[len(prefix):] for pk in pks if pk.startswith(prefix) and not pk.endswith("__users__")]
        return ids[:MAX_CHANNEL_IDS]
    except Exception as e:
        logger.warning("Failed to list all channel IDs", extra={"error": str(e)})
        return []


def _handle_bot_message(
    team_id: str, dm_channel: str, user_text: str,
    thread_ts: str, bot_token: str, user_id: str,
) -> None:
    """
    Full bot logic:
      1. Parse command + channel from user_text
      2. Resolve channel name → channel ID
      3. Retrieve messages (positional / semantic / summarize)
      4. Call Groq with conversation history
      5. Post reply back to Slack
    """
    _check_prompt_injection(user_text)

    if not _check_bot_rate_limit(user_id):
        _post_slack_message(bot_token, dm_channel,
            "⚠️ You\'re sending messages too fast — please wait a moment.", thread_ts)
        return

    m = _CMD_RE.match(user_text.strip())
    if not m:
        # No recognised command — send help
        _post_slack_message(bot_token, dm_channel, (
            "👋 Here\'s how to use me:\n\n"
            "• `ask #channel-name <question>` — ask AI about a channel\n"
            "• `search #channel-name <keywords>` — keyword search\n"
            "• `summarize #channel-name` — summarize recent messages\n"
            "• `summarize #channel-name last 50` — summarize last N messages\n"
            "• `ask all <question>` — search across ALL channels\n\n"
            "_Example:_ `ask #general what did John say about the deadline?`"
        ), thread_ts)
        return

    cmd        = (m.group("cmd") or "ask").lower()
    ch_name    = m.group("channel")   # e.g. "general"  (no #)
    search_all = bool(m.group("all"))
    query      = (m.group("query") or "").strip()

    is_summarize = cmd in _SUMMARIZE_CMDS

    # ── Resolve which channel(s) to search ───────────────────────────────────
    if search_all:
        channel_ids = _get_all_bot_channel_ids(team_id)
        if not channel_ids:
            _post_slack_message(bot_token, dm_channel,
                "⚠️ I don\'t have any messages stored yet. Try backfilling channels first.", thread_ts)
            return
        search_channel_id = None   # will use multi-channel retrieval
    elif ch_name:
        search_channel_id = _resolve_channel_id_by_name(bot_token, ch_name)
        if not search_channel_id:
            _post_slack_message(bot_token, dm_channel,
                f"⚠️ I couldn\'t find a channel named *#{ch_name}*. Check the spelling and make sure I\'m in that channel.", thread_ts)
            return
        channel_ids = [search_channel_id]
    else:
        # No channel specified — ask user
        _post_slack_message(bot_token, dm_channel, (
            "Please specify a channel. Examples:\n"
            "• `ask #general <question>`\n"
            "• `summarize #standup`\n"
            "• `ask all <question>`"
        ), thread_ts)
        return

    # ── Summarize mode ────────────────────────────────────────────────────────
    if is_summarize:
        limit_match = _SUMMARIZE_LIMIT_RE.search(query)
        summarize_limit = int(limit_match.group(1)) if limit_match else 50
        summarize_limit = min(summarize_limit, 200)

        try:
            if search_all or len(channel_ids) > 1:
                messages = retrieve_messages_multi(
                    team_id, channel_ids, None, None, None, None,
                    limit=summarize_limit, top_k=summarize_limit,
                    bot_token=bot_token,
                )
            else:
                resp = ddb_table.query(
                    KeyConditionExpression=Key("pk").eq(f"{team_id}#{channel_ids[0]}"),
                    ScanIndexForward=False,
                    Limit=summarize_limit,
                )
                raw_items = [i for i in resp.get("Items", []) if not re.search(
                    r"<@\w+> has (joined|left)", (i.get("text") or "").lower()
                )]
                messages = _format_messages(raw_items)
        except Exception as e:
            logger.error("Summarize retrieval failed", extra={"error": str(e)})
            _post_slack_message(bot_token, dm_channel,
                "⚠️ I had trouble fetching messages for summarization.", thread_ts)
            return

        if not messages:
            ch_label = "all channels" if search_all else f"#{ ch_name}"
            _post_slack_message(bot_token, dm_channel,
                f"No messages found in {ch_label} to summarize.", thread_ts)
            return

        context, _ = _build_context(messages, channel_prefix=search_all)
        ch_label   = "all channels" if search_all else f"#{ch_name}"
        system_prompt = (
            "You are a helpful Slack assistant. Summarize the provided Slack messages.\n"
            "Rules:\n"
            "1. Write a concise summary — 3 to 8 bullet points.\n"
            "2. Highlight key decisions, action items, and important topics.\n"
            "3. Name the people involved where relevant.\n"
            "4. Use Slack markdown: *bold* for names/topics, • for bullets.\n"
            "5. Do NOT add citations or message numbers — just a clean summary.\n"
            "6. Keep the total response under 400 words.\n"
        )
        user_prompt = (
            f"Summarize the following {len(messages)} Slack messages from {ch_label}:\n\n"
            f"{context}"
        )
        answer = _groq_complete(user_prompt, MAX_TOKENS_BOT, system=system_prompt)
        _post_slack_message(bot_token, dm_channel,
            f"*Summary of {ch_label}* (last {len(messages)} messages):\n\n{answer}", thread_ts)
        logger.info("Bot summarize sent", extra={
            "team_id": team_id, "channel_ids": channel_ids, "msg_count": len(messages),
        })
        return

    # ── Ask / Search mode ─────────────────────────────────────────────────────
    if not query:
        _post_slack_message(bot_token, dm_channel,
            f"What do you want to {'search for' if cmd == 'search' else 'ask'} in "
            f"{'all channels' if search_all else f'#{ch_name}'}?", thread_ts)
        return

    active_username = extract_username_from_question(query)

    # Positional query (first/last message)
    if _is_positional_query(query) and not search_all and len(channel_ids) == 1:
        position = "first" if _FIRST_RE.search(query) else "last"
        try:
            messages = retrieve_first_or_last(team_id, channel_ids[0], position=position, top_k=3)
        except RuntimeError as e:
            logger.error("Bot positional retrieval failed", extra={"error": str(e)})
            _post_slack_message(bot_token, dm_channel,
                "⚠️ I had trouble fetching messages. Please try again.", thread_ts)
            return
    elif search_all or len(channel_ids) > 1:
        try:
            messages = retrieve_messages_multi(
                team_id, channel_ids, query, None, None, None, 200, 10,
                username=active_username, bot_token=bot_token,
            )
        except Exception as e:
            logger.error("Bot multi retrieval failed", extra={"error": str(e)})
            _post_slack_message(bot_token, dm_channel,
                "⚠️ I had trouble searching messages. Please try again.", thread_ts)
            return
    else:
        try:
            messages = retrieve_messages(
                team_id, channel_ids[0], query, None, None, None, 200, 10,
                username=active_username, bot_token=bot_token,
            )
        except RuntimeError as e:
            logger.error("Bot retrieval failed", extra={"error": str(e)})
            _post_slack_message(bot_token, dm_channel,
                "⚠️ I had trouble fetching messages. Please try again.", thread_ts)
            return

    if not messages:
        ch_label = "all channels" if search_all else f"#{ch_name}"
        note = (f"I couldn\'t find any messages from *{active_username}* in {ch_label}."
                if active_username else f"I couldn\'t find relevant messages for that in {ch_label}.")
        _post_slack_message(bot_token, dm_channel, note, thread_ts)
        return

    context, _ = _build_context(messages, channel_prefix=search_all)
    ch_label   = "all channels" if search_all else f"#{ch_name}"
    system_prompt = (
        "You are a helpful Slack assistant. Answer questions ONLY from the Slack messages provided.\n"
        "Rules:\n"
        "1. Read each message IN FULL.\n"
        "2. If the answer is not in the messages say: I couldn\'t find that in the available messages.\n"
        "3. Never use outside knowledge or guess.\n"
        "4. Cite message numbers like [1] or [2] for every claim.\n"
        "5. Be concise — replying inside Slack. Short, scannable answers.\n"
        "6. CRITICAL: sender name is between | and : in each line. When asked WHO, name them.\n"
        "7. Use Slack markdown: *bold*, _italic_, `code`, • for bullets.\n"
    )

    convo_history = _get_thread_history(bot_token, dm_channel, thread_ts)
    augmented_q   = _augment_question_with_senders(query, messages)
    user_prompt   = f"SLACK MESSAGES from {ch_label}:\n{context}\n\nQUESTION: {augmented_q}"

    answer = _groq_complete(
        user_prompt, MAX_TOKENS_BOT,
        system=system_prompt,
        conversation_history=convo_history,
    )
    _post_slack_message(bot_token, dm_channel, answer, thread_ts)
    logger.info("Bot reply sent", extra={
        "team_id": team_id, "channel_ids": channel_ids,
        "user_id": user_id, "history_turns": len(convo_history), "retrieved": len(messages),
    })


@app.post("/slack/events")
@app.post("/api/slack/events")
async def slack_events(request: Request):
    require_ddb()
    raw_body = await request.body()

    if len(raw_body) > MAX_BODY_BYTES:
        logger.warning("Slack event payload too large", extra={"size_bytes": len(raw_body)})
        return JSONResponse({"ok": False, "error": "payload_too_large"}, status_code=413)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.warning("Slack event: invalid JSON body")
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    # ── URL verification (one-time Slack setup handshake) ────────────────────
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})

    # ── Signature verification ───────────────────────────────────────────────
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(SLACK_SIGNING_SECRET, timestamp, raw_body, signature):
        logger.warning("Slack event: invalid signature")
        return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=401)

    if payload.get("type") != "event_callback":
        return JSONResponse({"ok": True})

    event      = payload.get("event") or {}
    event_type = event.get("type")

    # Only handle message and app_mention events
    if event_type not in ("message", "app_mention"):
        return JSONResponse({"ok": True})
    # Ignore bot's own messages, edits, deletes
    if event.get("bot_id") or event.get("subtype") in {"message_changed", "message_deleted", "bot_message"}:
        return JSONResponse({"ok": True})

    team_id    = payload.get("team_id")
    channel_id = event.get("channel")
    ts_msg     = event.get("ts")
    if not team_id or not channel_id or not ts_msg:
        return JSONResponse({"ok": True})

    uid = event.get("user")

    # ── Get bot token for this team ──────────────────────────────────────────
    sec = read_secret(secret_name(team_id))
    bot_token: Optional[str] = None
    if sec and not sec.get("_error"):
        bot_token = sec.get("bot_token")

    # ── Resolve display name ─────────────────────────────────────────────────
    event_username = ""
    if uid and bot_token:
        try:
            event_username = resolve_username_for_message(team_id, uid, bot_token)
        except Exception:
            pass

    # ── Store message in DynamoDB (always, for all channel messages) ─────────
    item = {
        "pk": f"{team_id}#{channel_id}", "sk": str(ts_msg),
        "team_id": team_id, "channel_id": channel_id, "ts": str(ts_msg),
        "user_id": uid, "username": event_username, "text": event.get("text", ""),
        "thread_ts": event.get("thread_ts"), "subtype": event.get("subtype"),
        "type": event.get("type"), "fetched_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        ddb_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )
        logger.info("Slack event stored", extra={
            "team_id": team_id, "channel_id": channel_id, "ts": ts_msg, "user_id": uid,
        })
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.error("DynamoDB put failed", extra={"team_id": team_id, "ts": ts_msg, "error": str(e)})
            raise

    # ── IMPORTANT: return 200 to Slack immediately BEFORE bot processing ─────
    # Slack requires a response within 3 seconds or it retries.
    # We return here and the bot reply happens synchronously but after the
    # DynamoDB write — acceptable because Lambda keeps running until the
    # function returns. The actual return is at the end of the function.

    # ── Bot reply: only on DMs or @mentions ──────────────────────────────────
    channel_type = event.get("channel_type", "")
    is_dm        = channel_type == "im"
    is_mention   = event_type == "app_mention"

    if bot_token and (is_dm or is_mention) and uid:
        user_text = re.sub(r"<@[A-Z0-9]+>\s*", "", event.get("text", "")).strip()
        if user_text:
            thread_ts = event.get("thread_ts") or ts_msg
            try:
                _handle_bot_message(
                    team_id=team_id,
                    dm_channel=channel_id,
                    user_text=user_text,
                    thread_ts=thread_ts,
                    bot_token=bot_token,
                    user_id=uid,
                )
            except Exception as e:
                logger.error("Bot handler error", extra={"error": str(e), "team_id": team_id})
                _post_slack_message(bot_token, channel_id,
                    "⚠️ Something went wrong. Please try again.", thread_ts)

    return JSONResponse({"ok": True})



handler = Mangum(app)