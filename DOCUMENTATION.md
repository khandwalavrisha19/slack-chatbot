# Slackbot AI Assistant — Complete Documentation

> **Live URL**: https://d2586s68tp0zxo.cloudfront.net/  
> **Repository**: github.com/khandwalavrisha19/slack-chatbot  
> **Current Environment**: AWS `ap-south-1` (Mumbai)

---

## Table of Contents
1. [Product Overview](#1-product-overview)
2. [Architecture](#2-architecture)
3. [Features](#3-features)
4. [User Guide](#4-user-guide)
5. [API Reference](#5-api-reference)
6. [Security Model](#6-security-model)
7. [Cost Analysis](#7-cost-analysis)
8. [Production Readiness Roadmap](#8-production-readiness-roadmap)
9. [FAQ](#9-faq)

---

## 1. Product Overview

The **Slackbot AI Assistant** connects your Slack workspace to an AI-powered search and Q&A interface. It lets you search archived Slack messages and ask questions in plain English, with AI-generated answers citing the exact source messages.

### Core Value Proposition

| Without AI Assistant | With AI Assistant |
|:---|:---|
| Manually scrolling through Slack history | Keyword search across any channel instantly |
| Searching channel by channel | Multi-channel search in one click |
| No way to "query" conversations | Ask "What did @alice say about the API deadline?" |
| No audit trail for answers | Every answer cites the original message [1], [2] |

---

## 2. Architecture

### Infrastructure Overview

```
User Browser
    │
    ▼
┌──────────────────────┐
│    AWS CloudFront    │  CDN + HTTPS termination
│  d2586s68tp0zxo...   │  (Mumbai — ap-south-1)
└───────────┬──────────┘
            │
     ┌──────┴──────┐
     │             │
     ▼             ▼
┌─────────┐  ┌───────────────┐
│   S3    │  │  API Gateway  │  /api/* and /slack/* routes
│  Bucket │  └───────┬───────┘
│ (HTML)  │          │
└─────────┘          ▼
               ┌─────────────┐
               │   Lambda    │  Python 3.11 · 1024 MB · 60s timeout
               │  (Docker)   │  FastAPI + Mangum
               └──────┬──────┘
                      │
         ┌────────────┼────────────┐
         ▼            ▼            ▼
  ┌────────────┐ ┌─────────┐ ┌───────────┐
  │  DynamoDB  │ │ Secrets │ │ Groq API  │
  │  Messages  │ │ Manager │ │  (LLM)    │
  │  Sessions  │ │(tokens) │ │ ⚠️ Ext.  │
  └────────────┘ └─────────┘ └───────────┘
```

### Key Components

| Component | Technology | Purpose |
|:---|:---|:---|
| **Frontend** | HTML + Vanilla JS | Dashboard UI served from S3 via CloudFront |
| **Backend** | Python 3.11 / FastAPI | REST API for all business logic |
| **Runtime** | AWS Lambda (Docker image) | Serverless — pay-per-request |
| **Database** | AWS DynamoDB (2 tables) | Stores messages & user sessions |
| **Secrets** | AWS Secrets Manager | Stores Slack bot tokens per workspace |
| **CDN** | AWS CloudFront | HTTPS, routing `/api/*` to Lambda |
| **CI/CD** | GitHub Actions (OIDC) | Auto-deploy on push to `develop`/`main` |
| **AI** | Groq API (Llama 3.3 70B) | LLM for question answering ⚠️ *External* |

### Application Modules

```
backend/app/
├── main.py           # App entry point (FastAPI + Mangum Lambda handler)
├── constants.py      # All environment config (region, model, limits)
├── routes.py         # All API endpoint definitions
├── models.py         # Pydantic request schemas with validation
├── utils.py          # AWS helpers (secrets, DynamoDB, Slack sig. verify)
├── retrieval.py      # Message search, scoring, context building for LLM
├── session.py        # Session management (create, validate, expire)
├── slack_handler.py  # Slack event handler (bot mentions, commands)
├── groq_client.py    # LLM inference client
├── exceptions.py     # Global error handlers & request size middleware
└── logger.py         # Structured JSON logging to CloudWatch
```

### DynamoDB Schema

**Messages Table** (`{env}-SlackMessagesV2`)

| Key | Type | Example |
|:---|:---|:---|
| `pk` *(Partition Key)* | String | `T01234567#C09876543` (teamId#channelId) |
| `sk` *(Sort Key)* | String | `1712345678.123456` (Slack timestamp) |
| `team_id` | String | `T01234567` |
| `channel_id` | String | `C09876543` |
| `user_id` | String | `U00000001` |
| `username` | String | `alice` |
| `text` | String | Full message content |
| `thread_ts` | String | Parent thread timestamp (if reply) |
| `fetched_at` | String | ISO timestamp of when stored |

**Sessions Table** (`{env}-slackbot-sessions`)

| Key | Type | Description |
|:---|:---|:---|
| `session_id` *(PK)* | String | UUID — browser session |
| `team_ids` | List | Workspace IDs connected to this session |
| `expires_at` | Number | Unix epoch — auto-deleted by DDB TTL |

---

## 3. Features

### 3.1 Connect Slack Workspace
- Single OAuth 2.0 flow — one click install
- Bot token stored encrypted in AWS Secrets Manager (never in DB or logs)
- Multiple workspaces per session
- Session auto-expires after **72 hours**

### 3.2 Channel Backfill

| Action | What it does |
|:---|:---|
| **Join + Backfill** | Bot joins one channel and imports its full history |
| **Backfill Public** | Imports all public channels the bot is a member of |
| **Backfill Private** | Imports all private channels the bot has been invited to |

- Paginated: 200 messages per API call with automatic cursor handling
- Idempotent: messages already in DB are skipped (no duplicates)

### 3.3 Keyword Search
- Searches stored messages by keyword
- Filters: date range, username, channel
- **Multi-channel mode**: search across up to 20 channels at once
- Results show sender name, timestamp, and message snippet

**Scoring algorithm:** keyword frequency + early-occurrence bonus + phrase-match bonus. Bot/join/leave messages are filtered out.

### 3.4 Ask AI

Flagship feature. Type a question → AI reads your stored Slack messages → returns a cited answer.

**Supported query patterns:**
- `"What did the team decide about the release date?"` — semantic
- `"What did @alice say about the API?"` — user-filtered
- `"Any action items from last week?"` — recency-boosted
- `"What was the last message in #general?"` — positional

**Pipeline:**
1. Extract `@username` mention from question (if any)
2. Query DynamoDB (up to 200 messages retrieved, top 10–12 used)
3. Score and rank messages by relevance
4. Build context string (max 8,000 characters)
5. Call LLM with strict system prompt + context + question
6. Parse citations (`[1]`, `[2]`, ...) from the response
7. Return answer + linked source messages

### 3.5 Slack Bot (Direct Integration)

Bot responds inside Slack when mentioned (`@BotName`) or via DM.

```
ask #channel-name <question>
search #channel-name <keywords>
summarize #channel-name
summarize #channel-name last 50
ask all <question>          ← searches ALL channels
search all <keywords>
summarize all
```

### 3.6 Load from DB
Browse raw stored messages directly from DynamoDB — no AI involved.

---

## 4. User Guide

### Step 1 — Open the App
Go to: **https://d2586s68tp0zxo.cloudfront.net/**

### Step 2 — Connect Your Slack Workspace
1. Click **"Connect Slack"**
2. Sign in on the Slack popup → click **Allow**
3. Your workspace appears in the dropdown

> You can connect **multiple workspaces** and switch between them freely.

### Step 3 — Load Channels
Click **"Load Channels"**, then pick a channel from the dropdown.

### Step 4 — Backfill Messages *(First-Time Only)*
| Button | Use it when... |
|:---|:---|
| **Join + Backfill** | Adding one new channel |
| **Backfill Public** | First time setup — imports everything |
| **Backfill Private** | Bot is already in private channels you want to search |

> You only need to do this **once per channel**. Real-time messages are captured automatically after that.

### Step 5 — Search Messages
1. Select channel(s) → type keyword → optionally set date/username filter → **Search**
2. Results show: sender, timestamp, matching text

### Step 6 — Ask the AI
1. Select channel(s) → type question → optionally set date range → **Ask**
2. AI reads your stored messages and shows a cited answer

**Example questions:**
- `"What did the team decide about the release date?"`
- `"What did @alice say about the API?"`
- `"Any action items from last week?"`

---

## 5. API Reference

All endpoints use `/api/` prefix. CloudFront routes `/api/*` to the Lambda function.

### Authentication
All endpoints require a valid `sb_session` cookie (created automatically on first visit). Team-scoped endpoints additionally verify workspace ownership.

### Endpoint Summary

#### Session & OAuth
| Method | Path | Description |
|:---|:---|:---|
| `GET` | `/api/session` | Get or create session |
| `POST` | `/api/logout` | Clear session cookie |
| `GET` | `/api/install` | Redirect to Slack OAuth |
| `GET` | `/api/oauth/callback` | Handle OAuth, store bot token |

#### Workspace
| Method | Path | Description |
|:---|:---|:---|
| `GET` | `/api/workspaces` | List connected workspaces |
| `DELETE` | `/api/workspaces/{team_id}` | Disconnect + revoke token |
| `GET` | `/api/token/status` | Check if token is valid |

#### Channels & Messages
| Method | Path | Description |
|:---|:---|:---|
| `GET` | `/api/channels` | List all channels |
| `GET` | `/api/fetch-messages` | Fetch live from Slack API |
| `GET` | `/api/db-messages` | Load stored from DynamoDB |
| `POST` | `/api/join-channel` | Bot joins a channel |
| `POST` | `/api/join-all-public` | Bot joins all public channels |
| `POST` | `/api/backfill-channel` | Import history for one channel |
| `POST` | `/api/backfill-all-public` | Import all public channels |
| `POST` | `/api/backfill-all-private` | Import all private channels |

#### Search & AI
| Method | Path | Description |
|:---|:---|:---|
| `GET` | `/api/search` | Keyword search (single channel) |
| `GET` | `/api/search/multi` | Keyword search (multi-channel) |
| `POST` | `/api/chat` | AI Q&A (single channel) |
| `POST` | `/api/chat/multi` | AI Q&A (multi-channel) |
| `POST` | `/api/slack/events` | Slack event webhook |
| `GET` | `/api/health` | Health check |

### Sample Request/Response

**POST `/api/chat`**
```json
// Request
{
  "team_id":    "T01234567",
  "channel_id": "C09876543",
  "question":   "What did @alice say about the budget?",
  "from_date":  "2024-01-01",
  "to_date":    "2024-03-31",
  "top_k":      10
}

// Response
{
  "ok":               true,
  "question":         "What did @alice say about the budget?",
  "answer":           "Alice confirmed the Q1 budget was approved [1]...",
  "citations": [
    {
      "username":        "alice",
      "text":            "The Q1 budget has been approved...",
      "timestamp_human": "2024-04-05 10:23 UTC",
      "channel_id":      "C09876543"
    }
  ],
  "retrieved_count":    8,
  "resolved_username": "alice"
}
```

---

## 6. Security Model

### Active Security Controls

| Control | Status | Detail |
|:---|:---|:---|
| HTTPS only | ✅ | CloudFront enforces `redirect-to-https`, TLS 1.2+ |
| Bot token storage | ✅ | AWS Secrets Manager — encrypted, never logged |
| Session isolation | ✅ | Each session can only access its own workspaces (`403` on violation) |
| Slack signature verification | ✅ | Every webhook validates `X-Slack-Signature` + timestamp freshness |
| Request size limit | ✅ | 64 KB hard cap on all POST bodies |
| Input validation | ✅ | Pydantic models validate all inputs (length, format, Slack ID patterns) |
| S3 public access | ✅ | All public ACLs blocked — only CloudFront OAC can read |
| HTTP-only cookies | ✅ | Session cookie not accessible from JavaScript |
| Secure cookie flag | ✅ | `Secure=True` set in production environment |
| Bot rate limiting | ✅ | 20 Slack bot requests per 60 seconds per user |
| Prompt injection guard | ✅ | System prompt strictly limits LLM to provided messages only |

### Current Security Gaps (Pre-Production)

| Gap | Risk Level | Recommended Fix |
|:---|:---|:---|
| **Groq API (3rd-party LLM)** | 🔴 Critical | Switch to AWS Bedrock — keeps all data in AWS |
| **DynamoDB unencrypted with CMK** | 🟡 High | Enable KMS Customer-Managed Key on both tables |
| **Secrets Manager `Resource: "*"`** | 🟡 High | Restrict IAM to `/slackbot/*` prefix only |
| **No WAF protection** | 🟡 High | Add AWS WAF to CloudFront distribution |
| **No audit logging** | 🟡 Medium | Enable AWS CloudTrail |
| **No MFA on AWS Console** | 🟡 Medium | Enforce MFA for all IAM users |
| **No message retention policy** | 🟢 Low | Add DynamoDB TTL (e.g. 180-day auto-expire) |

---

## 7. Cost Analysis

### 7.1 Current Setup (Groq + AWS)

#### AWS Infrastructure — Monthly Estimate

| Service | Usage Assumption | Cost/Month |
|:---|:---|:---|
| **Lambda** | 10,000 invocations @ 1GB·s | ~$0.21 |
| **API Gateway** | 10,000 requests | ~$0.04 |
| **DynamoDB** | On-demand, ~100 MB stored | ~$0.25 |
| **Secrets Manager** | ~5 secrets | ~$0.25 |
| **CloudFront** | ~1 GB transfer | ~$0.09 |
| **ECR** | 1 Docker image ~500 MB | ~$0.05 |
| **S3** | Static frontend files | ~$0.02 |
| **Total AWS** | | **~$0.91/month** |

#### Groq API — Cost

| Metric | Value |
|:---|:---|
| Current model | `llama-3.3-70b-versatile` |
| Free tier | 14,400 requests/day |
| Avg tokens per query | ~3,000 input + ~600 output |
| **Dev cost** | **~$0/month** (free tier) |
| Paid plan (when exceeded) | ~$0.59/1M input tokens |

> ⚠️ **Key Issue**: Every AI query sends your Slack messages to Groq servers (USA). This is the primary data security concern for production use.

---

### 7.2 Proposed Setup: AWS Bedrock

AWS Bedrock performs AI inference entirely inside your AWS account. No data ever leaves AWS.

#### Bedrock Model Pricing (On-Demand, `ap-south-1`)

| Model | Input / 1M tokens | Output / 1M tokens | Recommended For |
|:---|:---|:---|:---|
| **Meta Llama 3.1 8B** | $0.20 | $0.25 | High-volume, simple queries |
| **Meta Llama 3.1 70B** ⭐ | $0.35 | $0.45 | Best quality/cost balance |
| **Meta Llama 3.1 405B** | $0.65 | $0.80 | Complex reasoning |
| **Claude 3.5 Sonnet** | $3.00 | $15.00 | Premium quality |

#### Monthly AI Cost at Different Query Volumes

*(Assumes avg 3,000 input tokens + 600 output tokens per query)*

| Queries/Month | Groq | Llama 3.1 8B | Llama 3.1 70B ⭐ | Claude 3.5 |
|:---|:---|:---|:---|:---|
| 100 | $0 | ~$0.08 | ~$0.14 | ~$1.80 |
| 500 | $0 | ~$0.38 | ~$0.68 | ~$9.00 |
| 2,000 | ~$1.50 | ~$1.50 | ~$2.73 | ~$36.00 |
| 10,000 | ~$7.00 | ~$7.50 | ~$13.65 | ~$180.00 |

> **Recommended**: Llama 3.1 70B — equivalent quality to current Groq model, costs ~$1–15/month depending on usage, 100% data stays in AWS.

---

### 7.3 Full Production Cost (Bedrock + Security Hardening)

| Item | Monthly Cost |
|:---|:---|
| AWS Core Infrastructure | ~$0.91 |
| Bedrock Llama 3.1 70B (500 queries) | ~$0.68 |
| AWS WAF (CloudFront) | ~$5.00 |
| KMS Customer-Managed Key | ~$1.00 |
| CloudTrail (audit log) | ~$2.00 |
| CloudWatch Alarms | ~$0.30 |
| **Total Production** | **~$9–12/month** |

### 7.4 Cost Comparison Summary

| | Current Dev (Groq) | Production (Bedrock) |
|:---|:---|:---|
| Monthly cost | ~$0.91 | ~$10–12 |
| Data stays in AWS? | ❌ No | ✅ Yes |
| Enterprise compliant? | ❌ No | ✅ Yes |
| Audit logs? | ❌ No | ✅ Yes |
| WAF protection? | ❌ No | ✅ Yes |

---

## 8. Production Readiness Roadmap

### Phase 1 — Data Security (Critical, Do First)

| Task | Est. Time | Priority |
|:---|:---|:---|
| Switch LLM: Groq → AWS Bedrock (Llama 3.1 70B) | 2 hours | 🔴 Critical |
| Enable KMS encryption on DynamoDB tables | 30 min | 🔴 Critical |
| Restrict Secrets Manager IAM to `/slackbot/*` path | 15 min | 🔴 Critical |
| **Prerequisite**: Enable Bedrock model access in AWS Console | 5 min (1-day approval) | 🔴 Required |

### Phase 2 — Reliability & Monitoring

| Task | Est. Time | Priority |
|:---|:---|:---|
| Add AWS WAF to CloudFront | 1 hour | 🟡 High |
| CloudWatch error-rate alarm (alert if >5% errors) | 30 min | 🟡 High |
| Lambda concurrency limit (prevent cost spikes) | 10 min | 🟡 High |
| Enable CloudTrail (full AWS audit log) | 15 min | 🟡 Medium |

### Phase 3 — Data Governance (GDPR-Friendly)

| Task | Est. Time | Priority |
|:---|:---|:---|
| DynamoDB TTL on messages (auto-delete after 180 days) | 30 min | 🟢 Medium |
| Add `X-Frame-Options` and `X-Content-Type-Options` headers | 15 min | 🟢 Medium |
| Session cookie `SameSite=Strict` in production | 15 min | 🟢 Medium |

---

## 9. FAQ

**Do I need a Slack admin account?**  
No. You just need permission to install apps. If your workspace requires admin approval, ask your Slack admin.

**Is my data safe?**  
Currently, Slack messages are sent to Groq (a third-party US company) for AI queries. All other data lives in AWS. Switching to Bedrock will keep 100% of your data inside your AWS account.

**The AI gave a wrong answer — what happened?**  
The AI only uses messages stored in DynamoDB. If a channel wasn't backfilled, or a message is missing, the AI won't know about it. Re-backfill the channel and try again.

**My session expired — what do I do?**  
Sessions last 72 hours. Reconnect your Slack workspace — your stored messages don't need to be re-backfilled.

**Can I disconnect a workspace?**  
Yes. Click "Disconnect" to revoke the bot's Slack access and remove the token from Secrets Manager.

**Can multiple team members use this simultaneously?**  
Yes. Each browser gets an independent session. Each user connects the workspace separately (no shared sessions).

**What if I add new channels later?**  
Click "Join + Backfill" for the new channel. Existing messages aren't duplicated. Real-time messages are captured automatically after joining.

**How are new messages stored automatically?**  
Via the Slack Events API. The bot stores every message as it is posted in any channel it's a member of.

**What happens when someone posts in private Slack?**  
The bot only stores messages from channels it has been explicitly invited to. Private messages (DMs between users, not the bot) are never stored.

---

*Documentation version: April 2026*
