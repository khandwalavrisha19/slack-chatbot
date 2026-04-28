import json
import uuid
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError
from fastapi import HTTPException

from app.constants import BEDROCK_MODEL_ID, AWS_REGION
from app.logger import logger

try:
    bedrock_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
except Exception as e:
    logger.error("Failed to initialize Bedrock client", extra={"error": str(e)})
    bedrock_client = None


# ── CLAUDE (Anthropic) FORMAT ─────────────────────────────────────────────────

def _call_claude(prompt: str, max_tokens: int, system: Optional[str], request_id: str) -> tuple[str, int]:
    """Invoke Anthropic Claude models via Bedrock using the Anthropic Messages API format."""
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system

    response = bedrock_client.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(payload),
    )
    body = json.loads(response["body"].read())
    text = body.get("content", [{}])[0].get("text", "").strip()
    usage = body.get("usage", {})
    tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    return text, tokens


# ── LLAMA (Meta) FORMAT ───────────────────────────────────────────────────────

def _call_llama(prompt: str, max_tokens: int, system: Optional[str],
                conversation_history: list, request_id: str) -> tuple[str, int]:
    """Invoke Meta Llama models via Bedrock using the raw prompt format."""
    formatted = "<|begin_of_text|>"
    if system:
        formatted += f"<|start_header_id|>system<|end_header_id|>\n\n{system}<|eot_id|>"
    for msg in (conversation_history or []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        formatted += f"<|start_header_id|>{role}<|end_header_id|>\n\n{content}<|eot_id|>"
    formatted += f"<|start_header_id|>user<|end_header_id|>\n\n{prompt}<|eot_id|>"
    formatted += "<|start_header_id|>assistant<|end_header_id|>\n\n"

    payload = {
        "prompt": formatted,
        "max_gen_len": max_tokens,
        "temperature": 0.2,
        "top_p": 0.9,
    }
    response = bedrock_client.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(payload),
    )
    body = json.loads(response["body"].read())
    text = body.get("generation", "").strip()
    tokens = body.get("prompt_token_count", 0) + body.get("generation_token_count", 0)
    return text, tokens


# ── PUBLIC INTERFACE ──────────────────────────────────────────────────────────

def _bedrock_complete(
    prompt: str,
    max_tokens: int = 1024,
    system: Optional[str] = None,
    conversation_history: list[dict] = None,
) -> str:
    """
    Call AWS Bedrock.  Automatically detects the model family:
      - anthropic.claude*  → Anthropic Messages API
      - meta.llama*        → Llama raw prompt format
    """
    request_id = str(uuid.uuid4())[:8]

    if bedrock_client is None:
        logger.error("Bedrock client is not initialized", extra={"request_id": request_id})
        return "⚠️ The AI service is not properly configured. Please contact support."

    is_claude = BEDROCK_MODEL_ID.startswith("anthropic.claude")
    logger.info(
        "Bedrock invoke", extra={
            "request_id": request_id,
            "model": BEDROCK_MODEL_ID,
            "family": "claude" if is_claude else "llama",
        }
    )

    start = time.time()
    try:
        if is_claude:
            answer, tokens = _call_claude(prompt, max_tokens, system, request_id)
        else:
            answer, tokens = _call_llama(prompt, max_tokens, system,
                                         conversation_history or [], request_id)

        if not answer:
            logger.warning("Bedrock returned empty response", extra={"request_id": request_id})
            return "⚠️ The AI could not generate a response. Try rephrasing your question."

    except (ConnectTimeoutError, ReadTimeoutError) as e:
        elapsed = round(time.time() - start, 2)
        logger.error("Bedrock timeout", extra={"request_id": request_id, "elapsed_s": elapsed, "error": str(e)})
        return "⚠️ The AI service took too long. Try a shorter question or smaller date range."
    except ClientError as e:
        elapsed = round(time.time() - start, 2)
        code = e.response.get("Error", {}).get("Code", "Unknown")
        msg  = e.response.get("Error", {}).get("Message", str(e))
        if code in ("ThrottlingException", "TooManyRequestsException"):
            logger.warning("Bedrock rate limited", extra={"request_id": request_id, "error": msg})
            return "⚠️ The AI service is currently rate-limited. Please wait a few seconds and try again."
        elif code == "AccessDeniedException":
            logger.error("Bedrock access denied", extra={"request_id": request_id, "error": msg})
            return "⚠️ AWS Bedrock Model Access is missing. Please enable Claude 3 Haiku access in AWS Console → Bedrock → Model Access."
        else:
            logger.error("Bedrock ClientError", extra={"request_id": request_id, "error_code": code, "error": msg})
            return "⚠️ Could not reach the AI service. Please try again."
    except Exception as exc:
        logger.error("Bedrock general error", extra={"request_id": request_id, "error": str(exc)})
        return "⚠️ An unexpected error occurred. Please try again."

    elapsed = round(time.time() - start, 2)
    logger.info("Bedrock call succeeded", extra={
        "request_id": request_id,
        "elapsed_s": elapsed,
        "tokens": tokens,
        "model": BEDROCK_MODEL_ID,
    })
    return answer
