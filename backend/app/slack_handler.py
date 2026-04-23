import re
import time
import json
import requests
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError
from mangum import Mangum
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.logger import logger
from app.constants import (
    SLACK_SIGNING_SECRET, MAX_BODY_BYTES, RATE_WINDOW_SECS,
    MAX_CHANNEL_IDS, MAX_TOKENS_BOT, MAX_TOKENS_SINGLE, MAX_TOKENS_MULTI
)
from app.utils import (
    ddb_table, require_ddb, read_secret, secret_name,
    verify_slack_signature, resolve_username_for_message,
    resolve_user_id, extract_username_from_question
)
from app.retrieval import (
    retrieve_messages, retrieve_messages_multi,
    _build_context, _augment_question_with_senders,
    _format_messages, _is_recency_query
)
from app.bedrock_client import _bedrock_complete

# ── SLACK EVENTS WEBHOOK ──────────────────────────────────────────────────────

_bot_rate_store: dict = defaultdict(list)
BOT_CONVO_MAX_HISTORY = 10
BOT_RATE_LIMIT        = 20

# ── Slack-encoded channel link normalizer ────────────────────────────────────
_SLACK_CHANNEL_LINK_RE = re.compile(r"<#([A-Z0-9]+)(?:\|([a-z0-9_\-]+))?>", re.IGNORECASE)

def _normalize_slack_channel_links(text: str, bot_token: str = "") -> str:
    def _replace(match):
        ch_id   = match.group(1)
        ch_name = match.group(2)
        if ch_name:
            return f"#{ch_name}"
        if bot_token:
            try:
                r = requests.get(
                    "https://slack.com/api/conversations.info",
                    headers={"Authorization": f"Bearer {bot_token}"},
                    params={"channel": ch_id},
                    timeout=5,
                )
                data = r.json()
                if data.get("ok"):
                    resolved_name = data.get("channel", {}).get("name", "")
                    if resolved_name:
                        return f"#{resolved_name}"
            except Exception:
                pass
        return f"#{ch_id}"
    return _SLACK_CHANNEL_LINK_RE.sub(_replace, text)

_CMD_RE = re.compile(
    r"^(?P<cmd>search|ask|summarize|summary|sum)\s+"
    r"(?:#(?P<channel>[a-zA-Z0-9_\-]+)|(?P<all>all))"
    r"(?:\s+(?:for\s+)?(?P<query>.*))?$",
    re.IGNORECASE | re.DOTALL,
)
_SUMMARIZE_LIMIT_RE = re.compile(r"\blast\s+(\d+)\b", re.IGNORECASE)
_SUMMARIZE_CMDS = {"summarize", "summary", "sum"}

def _check_bot_rate_limit(user_id: str) -> bool:
    now = time.time()
    _bot_rate_store[user_id] = [t for t in _bot_rate_store[user_id] if now - t < RATE_WINDOW_SECS]
    if len(_bot_rate_store[user_id]) >= BOT_RATE_LIMIT:
        return False
    _bot_rate_store[user_id].append(now)
    return True

def _post_slack_message(bot_token: str, channel: str, text: str, thread_ts: Optional[str] = None) -> None:
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
        for m in msgs[:-1]:
            text = re.sub(r"<@[A-Z0-9]+>\s*", "", (m.get("text") or "")).strip()
            if not text: continue
            role = "assistant" if m.get("bot_id") else "user"
            history.append({"role": role, "content": text})
        return history[-BOT_CONVO_MAX_HISTORY:]
    except Exception as e:
        logger.warning("Thread history fetch failed", extra={"error": str(e)})
        return []

