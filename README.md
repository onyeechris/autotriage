# AutoTriage

An AI on-call agent for a small Dockerized app on EC2: a CloudWatch alarm fires, a Lambda
gathers evidence (logs, metrics, runbook), asks Claude for a structured verdict, and either
auto-remediates through a tightly scoped policy gate or escalates to a human — logging every
decision and writing a short RCA.

```
EC2 (Docker FastAPI app + CloudWatch agent)
   │  logs + metrics
   ▼
CloudWatch Alarm ──► SNS ──► Lambda: triage-agent
                                 │ 1. gather context (logs, metrics, runbook)
                                 │ 2. Claude (Bedrock) → structured JSON verdict
                                 │ 3. policy gate (allowlist + confidence + risk)
                                 ├──► auto-fix via SSM Run Command ──► verify ──► escalate if not recovered
                                 └──► needs approval → SNS email
                                 ▼
                       DynamoDB (audit log) + S3 (RCA markdown)
```

## Repo layout

```
autotriage/
├── app/            # FastAPI demo app + Dockerfile (chaos endpoints)
├── lambda/triage/  # handler.py, policy.py, actions.py, context.py, prompts.py
├── ssm/            # restart-container.yml, prune-disk.yml (SSM Command documents)
├── runbooks/       # runbook.md — the only source of truth the LLM is told to trust
└── terraform/      # all AWS infra
```

## What each failure mode looks like

| Chaos endpoint | Alarm it trips | Why it's safe to fix | Auto-fix |
|---|---|---|---|
| `POST /chaos/500` | 5xx rate > 5/min | Stateless app, flag is in-memory only | `restart_container` |
| `POST /chaos/crash` | Container not running | Container ran with `--restart=no`, so it never comes back on its own | `restart_container` |
| `POST /chaos/disk` | Disk used % > 85 | Junk files match a known, deletable pattern (`/tmp/autotriage-chaos-junk-*`) | `prune_disk` |
| `POST /chaos/reset` | — | Clears the 500 flag and any chaos junk files directly, no alarm needed | — |

`GET /health` always returns `200` unless the 500 flag is active.

## Guardrails

- Lambda IAM only allows `ssm:SendCommand` on the two named SSM documents and this one tagged
  instance — never arbitrary shell.
- `DRY_RUN=true` by default (Terraform variable `dry_run`): the Lambda reasons, decides, and
  logs normally, but never actually sends the SSM command.
- At most one auto-remediation per alarm per 30 minutes (`policy.is_rate_limited`), so a
  persistent problem gets escalated instead of retried in a loop.
- Invalid or unparseable LLM output always escalates — the Lambda never guesses (`policy.validate_verdict`).
- Auto-remediation requires `confidence >= 0.8` **and** `risk == "low"`; anything else routes to
  human approval via the escalation SNS topic/email.

## Deploying

Prerequisites: Terraform >= 1.5, AWS credentials for the target account, an existing EC2 key
pair in the target region, and Bedrock model access enabled for the model in `bedrock_model_id`
(Bedrock model access is granted per-account/region in the Bedrock console before first use).

```bash
cd terraform
terraform init
terraform apply \
  -var="key_name=<your-ec2-key-pair-name>" \
  -var="alert_email=<you@example.com>" \
  -var="ssh_ingress_cidr=<your-ip>/32"
```

`dry_run` defaults to `true`. Flip it once you've watched a full dry-run cycle end to end:

```bash
terraform apply -var="dry_run=false" ... # plus the vars above
```

The instance takes a couple of minutes to finish `user_data` (installing Docker, the CloudWatch
agent, and building/starting the app container) before `/health` responds.

## Triggering each failure mode

```bash
APP=$(terraform -chdir=terraform output -raw app_base_url)

curl -s "$APP/health"           # baseline: 200
curl -s -X POST "$APP/chaos/500"    # → 5xx alarm within ~1-2 min
curl -s -X POST "$APP/chaos/crash"  # → container-down alarm within ~2 min
curl -s -X POST "$APP/chaos/disk"   # → disk alarm within ~2-4 min
curl -s -X POST "$APP/chaos/reset"  # manual reset, no alarm
```

Watch it end to end:
- CloudWatch → Alarms, to see the alarm fire and (after the fix) clear
- DynamoDB table (`terraform output audit_log_table`) for the decision trail
- S3 bucket (`terraform output artifacts_bucket`), under `rca/<alarm-name>/...`, for the RCA markdown
- CloudWatch Logs → Lambda's log group, for the full triage trace

## Sample RCA (from a dry run)

```markdown
# RCA: autotriage-http-5xx-rate

- Incident ID: 3f2c1a8e-...
- Time: 2026-09-25T18:04:12Z
- Alarm reason: Threshold Crossed: 1 datapoint [7.0] was greater than the threshold (5.0).
- Decision: auto (confidence and risk within policy for automatic remediation)
- Recovered: None
- Dry run: True

## Verdict
- Summary: The app is returning 500 for every request, including /health, with no CPU/disk anomaly.
- Root cause: The /chaos/500 endpoint was triggered, setting the app's in-memory force_500 flag.
- Confidence: 0.94
- Action: restart_container
- Risk: low
- Evidence:
  - "status_code": 500 on GET /health, repeated across all recent log lines
  - body: {"error": "chaos_500_active"} matches the known runbook signature
  - CPU and disk metrics are within normal range

## Action result
{
  "status": "dry_run",
  "document_name": "autotriage-restart-container",
  "instance_id": "i-0123456789abcdef0",
  "note": "DRY_RUN=true, no SSM command was sent"
}
```

## What I'd add next

- **Slack approval button** for medium-risk actions instead of a plain escalation email, so a
  human can approve/deny from the alert itself.
- **Search past incidents before calling the LLM**: query the DynamoDB audit log for this alarm
  first and feed prior verdicts/outcomes into the prompt, so repeat incidents get diagnosed
  faster and more consistently.
- **Weekly "permanent fix" report**: scan the audit log for alarms that keep recurring despite
  successful auto-remediation, and flag those for an engineering fix rather than continuing to
  paper over them with automation.
- **VPC-scoped Lambda + direct health check** as a second recovery signal alongside the
  CloudWatch alarm re-check, to catch cases where the alarm's evaluation window lags real recovery.
- Move the Bedrock model id to an inference profile ARN and add a CloudWatch alarm on the
  triage Lambda's own error rate, so a broken triage pipeline pages a human too.

## Cleanup

```bash
cd terraform
terraform destroy
```
