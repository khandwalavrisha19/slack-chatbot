from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from pathlib import Path
import requests, os
from dotenv import load_dotenv
from mangum import Mangum
import time
import json
import boto3
from botocore.exceptions import ClientError


load_dotenv()
app = FastAPI()

CLIENT_ID = os.getenv("SLACK_CLIENT_ID")
CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SLACK_REDIRECT_URI")

dynamodb = boto3.resource("dynamodb", region_name="ap-south-1")
table = dynamodb.Table("SlackMessages")

def read_secret(secret_name: str, region: str = "ap-south-1") -> dict:
    client = boto3.client("secretsmanager", region_name=region)
    try:
        resp = client.get_secret_value(SecretId=secret_name)
        secret_string = resp.get("SecretString", "{}")
        return json.loads(secret_string)
    except ClientError as e:
        # Useful error text for debugging
        return {"error": e.response["Error"]["Code"], "message": str(e)}
def upsert_secret(secret_name: str, payload: dict, region: str = "ap-south-1") -> None:
    client = boto3.client("secretsmanager", region_name=region)
    secret_string = json.dumps(payload)

    try:
        client.create_secret(Name=secret_name, SecretString=secret_string)
    except ClientError as e:
        # If secret already exists, update it
        if e.response["Error"]["Code"] == "ResourceExistsException":
            client.put_secret_value(SecretId=secret_name, SecretString=secret_string)
        else:
            raise
@app.get("/db/test-write")
def db_test_write():
    item = {
        "team_id": "TEST_TEAM",
        "ts": str(time.time()),
        "channel_id": "C_TEST",
        "text": "hello from test write",
        "created_at": int(time.time())
    }
    table.put_item(Item=item)
    return {"ok": True, "saved": item}

# @app.get("/", response_class=HTMLResponse)
# def serve_frontend():
#     return Path("index.html").read_text(encoding="utf-8")
@app.get("/")
def root():
    return {"ok": True, "service": "slackbot-backend"}

@app.get("/install")
def install():
    slack_oauth_url = (
        "https://slack.com/oauth/v2/authorize"
        f"?client_id={CLIENT_ID}"
        "&scope=channels:history,users:read,chat:write"
        f"&redirect_uri={REDIRECT_URI}"
    )
    return RedirectResponse(slack_oauth_url)
@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/debug/secret")
def debug_secret():
    # This reads the exact secret you created in Step 1
    data = read_secret("slackbot/TEST_TEAM")

    # ✅ IMPORTANT: do NOT return real tokens in production.
    # For test only, we will mask the token.
    token = data.get("bot_token", "")
    masked = (token[:4] + "..." + token[-4:]) if token else ""

    return {
        "ok": True,
        "team_id": data.get("team_id"),
        "bot_token_masked": masked,
        "raw_has_error": "error" in data
    }
@app.get("/token/status")
def token_status(team_id: str):
    name = f"slackbot/{team_id}"
    try:
        s = read_secret(name)
        if "error" in s:
            return {"ok": True, "team_id": team_id, "has_token": False, "error": s["error"]}
        return {"ok": True, "team_id": team_id, "has_token": True, "scope": s.get("scope")}
    except Exception as e:
        return {"ok": False, "team_id": team_id, "has_token": False, "message": str(e)}


@app.get("/debug/write-secret")
def debug_write_secret():
    name = "slackbot/WRITE_TEST"
    try:
        upsert_secret(name, {"hello": "world"})
        return {"ok": True, "wrote": name}
    except Exception as e:
        print("WRITE TEST FAILED:", str(e))
        return {"ok": False, "error": str(e)}

@app.get("/oauth/callback")
def oauth_callback(code: str | None = None, error: str | None = None):
    try:
        if error:
            return HTMLResponse(f"<h3>Slack install failed</h3><p>{error}</p>", status_code=400)
        if not code:
            return HTMLResponse("<h3>Slack install failed</h3><p>Missing code</p>", status_code=400)

        r = requests.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            timeout=20,
        )
        data = r.json()

        if not data.get("ok"):
            # show Slack error safely
            return HTMLResponse(
                f"<h3>Slack install failed</h3><p>{data.get('error','unknown_error')}</p>",
                status_code=400
            )

        # Use .get() to avoid KeyError crashes
        team = data.get("team") or {}
        team_id = team.get("id")
        team_name = team.get("name")
        bot_token = data.get("access_token")
        bot_user_id = data.get("bot_user_id")
        scope = data.get("scope")

        if not team_id or not bot_token:
            return HTMLResponse("<h3>Slack install failed</h3><p>Missing team_id or access_token</p>", status_code=500)

        # Save secret per workspace
        upsert_secret(f"slackbot/{team_id}", {
            "team_id": team_id,
            "team_name": team_name,
            "bot_user_id": bot_user_id,
            "bot_token": bot_token,
            "scope": scope
        })

        BASE_URL = "https://fcemnui289.execute-api.ap-south-1.amazonaws.com"
        return RedirectResponse(url=f"{BASE_URL}/success?team={team_id}", status_code=302)

    except Exception as e:
        print("OAUTH CALLBACK ERROR:", str(e))
        return HTMLResponse("<h3>Internal error during install</h3><p>Check CloudWatch logs.</p>", status_code=500)