def _resolve_channel_id_by_name(bot_token: str, channel_name: str) -> Optional[str]:
    try:
        cursor = None
        while True:
            params: dict = {"limit": 200, "types": "public_channel,private_channel", "exclude_archived": "true"}
            if cursor: params["cursor"] = cursor
            r = requests.get(
                "https://slack.com/api/conversations.list",
                headers={"Authorization": f"Bearer {bot_token}"},
                params=params, timeout=10,
            )
            data = r.json()
            if not data.get("ok"): break
            for ch in data.get("channels", []):
                if ch.get("name", "").lower() == channel_name.lower():
                    return ch["id"]
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor: break
    except Exception as e:
        logger.warning("Channel name lookup failed", extra={"error": str(e)})
    return None

def _get_all_bot_channel_ids(team_id: str) -> list[str]:
    if ddb_table is None: return []
    try:
        resp = ddb_table.scan(FilterExpression=Attr("team_id").eq(team_id), ProjectionExpression="pk")
        pks = {item["pk"] for item in resp.get("Items", [])}
        prefix = f"{team_id}#"
        ids = [pk[len(prefix):] for pk in pks
               if pk.startswith(prefix) and not pk.endswith("__users__")
               and not pk[len(prefix):].startswith("D")]
        return ids[:MAX_CHANNEL_IDS]
    except Exception as e:
        logger.warning("Failed to list all channel IDs", extra={"error": str(e)})
        return []

def _handle_bot_message(
    team_id: str, dm_channel: str, user_text: str,
    thread_ts: str, bot_token: str, user_id: str,
) -> None:
    if not _check_bot_rate_limit(user_id):
        _post_slack_message(bot_token, dm_channel, "⚠️ You're sending messages too fast.", thread_ts)
        return

    cleaned_text = user_text.strip().replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    normalized_text = _normalize_slack_channel_links(cleaned_text, bot_token)
    
    m = _CMD_RE.match(normalized_text)
    if not m:
        _post_slack_message(bot_token, dm_channel, (
            "👋 Here's how to use me:\n"
            "• `ask #channel-name <question>`\n• `search #channel-name <keywords>`\n"
            "• `summarize #channel-name`\n• `ask all <question>`"
        ), thread_ts)
        return

    cmd        = (m.group("cmd") or "ask").lower()
    ch_name    = m.group("channel")
    search_all = bool(m.group("all"))
    query      = (m.group("query") or "").strip()
    if query.startswith("<") and query.endswith(">"): query = query[1:-1].strip()

    is_summarize = cmd in _SUMMARIZE_CMDS

    if search_all:
        channel_ids = _get_all_bot_channel_ids(team_id)
        if not channel_ids:
            _post_slack_message(bot_token, dm_channel, "⚠️ No messages stored yet.", thread_ts)
            return
    elif ch_name:
        if re.match(r"^[A-Z0-9]{8,15}$", ch_name): search_channel_id = ch_name
        else: search_channel_id = _resolve_channel_id_by_name(bot_token, ch_name)
        if not search_channel_id:
            _post_slack_message(bot_token, dm_channel, f"⚠️ Couldn't find channel *#{ch_name}*.", thread_ts)
            return
        channel_ids = [search_channel_id]
    else:
        _post_slack_message(bot_token, dm_channel, "Please specify a channel.", thread_ts)
        return

    if is_summarize:
        limit_match = _SUMMARIZE_LIMIT_RE.search(query)
        summarize_limit = min(int(limit_match.group(1)) if limit_match else 50, 200)
        messages = retrieve_messages_multi(team_id, channel_ids, None, None, None, None, limit=summarize_limit, top_k=summarize_limit, bot_token=bot_token)
        if not messages:
            _post_slack_message(bot_token, dm_channel, "No messages found to summarize.", thread_ts)
            return
        context, _ = _build_context(messages, channel_prefix=search_all)
        system_prompt = "Summarize the provided Slack messages concisely with bullets. Use Slack markdown."
        answer = _bedrock_complete(f"Summarize:\n{context}", MAX_TOKENS_BOT, system=system_prompt)
        _post_slack_message(bot_token, dm_channel, f"*Summary*:\n\n{answer}", thread_ts)
        return

    active_username = extract_username_from_question(query, team_id=team_id, bot_token=bot_token)

    if cmd == "search":
        messages = retrieve_messages_multi(team_id, channel_ids, query, None, None, None, 200, 20, username=active_username, bot_token=bot_token)
        if not messages:
            _post_slack_message(bot_token, dm_channel, f"No messages matching *{query}* found.", thread_ts)
            return
        lines = [f"🔍 Found *{len(messages)}* results for *{query}*:\n"]
        for i, msg in enumerate(messages[:20], 1):
            who = msg.get("username") or msg.get("user_id") or "unknown"
            snippet = (msg.get("text") or "")[:300]
            lines.append(f"*[{i}]* {who}:\n> {snippet}")
        _post_slack_message(bot_token, dm_channel, "\n".join(lines), thread_ts)
        return

    # ASK mode
    messages = retrieve_messages_multi(team_id, channel_ids, query, None, None, None, 200, 10, username=active_username, bot_token=bot_token)
    if not messages:
        _post_slack_message(bot_token, dm_channel, "I couldn't find relevant messages.", thread_ts)
        return

    context, _ = _build_context(messages, channel_prefix=search_all)
    system_prompt = "You are a helpful Slack assistant. Answer ONLY from provided messages. Cite like [1]."
    convo_history = _get_thread_history(bot_token, dm_channel, thread_ts)
    augmented_q   = _augment_question_with_senders(query, messages)
    answer = _bedrock_complete(f"CONTEXT:\n{context}\n\nQUESTION: {augmented_q}", MAX_TOKENS_BOT, system=system_prompt, conversation_history=convo_history)
    _post_slack_message(bot_token, dm_channel, answer, thread_ts)

