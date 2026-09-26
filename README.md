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

## Building it by hand in the AWS Console (no Terraform)

Everything below creates the exact same resources as `terraform apply`, click by click, in the
order each resource's dependencies force you into. Resource names match what Terraform would
have created, so the two approaches are drop-in equivalents — and so you can cross-reference
`README_exp.md`, which explains *why* each of these exists, while you click through this.

Fill in as you go: `<REGION>` (e.g. `us-east-1`), `<ACCOUNT_ID>` (12-digit, top-right of the
console), `<YOUR_IP>` (your own public IP, for security group rules), and `<KEY_PAIR>` (an
existing EC2 key pair in that region — EC2 → Key Pairs → Create key pair, if you don't have one).

### 1. S3 bucket — RCA reports + runbook

**S3 → Buckets → Create bucket**
- Bucket name: `autotriage-artifacts-<ACCOUNT_ID>` (must be globally unique account-wide)
- Region: `<REGION>`
- Block all public access: leave **on** (default)
- Default encryption: **SSE-S3** (`AES256`)
- Create bucket

Inside the new bucket: **Create folder** named `runbooks/`, then **Upload** your local
`runbooks/runbook.md` into it, so the object key ends up as `runbooks/runbook.md`.

### 2. DynamoDB table — audit log

**DynamoDB → Tables → Create table**
- Table name: `autotriage-audit-log`
- Partition key: `alarm_name` (String)
- Sort key: `timestamp` (String)
- Table settings: **Customize** → Read/write capacity: **On-demand**
- Create table

### 3. SNS topics — trigger + escalation

**SNS → Topics → Create topic** (×2, both type **Standard**):
- `autotriage-triage-trigger` — alarms publish here, the Lambda subscribes
- `autotriage-escalation` — the Lambda publishes here, a human subscribes

Open `autotriage-escalation` → **Create subscription** → Protocol: **Email** → Endpoint: your
address → Create subscription, then check your inbox and click **Confirm subscription**. No
messages will arrive until that's confirmed.

### 4. IAM role — EC2 instance

**IAM → Roles → Create role**
- Trusted entity type: **AWS service** → Use case: **EC2**
- Attach policies: `AmazonSSMManagedInstanceCore` and `CloudWatchAgentServerPolicy`
- Role name: `autotriage-ec2-role` → Create role

(Attaching this role to an instance auto-creates a matching instance profile — there's no
separate "instance profile" object to create by hand in the console the way Terraform has one.)

### 5. Security group

**EC2 → Security Groups → Create security group**
- Name: `autotriage-app-sg`, VPC: your default VPC
- Inbound rules: SSH (22) from `<YOUR_IP>/32`; Custom TCP (8000) from `<YOUR_IP>/32` (or
  `0.0.0.0/0` if you want to hit it from anywhere for the demo)
- Outbound rules: leave the default **All traffic → 0.0.0.0/0**
- Create security group

### 6. SSM documents — the only two remediations that can ever run

**Systems Manager → Documents → Owned by me → Create document → Command document**, twice:

- Name `autotriage-restart-container`, Target type `/AWS::EC2::Instance`, Document type
  **Command**. Switch the content editor to **YAML** and paste in the contents of
  `ssm/restart-container.yml` verbatim. Create document.
- Name `autotriage-prune-disk`, same settings, contents of `ssm/prune-disk.yml`.

### 7. Launch the EC2 instance

**EC2 → Instances → Launch instance**
- Name: `autotriage-app`
- AMI: **Amazon Linux 2023** (search "Amazon Linux 2023" in Quick Start)
- Instance type: `t3.micro`
- Key pair: `<KEY_PAIR>`
- Network settings → Select existing security group: `autotriage-app-sg`
- Advanced details → IAM instance profile: `autotriage-ec2-role`
- Advanced details → User data — paste this (fill in `<REGION>`):

  ```bash
  #!/bin/bash
  set -euxo pipefail
  exec > >(tee /var/log/autotriage-user-data.log) 2>&1

  dnf update -y
  dnf install -y docker unzip jq cronie
  systemctl enable --now docker
  systemctl enable --now crond

  if ! command -v aws >/dev/null 2>&1; then
    curl -sSL "https://awscliv2.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
    unzip -q /tmp/awscliv2.zip -d /tmp
    /tmp/aws/install
  fi

  curl -sSL "https://amazoncloudwatch-agent-<REGION>.s3.<REGION>.amazonaws.com/amazon_linux/amd64/latest/amazon-cloudwatch-agent.rpm" \
    -o /tmp/amazon-cloudwatch-agent.rpm
  rpm -U /tmp/amazon-cloudwatch-agent.rpm

  mkdir -p /opt/autotriage/app /var/log/autotriage
  chmod 777 /var/log/autotriage
  ```
- Launch instance, and note its **Instance ID** and **Public IPv4 address** from the instance
  list — you'll need both repeatedly below.

This user data only installs Docker, the AWS CLI, and the CloudWatch agent — unlike the
Terraform build, it doesn't bake the app's source into the launch config (there's no
`templatefile()` here to embed it, and pasting raw code into the console's user-data box gets
unwieldy fast). You'll copy the three app files up yourself in the next step.

### 8. Copy the app up and start it

From your own machine, with the repo checked out locally:

```bash
scp -i <path-to-KEY_PAIR>.pem app/main.py app/requirements.txt app/Dockerfile \
  ec2-user@<PUBLIC_IP>:/tmp/
ssh -i <path-to-KEY_PAIR>.pem ec2-user@<PUBLIC_IP>
```

Then, on the instance:

```bash
sudo mkdir -p /opt/autotriage/app
sudo mv /tmp/main.py /tmp/requirements.txt /tmp/Dockerfile /opt/autotriage/app/
cd /opt/autotriage/app
sudo docker build -t autotriage-app:latest .
sudo docker run -d --name autotriage-app --restart=no -p 8000:8000 \
  -v /var/log/autotriage:/var/log/autotriage \
  autotriage-app:latest
curl -s localhost:8000/health   # sanity check: {"status":"ok"}
```

### 9. Configure and start the CloudWatch agent

Still on the instance (fill in `<REGION>`):

```bash
sudo tee /opt/aws/amazon-cloudwatch-agent/etc/config.json > /dev/null <<'CWCONFIG'
{
  "agent": { "metrics_collection_interval": 60, "run_as_user": "root" },
  "metrics": {
    "namespace": "CWAgent",
    "append_dimensions": { "InstanceId": "${aws:InstanceId}" },
    "metrics_collected": {
      "disk": {
        "measurement": ["used_percent"],
        "resources": ["/"],
        "drop_device": true,
        "metrics_collection_interval": 60
      },
      "mem": {
        "measurement": ["mem_used_percent"],
        "metrics_collection_interval": 60
      }
    }
  },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/autotriage/app.log",
            "log_group_name": "/autotriage/app",
            "log_stream_name": "{instance_id}/app",
            "timestamp_format": "%Y-%m-%dT%H:%M:%SZ"
          }
        ]
      }
    }
  }
}
CWCONFIG

sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json
```

The `/autotriage/app` log group is created automatically the first time the agent ships a line
to it — no need to pre-create it, though you can (CloudWatch → Log groups → Create log group)
if you'd rather set its retention before any data lands.

### 10. Container heartbeat (the container-down alarm has nothing to watch without this)

Still on the instance:

```bash
sudo tee /etc/default/autotriage > /dev/null <<ENVFILE
CONTAINER_NAME=autotriage-app
AWS_REGION=<REGION>
ENVFILE

sudo tee /opt/autotriage/heartbeat.sh > /dev/null <<'HEARTBEAT'
#!/bin/bash
set -euo pipefail
. /etc/default/autotriage
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
RUNNING=$(docker ps -q -f "name=^$CONTAINER_NAME$" -f status=running)
if [ -n "$RUNNING" ]; then VALUE=1; else VALUE=0; fi
aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace AutoTriage \
  --metric-name ContainerRunning --dimensions InstanceId=$INSTANCE_ID --value $VALUE
HEARTBEAT
sudo chmod +x /opt/autotriage/heartbeat.sh

echo "* * * * * root /opt/autotriage/heartbeat.sh >> /var/log/autotriage-heartbeat.log 2>&1" \
  | sudo tee /etc/cron.d/autotriage-heartbeat > /dev/null
sudo chmod 644 /etc/cron.d/autotriage-heartbeat
```

Give it a minute, then confirm data is flowing: **CloudWatch → All metrics → AutoTriage** should
show `ContainerRunning`, and — once the CloudWatch agent has had a minute to report too —
**CloudWatch → All metrics → CWAgent** should show `disk_used_percent`.

### 11. Log metric filter + all three alarms

**CloudWatch → Log groups → `/autotriage/app` → Metric filters tab → Create metric filter**
- Filter pattern: `{ $.status_code = 500 }`
- Filter name: `autotriage-http-5xx`
- Metric namespace: `AutoTriage`, Metric name: `HTTP5xxCount`, Metric value: `1`, Default
  value: `0`
- Create metric filter

**CloudWatch → Alarms → Create alarm**, three times:

| | Namespace / metric | Dimensions | Statistic, period | Threshold | Evaluation periods | Missing data |
|---|---|---|---|---|---|---|
| `autotriage-http-5xx-rate` | `AutoTriage` / `HTTP5xxCount` | — | Sum, 1 min | `>` 5 | 1 | Not breaching |
| `autotriage-container-down` | `AutoTriage` / `ContainerRunning` | `InstanceId=<INSTANCE_ID>` | Minimum, 1 min | `<` 1 | 2 | **Breaching** |
| `autotriage-disk-high` | `CWAgent` / `disk_used_percent` | `InstanceId=<INSTANCE_ID>, path=/, fstype=xfs` | Average, 1 min | `>` 85 | 2 | Not breaching |

For each: under **Configure actions**, set the "In alarm" action to **Send a notification to an
SNS topic** → `autotriage-triage-trigger` (add the same for "OK", so you also see it clear).

Two things that trip people up here:
- The `disk_used_percent` metric only shows up in the metric picker once the agent has actually
  published a data point — if you don't see it yet, wait a minute and refresh.
- Its dimensions must match **exactly** what the agent emits. `drop_device: true` in the agent
  config (step 9) removes the `device` dimension the agent would otherwise add, leaving
  `InstanceId`, `path`, and `fstype` — confirm `fstype` really is `xfs` on your instance with
  `df -T /` if the alarm sits in `INSUFFICIENT_DATA` after data should have arrived; a
  mismatched dimension means the alarm is watching a metric that doesn't exist and it will never
  fire.
- `autotriage-container-down` treating missing data as **breaching** is deliberate — if the
  heartbeat cron stops reporting entirely (e.g. the whole instance is down, not just the
  container), that absence should itself count as "not running."

### 12. IAM role — triage Lambda

**IAM → Roles → Create role**
- Trusted entity type: **AWS service** → Use case: **Lambda**
- Attach policy: `AWSLambdaBasicExecutionRole`
- Role name: `autotriage-triage-lambda-role` → Create role

Open the new role → **Add permissions → Create inline policy → JSON tab**, paste this with every
`<...>` placeholder filled in, then name it `autotriage-triage-lambda-policy` and save:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadAppLogs",
      "Effect": "Allow",
      "Action": ["logs:StartQuery", "logs:GetQueryResults", "logs:StopQuery", "logs:DescribeLogGroups"],
      "Resource": "*"
    },
    {
      "Sid": "ReadMetricsAndAlarms",
      "Effect": "Allow",
      "Action": ["cloudwatch:GetMetricData", "cloudwatch:DescribeAlarms"],
      "Resource": "*"
    },
    {
      "Sid": "RunbookAndRca",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::autotriage-artifacts-<ACCOUNT_ID>/*"
    },
    {
      "Sid": "AuditLog",
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem", "dynamodb:Query"],
      "Resource": "arn:aws:dynamodb:<REGION>:<ACCOUNT_ID>:table/autotriage-audit-log"
    },
    {
      "Sid": "Escalate",
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "arn:aws:sns:<REGION>:<ACCOUNT_ID>:autotriage-escalation"
    },
    {
      "Sid": "InvokeClaude",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": [
        "arn:aws:bedrock:<REGION>::foundation-model/<BEDROCK_MODEL_ID>",
        "arn:aws:bedrock:<REGION>:<ACCOUNT_ID>:inference-profile/*"
      ]
    },
    {
      "Sid": "RunRemediation",
      "Effect": "Allow",
      "Action": ["ssm:SendCommand"],
      "Resource": [
        "arn:aws:ssm:<REGION>:<ACCOUNT_ID>:document/autotriage-restart-container",
        "arn:aws:ssm:<REGION>:<ACCOUNT_ID>:document/autotriage-prune-disk",
        "arn:aws:ec2:<REGION>:<ACCOUNT_ID>:instance/<INSTANCE_ID>"
      ]
    },
    {
      "Sid": "ReadRemediationResult",
      "Effect": "Allow",
      "Action": ["ssm:GetCommandInvocation"],
      "Resource": "*"
    }
  ]
}
```

This is the same policy `terraform/iam.tf` generates — the console just needs literal ARNs where
Terraform could reference other resources by name.

### 13. Package and create the Lambda function

Locally, zip the Lambda source (the console needs a `.zip`, not a folder):

```bash
cd lambda/triage
zip -r ../../triage_lambda.zip . -x "requirements.txt"
cd ../..
```

**Lambda → Functions → Create function**
- Author from scratch, name `autotriage-triage`, runtime **Python 3.12**
- Permissions → Use an existing role → `autotriage-triage-lambda-role`
- Create function

Then:
- **Code** tab → **Upload from** → **.zip file** → select `triage_lambda.zip` → Save
- **Runtime settings → Edit** → Handler: `handler.lambda_handler` → Save
- **Configuration → General configuration → Edit** → Memory: `256` MB, Timeout: `8` min `0` sec
  (480s — covers the Logs Insights query, the Bedrock call, up to 90s of SSM polling, and the
  120s post-action wait) → Save
- **Configuration → Environment variables → Edit**, add:

  | Key | Value |
  |---|---|
  | `LOG_GROUP_NAME` | `/autotriage/app` |
  | `INSTANCE_ID` | `<INSTANCE_ID>` |
  | `S3_BUCKET` | `autotriage-artifacts-<ACCOUNT_ID>` |
  | `RUNBOOK_KEY` | `runbooks/runbook.md` |
  | `DYNAMODB_TABLE` | `autotriage-audit-log` |
  | `BEDROCK_MODEL_ID` | `anthropic.claude-3-5-sonnet-20241022-v2:0` (or whichever you enabled) |
  | `ESCALATION_TOPIC_ARN` | `arn:aws:sns:<REGION>:<ACCOUNT_ID>:autotriage-escalation` |
  | `SSM_DOC_RESTART` | `autotriage-restart-container` |
  | `SSM_DOC_PRUNE` | `autotriage-prune-disk` |
  | `DRY_RUN` | `true` |
  | `POST_ACTION_WAIT_SECONDS` | `120` |

### 14. Wire the trigger topic to the Lambda

**Lambda → `autotriage-triage` → Add trigger → SNS → Topic: `autotriage-triage-trigger` → Add.**

This one click does two things Terraform does as two separate resources
(`aws_sns_topic_subscription` + `aws_lambda_permission`): it subscribes the function to the
topic, *and* it grants SNS the resource-based permission to actually invoke it. Both are
required; the console just bundles them.

### 15. Enable Bedrock model access

**Bedrock → Model access → Manage model access**, enable the Anthropic model matching
`BEDROCK_MODEL_ID` above, and save. This is an account/region-level, one-time grant — activation
can take a few minutes.

### 16. Test it

Same as the Terraform path (substitute `<PUBLIC_IP>` for `$APP`):

```bash
curl -s http://<PUBLIC_IP>:8000/health
curl -s -X POST http://<PUBLIC_IP>:8000/chaos/500
```

Then watch: **CloudWatch → Alarms** (fires, then clears), the **DynamoDB table** (one new item
per incident), the **S3 bucket** under `rca/<alarm-name>/...`, and the Lambda's own **CloudWatch
Logs** log group for the full trace of what it decided and why.

### Cleanup (console)

Delete in this order — later resources reference earlier ones' names/ARNs, so most of these
will simply refuse to delete out of order:

1. Lambda function `autotriage-triage` (this also removes its SNS trigger)
2. SNS subscriptions, then both topics (`autotriage-triage-trigger`, `autotriage-escalation`)
3. CloudWatch alarms (all three) and the `/autotriage/app` log group (deleting the log group also
   removes its metric filter)
4. SSM documents (`autotriage-restart-container`, `autotriage-prune-disk`)
5. EC2 instance (terminate `autotriage-app`), then the `autotriage-app-sg` security group
6. IAM roles `autotriage-ec2-role` and `autotriage-triage-lambda-role` (detach/delete their
   inline and managed policies first if the console asks)
7. DynamoDB table `autotriage-audit-log`
8. S3 bucket `autotriage-artifacts-<ACCOUNT_ID>` (empty it first — S3 won't delete a non-empty
   bucket)

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

## Cleanup (Terraform)

```bash
cd terraform
terraform destroy
```

(If you built it by hand instead, see "Cleanup (console)" above.)
