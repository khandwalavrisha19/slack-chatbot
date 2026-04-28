# Slack AI Chatbot — Complete Technical Documentation

**Project:** Internal Slack AI Chatbot 
**Author:** Vrisha Khandwala
**Date:** April 2026
**AWS Region:** us-west-2 (Oregon)

---

## 1. Project Overview

This project is an **AI-powered internal Slack assistant** that enables employees to:

- 🔍 **Search** company Slack messages by keyword, username, channel, or date
- 🤖 **Ask AI questions** about historical conversations (e.g. *"What did the team decide about the API deadline?"*)
- 📊 **Manage workspaces** via a secure web dashboard
- 📥 **Backfill** historical Slack data into the database on demand

The system uses a **RAG (Retrieval-Augmented Generation)** architecture — it retrieves relevant Slack messages from a database and passes them to an AI model, ensuring the AI only answers based on your actual company data, not general internet knowledge.

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    USER'S BROWSER                        │
└──────────────────────────┬──────────────────────────────┘
                           │ HTTPS
                           ▼
┌─────────────────────────────────────────────────────────┐
│              Amazon CloudFront (CDN)                     │
│  Routes:  /  → S3 (Frontend HTML)                       │
│          /api/* → API Gateway → Lambda                   │
└────────────┬────────────────────────┬───────────────────┘
             │                        │
             ▼                        ▼
    ┌──────────────┐       ┌──────────────────────┐
    │  Amazon S3   │       │  API Gateway HTTP API │
    │  (Frontend)  │       └──────────┬───────────┘
    └──────────────┘                  │
                                      ▼
                           ┌──────────────────────┐
                           │    AWS Lambda         │
                           │  (FastAPI + Python)   │
                           └───┬──────────────┬───┘
                               │              │
                    ┌──────────▼───┐   ┌──────▼──────────┐
                    │  Amazon RDS  │   │   AWS Bedrock    │
                    │  PostgreSQL  │   │  Llama 3.1 70B   │
                    │ (db.t4g.micro│   │  (AI Inference)  │
                    └──────────────┘   └─────────────────┘
                               │
                    ┌──────────▼────────────────┐
                    │   AWS Secrets Manager      │
                    │   (Slack Bot Tokens)        │
                    └───────────────────────────┘
```

---

## 3. All AWS Services Used

---

### 3.1 AWS Lambda

**What it is:**
AWS Lambda is a serverless compute service that runs code without provisioning or managing servers. Code runs only when triggered by a request.

**Why we use it:**
Our backend (FastAPI + Python) is deployed as a Docker container on Lambda. It handles all API requests — from Slack OAuth to AI queries to database reads.

**Technical details:**
- Runtime: Python 3.12 (container image)
- Memory: 1024 MB
- Timeout: 60 seconds
- Deployed via Docker image stored in Amazon ECR

**Why serverless over EC2:**
| Factor | Lambda (Serverless) | EC2 (Traditional Server) |
|--------|--------------------|-----------------------------|
| Cost model | Pay per request | Pay 24/7 even when idle |
| Scaling | Automatic | Manual |
| Maintenance | None | OS patches, updates |
| Idle cost | $0 | ~$15–30/month minimum |

**Cost:**
- Free tier: 1 million requests + 400,000 GB-seconds per month (permanent, not trial)
- Beyond free tier: $0.20 per million requests + $0.0000167 per GB-second
- **Our estimated cost: $0 – $2/month** (well within free tier for all scenarios below 500 users)

---

### 3.2 Amazon API Gateway (HTTP API)

**What it is:**
API Gateway is the entry point for all HTTP requests to our backend. It acts as a secure "front door" that accepts requests from CloudFront and routes them to Lambda.

**Why we use it:**
- Sits between CloudFront and Lambda
- Handles request routing, throttling, and HTTPS termination
- HTTP API type is 70% cheaper than REST API for our use case

**Technical details:**
- Type: HTTP API (v2) — not REST API
- Default route `$default` → Lambda proxy integration
- Payload format version: 2.0

**Cost:**
- $1.00 per million HTTP API calls
- **Our estimated cost: $0.01 – $0.20/month** across all usage scenarios

---

### 3.3 Amazon CloudFront

**What it is:**
CloudFront is AWS's global Content Delivery Network (CDN). It caches and delivers content from locations closest to the user.

**Why we use it:**
- Serves the frontend (HTML dashboard) from S3 with low latency globally
- Routes all `/api/*` calls to API Gateway
- Provides HTTPS for the entire application without us managing SSL certificates
- Adds a security layer between the internet and our backend

**Technical details:**
- Origin 1: S3 bucket (frontend HTML)
- Origin 2: API Gateway (backend API)
- Cache policy: No-cache for API, 1-day for static assets
- Custom domain: `d7yqw1hafu45p.cloudfront.net`

**Cost:**
- First 1 TB of data transfer per month: **FREE** (permanent free tier)
- First 10 million HTTP requests per month: **FREE**
- **Our estimated cost: $0/month** (well within free tier)

---

### 3.4 Amazon S3 (Simple Storage Service)

**What it is:**
S3 is AWS's object storage service. We use it to host the static frontend (HTML, CSS, JS).

**Why we use it:**
- No server needed to serve a static frontend
- Extremely reliable (99.999999999% durability)
- Integrates directly with CloudFront

**Technical details:**
- Bucket policy: Only accessible via CloudFront (not directly from internet)
- Frontend size: ~2 MB
- Deployment: GitHub Actions uploads the built frontend on every push

**Cost:**
- $0.023 per GB storage
- First 5 GB storage free
- **Our estimated cost: $0/month** (frontend is only ~2 MB)

---

### 3.5 Amazon RDS PostgreSQL ⭐ Primary Database

**What it is:**
Amazon RDS (Relational Database Service) is a managed SQL database. We use PostgreSQL — the world's most advanced open-source relational database.

**Why we moved from DynamoDB to RDS PostgreSQL:**

| Problem with DynamoDB (old) | Solution with PostgreSQL (new) |
|-----------------------------|-------------------------------|
| To filter by username, download ALL messages into Python memory | `WHERE username ILIKE '%Vrisha%'` — database filters instantly |
| No date range queries | `WHERE sk BETWEEN '2024-01-01' AND '2024-03-01'` |
| Can't filter by multiple fields simultaneously | `WHERE user_id=? AND channel=? AND text ILIKE '%billing%'` |
| No full-text search capability | Native `ILIKE`, `tsvector`, future `pgvector` |
| Session storage was complex | Simple `sessions` table with TTL |

**Database Schema:**
```
messages table:      Stores all Slack messages (pk, sk, user_id, username, text, channel_id, team_id)
sessions table:      OAuth session management (session_id, team_ids, expires_at)
users_cache table:   Caches Slack username lookups (team_id, user_id, display_name)
```

**Why `db.t4g.micro` instance:**
- `t` = Burstable — ideal for workloads that are idle most of the time (chatbots are used occasionally, not constantly)
- `4g` = Graviton 2 (ARM chip) — 40% cheaper than Intel equivalent
- `micro` = 2 vCPU, 1 GB RAM — sufficient for our dataset size
- `gp3` storage — 20% cheaper than `gp2`, 3× more IOPS

**Cost:**
- `db.t4g.micro`: $0.016/hour = $11.68/month
- Storage 20 GB gp3: $2.30/month
- **Our estimated cost: $13 – $15/month** (fixed regardless of user count)

---

### 3.6 AWS Bedrock (AI Inference) ⭐ AI Engine

**What it is:**
AWS Bedrock is Amazon's managed AI service. It hosts foundation models (AI models) from multiple providers (Meta, Anthropic, Amazon) and runs them entirely within your AWS account.

**Why we moved from Groq to AWS Bedrock:**

| Problem with Groq (old) | Solution with Bedrock (new) |
|------------------------|------------------------------|
| Slack data sent to external servers | Data never leaves AWS account |
| No compliance guarantee | Full AWS data residency |
| No audit trail | Every call logged in CloudWatch |
| Vendor dependency | AWS-managed, not a startup risk |

**Model Used: Meta Llama 3.1 70B Instruct**

| Reason | Detail |
|--------|--------|
| **70B parameters** | Larger models understand complex multi-part questions better (e.g. "who mentioned X and what was the context?") |
| **Llama 3.1 generation** | Meta's 2024 release — significantly better instruction following than earlier versions |
| **Instruct variant** | Fine-tuned specifically to follow instructions and answer questions precisely — not just generate text |
| **Open source** | No per-seat licensing, no usage caps |
| **AWS hosted** | Runs inside our VPC — zero external data transfer |

**How it works (RAG pipeline):**
```
User Question
     │
     ▼
Retrieve relevant Slack messages from PostgreSQL
(SQL query with keyword + user + date filters)
     │
     ▼
Build context string from retrieved messages
(up to 5,000 characters of actual Slack content)
     │
     ▼
Send to Bedrock: [System Prompt] + [Context] + [Question]
     │
     ▼
Bedrock runs Llama 3.1 70B — generates answer
based ONLY on provided Slack messages
     │
     ▼
Return answer + cited sources to user
```

**Pricing (Cross-Region Inference, us-west-2):**
- Input tokens: $0.00072 per 1,000 tokens
- Output tokens: $0.00072 per 1,000 tokens
- Average per query: ~2,750 input + 500 output = 3,250 tokens = **$0.00234 per query**

**Cost varies directly with usage — see Section 5.**

---

### 3.7 AWS Secrets Manager

**What it is:**
Secrets Manager securely stores and retrieves sensitive credentials. We use it to store Slack bot tokens for each connected workspace.

**Why we use it:**
- Slack bot tokens are sensitive — if leaked, anyone can read your Slack messages
- Secrets Manager encrypts at rest using AWS KMS
- Tokens are retrieved at runtime — never hardcoded in source code
- Automatic rotation support (future enhancement)

**Technical details:**
- One secret per Slack workspace (named: `dev-slackbot/{team_id}`)
- Accessed via IAM role — Lambda has least-privilege access
- Never logged, never in environment variables in plaintext

**Cost:**
- $0.40 per secret per month
- ~4 secrets for typical setup
- **Our estimated cost: $1.60 – $2.50/month**

---

### 3.8 Amazon CloudWatch

**What it is:**
CloudWatch is AWS's monitoring and observability service. All Lambda logs go here automatically.

**Why we use it:**
- Every API request, error, and AI call is logged
- Track response times and identify slow queries
- Monitor Bedrock AI usage and token consumption
- Set billing alarms to catch unexpected cost spikes

**Cost:**
- First 5 GB of log ingestion per month: **FREE**
- **Our estimated cost: $0 – $1/month**

---

### 3.9 Amazon ECR (Elastic Container Registry)

**What it is:**
ECR is AWS's private Docker image repository. Our Lambda runs as a Docker container image — ECR stores that image.

**Why we use it:**
- Lambda container images must be stored in ECR
- Private registry — images are not publicly accessible
- Automatic scanning for security vulnerabilities

**Cost:**
- First 500 MB storage free
- $0.10 per GB-month after
- **Our estimated cost: $0.05 – $0.10/month**

---

### 3.10 Amazon EventBridge (Keep-Warm Rule)

**What it is:**
EventBridge is AWS's event bus service. We use it to schedule a recurring ping to Lambda every 5 minutes.

**Why we use it:**
Lambda "sleeps" after periods of inactivity — the first request after sleep incurs a "cold start" (3–5 second delay). The EventBridge rule pings Lambda every 5 minutes to keep it loaded in memory, eliminating cold starts for users.

**Cost:**
- First 14 million events per month: **FREE**
- 5-minute pings = 8,640 events/month — well within free tier
- **Cost: $0/month**

---

### 3.11 GitHub Actions (CI/CD)

**What it is:**
GitHub Actions is the CI/CD (Continuous Integration / Continuous Deployment) pipeline. Every `git push` automatically builds and deploys the application.

**Pipeline steps on every push:**
```
1. Build Docker image (Python + FastAPI + dependencies)
2. Push image to Amazon ECR
3. Deploy CloudFormation stack (creates/updates all AWS resources)
4. Upload frontend (index.html) to S3
5. Invalidate CloudFront cache (so users see latest version)
```

**Cost:** Free for public repositories; 2,000 minutes/month free for private repos. **Effective cost: $0/month**

---

## 4. Security Implementation

| Area | Implementation |
|------|---------------|
| **Slack tokens** | Stored encrypted in AWS Secrets Manager — never in code |
| **Database credentials** | Injected via CloudFormation from GitHub Secrets — never hardcoded |
| **RDS encryption** | `StorageEncrypted: true` using AWS KMS |
| **Session management** | HTTP-only cookies, 72-hour TTL, server-side sessions in PostgreSQL |
| **Slack webhook** | Every event verified using `X-Slack-Signature` (HMAC-SHA256) |
| **S3 frontend** | Not publicly accessible — only via CloudFront with signed requests |
| **Data residency** | All processing in `us-west-2` — no data leaves AWS account |
| **IAM** | Lambda role follows least-privilege — can only access Bedrock, Secrets, RDS |

---

## 5. Cost Analysis

### Assumptions

| Parameter | Small Team | Mid-Size | Large Team |
|-----------|-----------|----------|------------|
| Users | 10 | 150 | 200 |
| AI queries/user/day | 10 | 12 | 15 |
| Total queries/month | 3,000 | 54,000 | 90,000 |
| Working days/month | 22 | 22 | 22 |

---

### Monthly Cost by Service

| Service | 10 Users | 150 Users | 200 Users |
|---------|----------|-----------|-----------|
| AWS Lambda | $0.00 | $0.00 | $0.00 |
| API Gateway | $0.01 | $0.05 | $0.09 |
| CloudFront | $0.00 | $0.00 | $0.00 |
| Amazon S3 | $0.00 | $0.00 | $0.00 |
| **RDS PostgreSQL** | **$13.98** | **$13.98** | **$24.82*** |
| **AWS Bedrock (AI)** | **$7.02** | **$126.36** | **$210.60** |
| Secrets Manager | $1.63 | $1.63 | $2.05 |
| CloudWatch | $0.00 | $0.00 | $0.50 |
| ECR | $0.05 | $0.05 | $0.05 |
| EventBridge | $0.00 | $0.00 | $0.00 |
| **TOTAL** | **~$23 – $30** | **~$140 – $175** | **~$240 – $280** |

*At 200 users, RDS recommended upgrade to `db.t4g.small` ($24.82/month)

---

### Cost Summary (Range Format)

| Scale | Monthly Range | Annual Range | Per User/Month |
|-------|--------------|--------------|----------------|
| **10 users (Pilot)** | **$20 – $30** | **$240 – $360** | **$2 – $3** |
| **150 users (Team)** | **$140 – $180** | **$1,680 – $2,160** | **$0.93 – $1.20** |
| **200 users (Full Org)** | **$240 – $290** | **$2,880 – $3,480** | **$1.20 – $1.45** |

> **Cost validated using**: AWS Pricing Calculator — https://calculator.aws
> Primary cost driver: AWS Bedrock AI inference (~80% of total at scale)

---

### Cost vs Alternatives

| Architecture | 10 Users/mo | 150 Users/mo | Data Privacy |
|-------------|-------------|--------------|--------------|
| **Our Stack (Lambda + RDS + Bedrock)** | **~$25** | **~$160** | ✅ Data in AWS |
| Lambda + DynamoDB + Groq (previous) | ~$15 | ~$300+ | ❌ Data leaves AWS |
| ECS Fargate + RDS + OpenAI GPT-4 | ~$80 | ~$500+ | ❌ Data leaves AWS |
| EC2 + Self-hosted LLM | ~$50 | ~$150 | ✅ But manual ops |

---

### Free Credits Coverage

> Current AWS account: **$100 free credits**

| Scale | Monthly Cost | Credits Last |
|-------|-------------|-------------|
| 10 users (pilot) | ~$25 | **~4 months** |
| 150 users (team) | ~$160 | **~19 days** |

Recommendation: Use the pilot period (10 users) to validate the system before scaling to 150 users.

---

## 6. Technology Migrations Performed

### Migration 1: Groq → AWS Bedrock
| | Before | After |
|-|--------|-------|
| AI Provider | Groq (external API) | AWS Bedrock (inside AWS) |
| Data privacy | ❌ Slack data sent externally | ✅ Data never leaves AWS |
| Model | Llama 3 70B via Groq | Meta Llama 3.1 70B via Bedrock |
| Monitoring | None | CloudWatch full logging |
| Compliance | ❌ Not enterprise-grade | ✅ AWS data residency |

### Migration 2: DynamoDB → RDS PostgreSQL
| | Before | After |
|-|--------|-------|
| Database | DynamoDB (NoSQL) | PostgreSQL (Relational SQL) |
| User filter | Download all → Python loop | `WHERE user_id = ?` in SQL |
| Keyword search | Not supported natively | `WHERE text ILIKE '%keyword%'` |
| Multi-filter | Impossible | `WHERE user AND date AND keyword` |
| Session storage | DynamoDB items | `sessions` SQL table |
| Future vector search | ❌ Not possible | ✅ `pgvector` extension ready |

---

## 7. Performance Characteristics

| Metric | Value |
|--------|-------|
| Average AI response time | 3 – 7 seconds |
| Database query time | < 100 ms |
| Cold start (Lambda) | Eliminated via EventBridge keep-warm |
| Max concurrent users supported | 100+ (Lambda auto-scales) |
| Message storage capacity | Unlimited (RDS scales via storage) |

---

## 8. Future Enhancements (Roadmap)

| Enhancement | Purpose | Est. Cost Impact |
|-------------|---------|-----------------|
| `pgvector` semantic search | Find messages by meaning, not just keywords | +$0/month (PostgreSQL extension) |
| Multi-AZ RDS | Database high availability, zero downtime | +$13/month |
| Lambda Provisioned Concurrency | Guaranteed zero cold starts | +$3/month |
| Cost alerts (CloudWatch Billing Alarm) | Alert if monthly spend exceeds threshold | $0 |
| Message threading support | Include Slack thread replies in search | Development effort only |

---

## 9. File Structure

```
slack-chatbot/
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI app entry point, Lambda handler
│   │   ├── routes.py        # All API endpoints
│   │   ├── db.py            # PostgreSQL connection pool
│   │   ├── models.py        # Request/response data models
│   │   ├── retrieval.py     # Message search + AI context building
│   │   ├── slack_handler.py # Real-time Slack event processing
│   │   ├── session.py       # OAuth session management
│   │   ├── bedrock_client.py# AWS Bedrock AI integration
│   │   ├── utils.py         # Token management, username extraction
│   │   └── constants.py     # All configuration constants
│   ├── Dockerfile           # Container image definition
│   └── requirements.txt     # Python dependencies
├── infra/
│   └── template.yml         # CloudFormation (all AWS resources)
├── frontend/
│   └── index.html           # Dashboard UI
└── .github/
    └── workflows/
        └── deploy.yml       # CI/CD pipeline
```

---

*This document prepared for internal technical review.*
*Pricing figures sourced from AWS Pricing Calculator (calculator.aws), April 2026.*
*Actual costs may vary ±15% based on exact usage patterns.*