async def handle_slack_event(payload: dict, raw_body: bytes, timestamp: str, signature: str):
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})

    if not verify_slack_signature(SLACK_SIGNING_SECRET, timestamp, raw_body, signature):
        return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=401)

    if payload.get("type") != "event_callback": return JSONResponse({"ok": True})

    event = payload.get("event") or {}
    if event.get("type") not in ("message", "app_mention") or event.get("bot_id"):
        return JSONResponse({"ok": True})

    team_id    = payload.get("team_id")
    channel_id = event.get("channel")
    ts_msg     = event.get("ts")
    uid        = event.get("user")
    if not team_id or not channel_id or not ts_msg or not uid: return JSONResponse({"ok": True})

    sec = read_secret(secret_name(team_id))
    bot_token = (sec or {}).get("bot_token") if sec and not sec.get("_error") else None

    # Store message if not DM
    if event.get("channel_type") != "im":
        username = resolve_username_for_message(team_id, uid, bot_token) if bot_token else ""
        item = {
            "pk": f"{team_id}#{channel_id}", "sk": str(ts_msg),
            "team_id": team_id, "channel_id": channel_id, "ts": str(ts_msg),
            "user_id": uid, "username": username, "text": event.get("text", ""),
            "thread_ts": event.get("thread_ts"), "type": event.get("type"),
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        }
        try: ddb_table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException": raise

    # Handle bot reply
    is_mention = event.get("type") == "app_mention"
    is_dm      = event.get("channel_type") == "im"
    if bot_token and (is_dm or is_mention):
        raw_text = event.get("text", "")
        bot_user_id = (sec or {}).get("bot_user_id", "")
        user_text = re.sub(rf"<@{re.escape(bot_user_id)}>\s*", "", raw_text).strip() if bot_user_id else re.sub(r"^<@[A-Z0-9]+>\s*", "", raw_text).strip()
        if user_text:
            try: _handle_bot_message(team_id, channel_id, user_text, event.get("thread_ts") or ts_msg, bot_token, uid)
            except Exception as e:
                logger.error("Bot handler error", extra={"error": str(e)})
                _post_slack_message(bot_token, channel_id, "⚠️ Something went wrong.", event.get("thread_ts"))

    return JSONResponse({"ok": True})
