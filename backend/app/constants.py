import os
from pathlib import Path

# ── SLACK BASE URLS ───────────────────────────────────────────────────────────
# All Web API calls  → https://slack.com/api/<method>
# OAuth authorize    → https://slack.com/oauth/v2/authorize  (different path root)
SLACK_API_BASE   = "https://slack.com/api"
SLACK_OAUTH_BASE = "https://slack.com/oauth/v2"

# ── REQUEST SIZE LIMITS ───────────────────────────────────────────────────────
MAX_BODY_BYTES   = 64 * 1024   # 64 KB hard limit for all POST bodies
MAX_QUESTION_LEN = 1_000       # chars
MAX_CHANNEL_IDS  = 20          # max channels in multi-chat/search

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
CORS_ORIGINS         = os.getenv("CORS_ORIGINS", "*")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "").strip()
DB_HOST              = os.getenv("DB_HOST", "localhost").strip()
DB_USER              = os.getenv("DB_USER", "postgres").strip()
DB_PASSWORD          = os.getenv("DB_PASSWORD", "postgres").strip()
DB_NAME              = os.getenv("DB_NAME", "slackbotdb").strip()
DB_PORT              = int(os.getenv("DB_PORT", "5432").strip())
BEDROCK_MODEL_ID     = os.getenv("BEDROCK_MODEL_ID", "meta.llama3-1-70b-instruct-v1:0").strip()
UI_BASE_URL          = os.getenv("UI_BASE_URL", "").rstrip("/")
SESSION_COOKIE_NAME  = "sb_session"
SESSION_TTL_HOURS    = 72
IS_PROD              = os.getenv("ENV", "dev").strip().lower() == "prod"

# ── FRONTEND ──────────────────────────────────────────────────────────────────
_frontend_default = Path(__file__).with_name("index.html")
FRONTEND_PATH     = Path(os.getenv("FRONTEND_PATH", str(_frontend_default)))

# ── GROQ TIMEOUTS & TOKEN LIMITS ─────────────────────────────────────────────
CONTEXT_MAX_CHARS    = 8_000
MAX_TOKENS_SINGLE    = 768
MAX_TOKENS_MULTI     = 900
MAX_TOKENS_BOT       = 600
RATE_WINDOW_SECS     = 60

# ── CORS ORIGINS (parsed) ─────────────────────────────────────────────────────
PARSED_CORS_ORIGINS = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()] or ["*"]