@app.get("/success", response_class=HTMLResponse)
def success(team: str | None = None):
    return HTMLResponse(
        f"<h2>✅ Installed successfully</h2><p>Workspace connected: <b>{team or 'unknown'}</b></p>"
        "<p>You can close this tab now.</p>"
    )

@app.post("/slack/events")
async def slack_events(request: Request):
    data = await request.json()

    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge")}
    event = data.get("event", {}) 
    print("EVENT RECEIVED:", event)

    # Handle events here
    return {"ok": True}
@app.delete("/workspaces/{team_id}")
def disconnect_workspace(team_id: str):
    secret_name = f"slackbot/{team_id}"

    # 1) Read bot token from Secrets Manager
    secret = read_secret(secret_name)
    if "error" in secret:
        return {"ok": False, "team_id": team_id, "message": "Secret not found", "detail": secret}

    bot_token = secret.get("bot_token")
    if not bot_token:
        return {"ok": False, "team_id": team_id, "message": "bot_token missing in secret"}

    # 2) Revoke token in Slack
    r = requests.post(
        "https://slack.com/api/auth.revoke",
        headers={"Authorization": f"Bearer {bot_token}"},
        data={"test": "false"},
        timeout=20,
    )
    revoke_data = r.json()

    # 3) Delete workspace messages (optional for MVP) OR keep
    # (Skipping messages delete for now)

    # 4) Delete Secret (force delete for dev)
    sm = boto3.client("secretsmanager", region_name="ap-south-1")
    try:
        sm.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
    except Exception as e:
        pass

    return {"ok": True, "team_id": team_id, "revoked": revoke_data}

@app.get("/workspaces")
def list_workspaces():
    # MVP: list all workspaces that have a stored secret prefix slackbot/
    # (Better approach: store workspace mapping in DynamoDB table later)
    sm = boto3.client("secretsmanager", region_name="ap-south-1")

    workspaces = []
    paginator = sm.get_paginator("list_secrets")

    for page in paginator.paginate():
        for s in page.get("SecretList", []):
            name = s.get("Name", "")
            if name.startswith("slackbot/"):
                team_id = name.split("slackbot/")[-1]
                workspaces.append({"team_id": team_id, "secret_name": name})

    return {"ok": True, "workspaces": workspaces}
@app.get("/channels")
def list_channels(team_id: str):
    secret_name = f"slackbot/{team_id}"
    secret = read_secret(secret_name)

    if "error" in secret:
        return {"ok": False, "message": "Secret not found", "detail": secret}

    bot_token = secret.get("bot_token")
    if not bot_token:
        return {"ok": False, "message": "bot_token missing"}

    r = requests.get(
        "https://slack.com/api/conversations.list",
        headers={"Authorization": f"Bearer {bot_token}"},
        params={"limit": 100},
        timeout=20,
    )

    data = r.json()

    if not data.get("ok"):
        return {"ok": False, "slack_error": data}

    channels = [
        {"id": c["id"], "name": c["name"]}
        for c in data.get("channels", [])
    ]

    return {"ok": True, "channels": channels}
@app.get("/fetch-messages")
def fetch_messages(team_id: str, channel_id: str):
    secret_name = f"slackbot/{team_id}"
    secret = read_secret(secret_name)

    if "error" in secret:
        return {"ok": False, "message": "Secret not found"}

    bot_token = secret.get("bot_token")
    if not bot_token:
        return {"ok": False, "message": "bot_token missing"}

    r = requests.get(
        "https://slack.com/api/conversations.history",
        headers={"Authorization": f"Bearer {bot_token}"},
        params={"channel": channel_id, "limit": 50},
        timeout=20,
    )

    data = r.json()

    if not data.get("ok"):
        return {"ok": False, "slack_error": data}

    messages = [
        {
            "ts": m.get("ts"),
            "text": m.get("text"),
            "user": m.get("user")
        }
        for m in data.get("messages", [])
    ]

    return {"ok": True, "messages": messages}

# handler = Mangum(app)
