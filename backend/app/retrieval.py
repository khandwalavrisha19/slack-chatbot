import re
from typing import Optional

from boto3.dynamodb.conditions import Key, Attr

from app.utils import ddb_table, _date_to_sk, _ts_human, require_ddb, resolve_user_id
from app.constants import CONTEXT_MAX_CHARS
from app.logger import logger


# ── RECENCY / KEYWORD HELPERS ─────────────────────────────────────────────────

_RECENCY_WORDS = frozenset([
    # temporal / recency
    "last", "latest", "recent", "newest", "today", "yesterday",
    "just", "now", "current", "recently", "new",
    "next", "week", "soon", "tomorrow", "upcoming", "future",
    # question words
    "what", "who", "whose", "whom", "where", "when", "why", "how",
    # dashboard meta-words and common query verbs (ignore for keyword matching)
    "channel", "channels", "message", "messages", "bot", "slack",
    "sent", "was", "has", "had", "been", "from", "and", "the", "for",
    "about", "said", "say", "says", "did", "does", "with", "its", "this", "that", "tell",
    "summarize", "summary", "chat", "conversation", "please", "give", "me", "of",
    "a", "all", "can", "you",
])

_CHRONO_WORDS = frozenset([
    "first", "oldest", "start", "beginning", "earliest", "origin",
])


def _is_recency_query(q: str) -> bool:
    if not q:
        return False
    words = set(re.findall(r"\w+", q.lower()))
    return bool(words & _RECENCY_WORDS)


def _is_chrono_query(q: str) -> bool:
    if not q:
        return False
    words = set(re.findall(r"\w+", q.lower()))
    return bool(words & _CHRONO_WORDS)


def _content_keywords(q: str) -> list[str]:
    if not q:
        return []
    return [w for w in re.findall(r"\w+", q.lower())
            if w not in _RECENCY_WORDS and len(w) > 2]


# ── SCORING ───────────────────────────────────────────────────────────────────

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

    if result:
        if _is_chrono_query(q):
            result.sort(key=lambda m: m.get("sk") or m.get("ts") or "")
        elif _is_recency_query(q):
            result.sort(key=lambda m: m.get("sk") or m.get("ts") or "", reverse=True)

    return result


# ── FORMATTING ────────────────────────────────────────────────────────────────

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


# ── RETRIEVAL ─────────────────────────────────────────────────────────────────

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

    # Oldest-first for chrono queries, newest-first otherwise
    scan_forward = _is_chrono_query(q)
    kwargs = {"KeyConditionExpression": key_expr, "Limit": limit, "ScanIndexForward": scan_forward}
    if user_id:
        kwargs["FilterExpression"] = Attr("user_id").eq(user_id)

    try:
        response = ddb_table.query(**kwargs)
        items    = response.get("Items", [])
    except Exception as e:
        raise RuntimeError(f"DynamoDB query failed: {e}")

    # Filter out system join/leave messages
    items = [i for i in items if i.get("subtype") not in ("channel_join", "channel_leave")]
    items = [i for i in items if not re.search(r"<@\w+> has (joined|left)", (i.get("text") or "").lower())]

    # For temporal / recency queries, bypass keyword scoring entirely —
    # the correct chronological context is already fetched from DynamoDB.
    if _is_chrono_query(q) or _is_recency_query(q):
        return _format_messages(items[:top_k])

    content_kws = _content_keywords(q)

    # ── Case 1: Username only (no keyword) → return all their messages newest-first
    if user_id and not content_kws:
        return _format_messages(items[:top_k])

    # ── Case 2: Keyword only (no username) → score all messages by keyword relevance
    # ── Case 3: Keyword + Username → score that user's messages by keyword relevance
    if content_kws:
        scored = _score_messages(items, q)[:top_k]
        scored.sort(key=lambda m: m.get("sk") or m.get("ts") or "", reverse=True)
        return _format_messages(scored)

    # Fallback: no keywords, no user — return top items
    return _format_messages(items[:top_k])


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

    scan_forward = _is_chrono_query(q)
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

        kwargs = {"KeyConditionExpression": key_expr, "Limit": limit, "ScanIndexForward": scan_forward}
        if user_id:
            kwargs["FilterExpression"] = Attr("user_id").eq(user_id)

        try:
            resp  = ddb_table.query(**kwargs)
            items = resp.get("Items", [])
            items = [i for i in items if i.get("subtype") not in ("channel_join", "channel_leave")]
            items = [i for i in items if not re.search(
                r"<@\w+> has (joined|left)", (i.get("text") or "").lower())]
            all_raw.extend(items)
        except Exception as e:
            logger.warning(f"[retrieve_multi] DDB query failed for {channel_id}: {e}")

    if not all_raw:
        return []

    # Temporal / chrono queries — bypass keyword scoring
    if _is_chrono_query(q) or _is_recency_query(q):
        all_raw.sort(
            key=lambda m: m.get("sk") or m.get("ts") or "",
            reverse=not _is_chrono_query(q),
        )
        return _format_messages(all_raw[:top_k])

    content_kws = _content_keywords(q)

    # ── Case 1: Username only (no keyword) → return all their messages newest-first
    if user_id and not content_kws:
        all_raw.sort(key=lambda m: m.get("sk") or m.get("ts") or "", reverse=True)
        return _format_messages(all_raw[:top_k])

    # ── Case 2: Keyword only → score all messages by keyword relevance
    # ── Case 3: Keyword + Username → DynamoDB already filtered by user_id,
    #            now score those user's messages by keyword relevance
    if content_kws:
        return _format_messages(_score_messages(all_raw, q)[:top_k])

    # Fallback: no keywords, no user — return top items newest-first
    all_raw.sort(key=lambda m: m.get("sk") or m.get("ts") or "", reverse=True)
    return _format_messages(all_raw[:top_k])


# ── CONTEXT / PROMPT BUILDERS ─────────────────────────────────────────────────

def _build_context(messages: list[dict], channel_prefix: bool = False) -> tuple[str, int]:
    """
    Build the LLM context string from retrieved messages.
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
    If the question asks WHO, inject the sender names directly into the question
    so the LLM cannot miss them.
    """
    who_words = {"who", "whose", "whom"}
    if not (set(question.lower().split()) & who_words):
        return question

    senders, seen = [], set()
    for m in messages:
        name = (m.get("username") or m.get("user_id") or "").strip()
        if name and name not in seen:
            senders.append(name)
            seen.add(name)

    if not senders:
        return question

    sender_str = ", ".join(senders)
    return f"{question} [NOTE: The message(s) were sent by: {sender_str}. You MUST name them in your answer.]"