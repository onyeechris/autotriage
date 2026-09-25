# Project: AutoTriage — AI on-call agent for a Dockerized app on EC2

## Context for this chat
I'm a Senior DevOps Engineer preparing for an IgniteTech "Senior Cloud Operations / SRE (AI-DNA)" interview.
This is a practice project to show AI-native operations: alarm → AI triage → safe auto-remediation → RCA.
Goal: build it end to end in ~60 minutes using Claude Code, AI-first (AI drafts, I review/test/fix).

## Scenario
A small FastAPI app on EC2 is broken on purpose (chaos endpoints). A CloudWatch alarm fires, and a Lambda agent
gathers evidence, asks Claude what's wrong, then either fixes it safely or asks a human, and writes a short RCA.

## Architecture
```
EC2 (Docker FastAPI app + CloudWatch agent)
   │  logs + metrics
   ▼
CloudWatch Alarm ──► SNS ──► Lambda: triage-agent
                                 │ 1. gather context (logs, metrics, runbook)
                                 │ 2. LLM call → structured JSON verdict
                                 │ 3. policy gate (allowlist + confidence + risk)
                                 ├──► auto-fix via SSM Run Command ──► verify ──► escalate if not recovered
                                 └──► needs approval → Slack/email
                                 ▼
                       DynamoDB (audit log) + S3 (RCA markdown)
```

## Components

### 1. Demo app (FastAPI + Uvicorn in Docker, t3.micro)
```
app/
├── main.py
├── requirements.txt   # fastapi, uvicorn[standard]
└── Dockerfile
```
- `GET /health` → 200
- `POST /chaos/500` → sets a flag so every request returns 500
- `POST /chaos/crash` → `os._exit(1)`, container dies
- `POST /chaos/disk` → writes junk to `/tmp` until disk ~90%
- `POST /chaos/reset` → clears the 500 flag
- JSON logs, one line per request with `status_code` and `path`
- Run container with `--restart=no` so "container down" alarm and `restart_container` fix are meaningful
- 5xx metric filter: `{ $.status_code >= 500 }`

### 2. Alarms (→ SNS)
- 5xx count > 5 in 1 min (metric filter on app logs)
- Container not running (custom metric from a cron script)
- Disk usage > 85% (CloudWatch agent)

### 3. Triage Lambda (Python)
- **Gather:** alarm payload, last 15 min of logs (Logs Insights), CPU/disk metrics, `runbook.md` from S3
- **Call Claude** (Bedrock or Anthropic API), must return JSON:
  ```json
  {"summary": "", "root_cause": "", "confidence": 0.0,
   "action": "restart_container|prune_disk|none",
   "risk": "low|medium|high", "evidence": []}
  ```
- **Policy gate:** auto-run only if action is allowlisted AND `confidence >= 0.8` AND `risk == low`; else request human approval
- **Execute** via SSM Run Command → wait 2 min → re-check alarm → escalate if not recovered
- **Log** every decision to DynamoDB; write RCA markdown to S3

### 4. Guardrails
- Lambda IAM: only `ssm:SendCommand` on tagged instances, only the 2 pre-defined SSM documents; never arbitrary shell
- `DRY_RUN=true` env var
- Max 1 auto-remediation per alarm per 30 min (no fix loops)
- Invalid LLM JSON → escalate, never guess

## 60-minute plan
| Time | Task |
|---|---|
| 0–10 | Terraform: EC2, IAM, CW agent, alarms, SNS, Lambda, DynamoDB, S3 |
| 10–25 | FastAPI app + chaos endpoints |
| 25–45 | Triage Lambda: context, LLM call, policy gate, SSM actions |
| 45–55 | Trigger each failure mode, confirm fix + RCA |
| 55–60 | README: diagram, sample RCA, "what I'd add next" |

## Repo layout
```
autotriage/
├── app/            # FastAPI app + Dockerfile
├── lambda/triage/  # handler.py, policy.py, actions.py, prompts.py
├── ssm/            # restart-container.yml, prune-disk.yml
├── runbooks/runbook.md
├── terraform/
└── README.md
```

## First prompt for Claude Code
> Build a project called autotriage. Terraform for AWS (us-east-1): a t3.micro EC2 with Docker and the CloudWatch agent running a FastAPI app (Uvicorn, JSON structured logs) with endpoints GET /health, POST /chaos/500, /chaos/crash, /chaos/disk, /chaos/reset. Run the container with --restart=no. Create CloudWatch alarms for the 5xx rate (metric filter on status_code >= 500), container down, and disk > 85%, all publishing to SNS. An SNS-triggered Python Lambda gathers the last 15 minutes of logs and metrics plus a runbook from S3, then calls Claude on Bedrock for a JSON triage verdict. A policy gate allows only restart_container or prune_disk via pre-defined SSM documents when confidence >= 0.8 and risk is low. The Lambda then verifies recovery, escalates via SNS email otherwise, and logs to DynamoDB and S3. Use least-privilege IAM and a DRY_RUN flag. Write the plan first, then implement step by step.

## Stretch goals
- Slack approval button for medium-risk actions
- Search past incidents in the audit log before calling the LLM (learn from history)
- Weekly "permanent fix" tracking: repeated incidents get flagged for an engineering fix

## Cleanup
Run `terraform destroy` when done.