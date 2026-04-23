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
    # Use standard retries (Boto3 defaults usually max_attempts=4)
    bedrock_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
except Exception as e:
    logger.error("Failed to initialize Bedrock client", extra={"error": str(e)})
    bedrock_client = None

def _bedrock_complete(prompt: str, max_tokens: int = 1024, system: Optional[str] = None, conversation_history: list[dict] = None) -> str:
    """
    Call AWS Bedrock API for Meta Llama 3.
    """
    request_id = str(uuid.uuid4())[:8]
    if bedrock_client is None:
        logger.error("Bedrock client is not initialized", extra={"request_id": request_id})
        return "⚠️ The AI service is not properly configured. Please contact support."

    # For Llama 3 70B Instruct on Bedrock, we format using prompt standard structure
    # Usually: <|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{system_prompt}<|eot_id|><|start_header_id|>user<|end_header_id|>\n\n{user_prompt}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n
    
    formatted_prompt = "<|begin_of_text|>"
    if system:
        formatted_prompt += f"<|start_header_id|>system<|end_header_id|>\n\n{system}<|eot_id|>"
    
    if conversation_history:
        for msg in conversation_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            formatted_prompt += f"<|start_header_id|>{role}<|end_header_id|>\n\n{content}<|eot_id|>"
    
    formatted_prompt += f"<|start_header_id|>user<|end_header_id|>\n\n{prompt}<|eot_id|>"
    formatted_prompt += "<|start_header_id|>assistant<|end_header_id|>\n\n"

    payload = {
        "prompt": formatted_prompt,
        "max_gen_len": max_tokens,
        "temperature": 0.2,
        "top_p": 0.9,
    }

    start = time.time()
    try:
        response = bedrock_client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(payload),
        )
        response_body = json.loads(response.get("body").read())
        answer = response_body.get("generation", "").strip()

    except (ConnectTimeoutError, ReadTimeoutError) as e:
        elapsed = round(time.time() - start, 2)
        logger.error("Bedrock timeout", extra={"request_id": request_id, "elapsed_s": elapsed, "error": str(e)})
        return "⚠️ The AI service took too long. Try a shorter question or smaller date range."
    except ClientError as e:
        elapsed = round(time.time() - start, 2)
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))
        
        if error_code in ("ThrottlingException", "TooManyRequestsException"):
            logger.warning("Bedrock rate limited", extra={"request_id": request_id, "error": error_message})
            return "⚠️ The AI service is currently rate-limited. Please wait a few seconds and try again."
        elif error_code == "AccessDeniedException":
            logger.error("Bedrock access denied", extra={"request_id": request_id, "error": error_message})
            return "⚠️ AWS Bedrock Model Access is missing. Please enable access to this model in the AWS Console."
        else:
            logger.error("Bedrock ClientError", extra={"request_id": request_id, "error_code": error_code, "error": error_message})
            return "⚠️ Could not reach the AI service due to an internal error. Please try again."
    except Exception as exc:
        logger.error("Bedrock general error", extra={"request_id": request_id, "error": str(exc)})
        return "⚠️ An unexpected networking error occurred. Please try again."

    elapsed = round(time.time() - start, 2)
    logger.info("Bedrock call succeeded", extra={
        "request_id": request_id,
        "elapsed_s":  elapsed,
        "usage":      response_body.get("prompt_token_count", 0) + response_body.get("generation_token_count", 0),
    })
    return answer
