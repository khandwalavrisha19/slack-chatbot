import uuid
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException, Request, Response

from app.constants import SESSION_COOKIE_NAME, SESSION_TTL_HOURS, IS_PROD
from app.logger import logger
# ── DB HELPERS ───────────────────────────────────────────────────────────────

import json
from datetime import datetime, timedelta

def create_session() -> str:
    from app.db import get_conn
    session_id = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sessions (session_id, team_ids, created_at, expires_at) VALUES (%s, %s, %s, %s)",
                    (session_id, "[]", datetime.utcnow().isoformat() + "Z", expires_at)
                )
        logger.info(f"[session] created {session_id}")
    except Exception as e:
        logger.error(f"[session] failed to create: {e}")
    return session_id


def get_session(session_id: str) -> Optional[dict]:
    if not session_id:
        return None
    try:
        from app.db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
                item = cur.fetchone()
        
        if not item:
            return None
        
        # Postgres returns datetime for expires_at, so we compare with current datetime
        if item.get("expires_at") and item["expires_at"] < datetime.utcnow():
            return None
            
        # Parse team_ids
        try:
            item["team_ids"] = json.loads(item.get("team_ids") or "[]")
        except:
            item["team_ids"] = []
            
        return dict(item)
    except Exception as e:
        logger.warning(f"[session] get error: {e}")
        return None


def bind_team_to_session(session_id: str, team_id: str) -> None:
    sess = get_session(session_id)
    if not sess:
        return
    current = sess.get("team_ids", [])
    if team_id not in current:
        current.append(team_id)
        
    try:
        from app.db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sessions SET team_ids = %s WHERE session_id = %s",
                    (json.dumps(current), session_id)
                )
        logger.info(f"[session] bound team {team_id} to session {session_id}")
    except Exception as e:
        logger.error(f"[session] bind error: {e}")


def unbind_team_from_session(session_id: str, team_id: str) -> None:
    if not session_id:
        return
    sess = get_session(session_id)
    if not sess:
        return
    updated = [t for t in sess.get("team_ids", []) if t != team_id]
    try:
        from app.db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sessions SET team_ids = %s WHERE session_id = %s",
                    (json.dumps(updated), session_id)
                )
    except Exception as e:
        logger.warning(f"[session] unbind error: {e}")


# ── COOKIE HELPERS ────────────────────────────────────────────────────────────

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


def get_or_create_session(request: Request, response: Response) -> tuple[str, dict]:
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    sess       = get_session(cookie_val) if cookie_val else None
    if not sess:
        session_id = create_session()
        sess       = get_session(session_id) or {}
        _set_session_cookie(response, session_id)
        return session_id, sess
    return cookie_val, sess


# ── AUTH GUARDS ───────────────────────────────────────────────────────────────

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