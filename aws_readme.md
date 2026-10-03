# AutoTriage — AWS setup guide

This guide rebuilds the whole AWS side of AutoTriage: the EC2 demo app, the CloudWatch
alarms, the SNS topics, the triage Lambda, the DynamoDB audit log, the S3 bucket for runbooks
and RCAs, and the SSM documents that do the actual remediation.

There are three ways to build it. They create the same resources with the same names, so you can
switch between them or mix them (for example, Terraform for the Lambda and the console for a
one-off instance):

| Path | Best for | Where it is |
|---|---|---|
| **A. Terraform** | Repeatable, reviewable, one command to create and destroy | [Part A](#part-a--terraform) |
| **B. AWS CLI** | Working from a terminal without Terraform state | [Part B](#part-b--aws-cli) |
| **C. AWS Console** | Clicking through each step to learn how the pieces connect | [Part C](#part-c--aws-console) |

After any of them, verify with [Part D](#part-d--verify-and-test) and tear down with
[Part E](#part-e--cleanup).

> **Read this first — `dry_run` now defaults to `false`.** The Lambda will restart or prune the
> instance on its own. To rehearse without touching anything, deploy with `-var dry_run=true`
> (Terraform) or set `DRY_RUN=true` (CLI/console) and flip it once you trust the decisions.

---

## Prerequisites

- An AWS account and an IAM user or role with rights to create the resources in this guide
  (the simplest route is an admin role in a sandbox account).
- AWS CLI v2 configured: `aws configure`, then `aws sts get-caller-identity` should print your account.
- An EC2 key pair in the target region (EC2 → Key Pairs, or
  `aws ec2 create-key-pair --key-name autotriage-app-key --query KeyMaterial --output text > autotriage-app-key.pem && chmod 400 autotriage-app-key.pem`).
- An [Anthropic API key](https://console.anthropic.com/).
- Terraform ≥ 1.5 (Part A only), `zip`, `curl`, and Python 3 (Parts B and C).
- Your public IP, used to restrict SSH and port 8000. Get it with `curl -s https://checkip.amazonaws.com`.

### Shared variables

Used by every path. Run these in the shell you'll use for the whole setup. Only `ALERT_EMAIL` and `KEY_NAME`
need your input.

```bash
export AWS_PAGER=""
export REGION=us-east-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ALERT_EMAIL=you@example.com          # receives alarm and escalation emails
export KEY_NAME=autotriage-app-key          # existing EC2 key pair in $REGION
export MY_CIDR=$(curl -s https://checkip.amazonaws.com | tr -d '\n')/32
export BUCKET=autotriage-artifacts-$ACCOUNT_ID
export ACCOUNT_ARN_PREFIX=arn:aws:
```

Resource names used everywhere (identical in all three paths):

| Resource | Name |
|---|---|
| S3 bucket | `autotriage-artifacts-<ACCOUNT_ID>` |
| DynamoDB table | `autotriage-audit-log` |
| SNS topics | `autotriage-triage-trigger` (alarms → Lambda), `autotriage-escalation` (Lambda → humans) |
| IAM roles | `autotriage-ec2-role` + instance profile `autotriage-ec2-profile`, `autotriage-triage-lambda-role` |
| Security group | `autotriage-app-sg` |
| SSM documents | `autotriage-restart-container`, `autotriage-prune-disk` |
| SSM parameter | `/autotriage/anthropic-api-key` (SecureString) |
| Log group | `/autotriage/app` (14-day retention) |
| Metric filter | `autotriage-http-5xx` → `AutoTriage/HTTP5xxCount` |
| Alarms | `autotriage-http-5xx`, `autotriage-container-down`, `autotriage-disk-high` |
| Lambda | `autotriage-triage` (Python 3.12, 256 MB, 600 s timeout) |
| EC2 instance | `autotriage-app` (`t3.micro`, Amazon Linux 2023) |

---

## Part A — Terraform

All infrastructure is in `terraform/`. The Lambda source (`lambda/triage/`), the app (`app/`), the
runbook (`runbooks/`), and the SSM documents (`ssm/`) are read from the repo root at plan time.

### A1. Create and check

```bash
cd terraform
terraform init
terraform validate

terraform apply \
  -var="key_name=$KEY_NAME" \
  -var="alert_email=$ALERT_EMAIL" \
  -var="ssh_ingress_cidr=$MY_CIDR" \
  -var="app_ingress_cidr=$MY_CIDR" \
  -var="dry_run=true"          # rehearsal first; set false in A4 once you trust it
```

Terraform creates the SSM parameter with a placeholder value and deliberately ignores drift on it,
so the real key is never stored in Terraform state.

### A2. Set the Anthropic API key

```bash
read -rs ANTHROPIC_API_KEY && echo
aws ssm put-parameter \
  --name "$(terraform output -raw anthropic_api_key_parameter_name)" \
  --type SecureString --value "$ANTHROPIC_API_KEY" --overwrite
unset ANTHROPIC_API_KEY
```

### A3. Confirm the two email subscriptions

Terraform creates two email subscriptions. **Nothing is delivered until both are confirmed**: check
`$ALERT_EMAIL` for two "AWS Notification - Subscription Confirmation" emails and click the link in each.

- `autotriage-triage-trigger` — every alarm ALARM/OK transition (the "something happened" emails)
- `autotriage-escalation` — approvals and unresolved incidents from the Lambda

Check status any time:

```bash
for T in autotriage-triage-trigger autotriage-escalation; do
  aws sns list-subscriptions-by-topic --topic-arn arn:aws:sns:$REGION:$ACCOUNT_ID:$T \
    --query 'Subscriptions[?Protocol==`email`].[Endpoint,SubscriptionArn]' --output table
done
```

A confirmed subscription shows a full ARN; a pending one shows `PendingConfirmation`.

### A4. Turn on real remediation (after a dry-run rehearsal)

```bash
cd terraform
terraform apply -var="dry_run=false" -var="key_name=$KEY_NAME" -var="alert_email=$ALERT_EMAIL" \
  -var="ssh_ingress_cidr=$MY_CIDR" -var="app_ingress_cidr=$MY_CIDR"
```

### A5. Wait for the app

`user_data` installs Docker, the CloudWatch agent, and builds the app. Allow about 3–5 minutes, then:

```bash
curl -s "$(terraform output -raw app_base_url)/health"    # {"status":"ok"}
```

### Terraform variables that matter

| Variable | Default | Meaning |
|---|---|---|
| `key_name` | — (required) | Existing EC2 key pair |
| `alert_email` | — (required) | Receives alarm and escalation emails |
| `dry_run` | `false` | `true`: decide and log, but never send the SSM command |
| `rate_limit_window_minutes` | `5` | Minimum minutes between real auto-remediations per alarm. **5 is a test setting; use 30 for production.** |
| `post_action_wait_seconds` | `360` | Wait after a fix before checking recovery. Log-based alarms take about 4 minutes to clear. |
| `anthropic_model` | `claude-haiku-4-5-20251001` | Model the Lambda calls |
| `ssh_ingress_cidr` / `app_ingress_cidr` | `0.0.0.0/0` | Restrict to `$MY_CIDR` |
| `instance_type` | `t3.micro` | EC2 size |

---

## Part B — AWS CLI

Every command below is plain AWS CLI, run from the repo root. Run the steps **in order**, because
later steps reference IDs and ARNs created earlier.

### B1. Lookups

```bash
export VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)
export SUBNET_ID=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC_ID \
  --query 'Subnets[0].SubnetId' --output text)
export AMI_ID=$(aws ssm get-parameter \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)
export ANTHROPIC_MODEL=claude-haiku-4-5-20251001
```

### B2. S3 bucket and runbook

```bash
if [ "$REGION" = "us-east-1" ]; then
  aws s3api create-bucket --bucket $BUCKET --region $REGION
else
  aws s3api create-bucket --bucket $BUCKET --region $REGION \
    --create-bucket-configuration LocationConstraint=$REGION
fi

aws s3api put-public-access-block --bucket $BUCKET --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-encryption --bucket $BUCKET --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3 cp runbooks/runbook.md s3://$BUCKET/runbooks/runbook.md --content-type text/markdown
```

### B3. DynamoDB audit log

```bash
aws dynamodb create-table --table-name autotriage-audit-log \
  --attribute-definitions AttributeName=alarm_name,AttributeType=S AttributeName=timestamp,AttributeType=S \
  --key-schema AttributeName=alarm_name,KeyType=HASH AttributeName=timestamp,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST
aws dynamodb wait table-exists --table-name autotriage-audit-log
```

### B4. SNS topics and email subscriptions

```bash
export TRIGGER_TOPIC_ARN=$(aws sns create-topic --name autotriage-triage-trigger --query TopicArn --output text)
export ESCALATION_TOPIC_ARN=$(aws sns create-topic --name autotriage-escalation --query TopicArn --output text)

aws sns subscribe --topic-arn $TRIGGER_TOPIC_ARN    --protocol email --notification-endpoint $ALERT_EMAIL
aws sns subscribe --topic-arn $ESCALATION_TOPIC_ARN --protocol email --notification-endpoint $ALERT_EMAIL
```

Confirm both from the emails you receive (see A3 for the check command). The Lambda subscription
to the trigger topic is added in B10.

### B5. IAM — EC2 instance role and profile

```bash
cat > ec2-trust.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF

aws iam create-role --role-name autotriage-ec2-role --assume-role-policy-document file://ec2-trust.json
aws iam attach-role-policy --role-name autotriage-ec2-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam attach-role-policy --role-name autotriage-ec2-role \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy

aws iam create-instance-profile --instance-profile-name autotriage-ec2-profile
aws iam add-role-to-instance-profile --instance-profile-name autotriage-ec2-profile \
  --role-name autotriage-ec2-role
```

### B6. Security group

```bash
export SG_ID=$(aws ec2 create-security-group --group-name autotriage-app-sg \
  --description "AutoTriage demo app instance" --vpc-id $VPC_ID --query GroupId --output text)

aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 22   --cidr $MY_CIDR
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 8000 --cidr $MY_CIDR
```

Outbound stays at the default `All traffic → 0.0.0.0/0`.

### B7. SSM remediation documents

```bash
aws ssm create-document --name autotriage-restart-container --document-type Command \
  --document-format YAML --content file://ssm/restart-container.yml
aws ssm create-document --name autotriage-prune-disk --document-type Command \
  --document-format YAML --content file://ssm/prune-disk.yml
```

### B8. Log group, metric filter, and the API key

```bash
aws logs create-log-group --log-group-name /autotriage/app
aws logs put-retention-policy --log-group-name /autotriage/app --retention-in-days 14

aws logs put-metric-filter --log-group-name /autotriage/app --filter-name autotriage-http-5xx \
  --filter-pattern '{ $.status_code = 500 }' \
  --metric-transformations metricName=HTTP5xxCount,metricNamespace=AutoTriage,metricValue=1,defaultValue=0

read -rs ANTHROPIC_API_KEY && echo
aws ssm put-parameter --name /autotriage/anthropic-api-key --type SecureString \
  --value "$ANTHROPIC_API_KEY" --overwrite
unset ANTHROPIC_API_KEY
```

### B9. Launch the EC2 instance

Render the same user-data Terraform uses (`terraform/user_data.sh.tpl`) with a short script. Check the rendered file
with `bash -n` before using it:

```bash
cat > render_user_data.py <<'EOF'
import base64, sys
REGION, LOG_GROUP, CONTAINER = sys.argv[1], sys.argv[2], sys.argv[3]
b64 = lambda p: base64.b64encode(open(p, "rb").read()).decode()
t = open("terraform/user_data.sh.tpl").read()
for k, v in {
    "${container_name}": CONTAINER,
    "${aws_region}": REGION,
    "${log_group_name}": LOG_GROUP,
    "${app_main_py_b64}": b64("app/main.py"),
    "${app_requirements_txt_b64}": b64("app/requirements.txt"),
    "${app_dockerfile_b64}": b64("app/Dockerfile"),
    "$${": "${",
}.items():
    t = t.replace(k, v)
sys.stdout.write(t)
EOF

python3 render_user_data.py $REGION /autotriage/app autotriage-app > user-data.sh
bash -n user-data.sh && echo "user-data OK"

sleep 10   # IAM instance profile propagation
export INSTANCE_ID=$(aws ec2 run-instances --image-id $AMI_ID --instance-type t3.micro \
  --key-name $KEY_NAME --subnet-id $SUBNET_ID --security-group-ids $SG_ID \
  --iam-instance-profile Name=autotriage-ec2-profile \
  --user-data file://user-data.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=autotriage-app},{Key=Project,Value=autotriage},{Key=Role,Value=autotriage-app}]' \
  --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --instance-ids $INSTANCE_ID
export PUBLIC_IP=$(aws ec2 describe-instances --instance-ids $INSTANCE_ID \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "instance $INSTANCE_ID at $PUBLIC_IP"
```

Give it 3–5 minutes for `user_data` to finish before `/health` responds.

### B10. Lambda role and policy

```bash
cat > lambda-trust.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
aws iam create-role --role-name autotriage-triage-lambda-role --assume-role-policy-document file://lambda-trust.json
aws iam attach-role-policy --role-name autotriage-triage-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

cat > lambda-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid":"ReadAppLogs","Effect":"Allow","Action":["logs:StartQuery","logs:GetQueryResults","logs:StopQuery","logs:DescribeLogGroups"],"Resource":"*"},
    {"Sid":"ReadMetricsAndAlarms","Effect":"Allow","Action":["cloudwatch:GetMetricData","cloudwatch:DescribeAlarms"],"Resource":"*"},
    {"Sid":"RunbookAndRca","Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],"Resource":"arn:aws:s3:::$BUCKET/*"},
    {"Sid":"AuditLog","Effect":"Allow","Action":["dynamodb:PutItem","dynamodb:Query"],"Resource":"arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/autotriage-audit-log"},
    {"Sid":"Escalate","Effect":"Allow","Action":"sns:Publish","Resource":"$ESCALATION_TOPIC_ARN"},
    {"Sid":"ReadAnthropicApiKey","Effect":"Allow","Action":["ssm:GetParameter"],"Resource":"arn:aws:ssm:$REGION:$ACCOUNT_ID:parameter/autotriage/anthropic-api-key"},
    {"Sid":"DecryptAnthropicApiKey","Effect":"Allow","Action":["kms:Decrypt"],"Resource":"*"},
    {"Sid":"RunRemediation","Effect":"Allow","Action":["ssm:SendCommand"],"Resource":[
      "arn:aws:ssm:$REGION:$ACCOUNT_ID:document/autotriage-restart-container",
      "arn:aws:ssm:$REGION:$ACCOUNT_ID:document/autotriage-prune-disk",
      "arn:aws:ec2:$REGION:$ACCOUNT_ID:instance/$INSTANCE_ID"]},
    {"Sid":"ReadRemediationResult","Effect":"Allow","Action":["ssm:GetCommandInvocation"],"Resource":"*"}
  ]
}
EOF
aws iam put-role-policy --role-name autotriage-triage-lambda-role \
  --policy-name autotriage-triage-lambda-policy --policy-document file://lambda-policy.json
export LAMBDA_ROLE_ARN=$(aws iam get-role --role-name autotriage-triage-lambda-role --query Role.Arn --output text)
sleep 10   # IAM propagation
```

### B11. Package and create the Lambda

```bash
(cd lambda/triage && zip -qr ../../triage_lambda.zip . -x requirements.txt README.md '__pycache__/*')

cat > lambda-env.json <<EOF
{"Variables": {
  "LOG_GROUP_NAME": "/autotriage/app",
  "INSTANCE_ID": "$INSTANCE_ID",
  "S3_BUCKET": "$BUCKET",
  "RUNBOOK_KEY": "runbooks/runbook.md",
  "DYNAMODB_TABLE": "autotriage-audit-log",
  "ANTHROPIC_MODEL": "$ANTHROPIC_MODEL",
  "ANTHROPIC_API_KEY_PARAM": "/autotriage/anthropic-api-key",
  "ESCALATION_TOPIC_ARN": "$ESCALATION_TOPIC_ARN",
  "SSM_DOC_RESTART": "autotriage-restart-container",
  "SSM_DOC_PRUNE": "autotriage-prune-disk",
  "DRY_RUN": "true",
  "POST_ACTION_WAIT_SECONDS": "360",
  "RATE_LIMIT_WINDOW_MINUTES": "5"
}}
EOF

export LAMBDA_ARN=$(aws lambda create-function --function-name autotriage-triage \
  --runtime python3.12 --role $LAMBDA_ROLE_ARN --handler handler.lambda_handler \
  --zip-file fileb://triage_lambda.zip --memory-size 256 --timeout 600 \
  --environment file://lambda-env.json --query FunctionArn --output text)
aws lambda wait function-active-v2 --function-name autotriage-triage
```

`DRY_RUN` is `"true"` in this file on purpose. Change it to `"false"` in B14 after a rehearsal.
`RATE_LIMIT_WINDOW_MINUTES` is `5` for testing; use `30` for production.

### B12. Wire the trigger topic to the Lambda

```bash
aws lambda add-permission --function-name autotriage-triage --statement-id AllowSNSInvoke \
  --action lambda:InvokeFunction --principal sns.amazonaws.com --source-arn $TRIGGER_TOPIC_ARN
aws sns subscribe --topic-arn $TRIGGER_TOPIC_ARN --protocol lambda --notification-endpoint $LAMBDA_ARN
```

The permission and the subscription are two separate resources. Missing the permission means SNS
delivers messages that the Lambda silently refuses.

### B13. The three alarms

```bash
ALARM_ACTIONS="--alarm-actions $TRIGGER_TOPIC_ARN --ok-actions $TRIGGER_TOPIC_ARN"

aws cloudwatch put-metric-alarm --alarm-name autotriage-http-5xx \
  --alarm-description "More than 5 HTTP 5xx responses in 1 minute" \
  --namespace AutoTriage --metric-name HTTP5xxCount --statistic Sum --period 60 \
  --evaluation-periods 1 --threshold 5 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching $ALARM_ACTIONS

aws cloudwatch put-metric-alarm --alarm-name autotriage-container-down \
  --alarm-description "The demo app container is not running" \
  --namespace AutoTriage --metric-name ContainerRunning \
  --dimensions Name=InstanceId,Value=$INSTANCE_ID \
  --statistic Minimum --period 60 --evaluation-periods 2 --threshold 1 \
  --comparison-operator LessThanThreshold --treat-missing-data breaching $ALARM_ACTIONS

aws cloudwatch put-metric-alarm --alarm-name autotriage-disk-high \
  --alarm-description "Root volume disk usage above 85%" \
  --namespace CWAgent --metric-name disk_used_percent \
  --dimensions Name=InstanceId,Value=$INSTANCE_ID Name=path,Value=/ Name=fstype,Value=xfs \
  --statistic Average --period 60 --evaluation-periods 2 --threshold 85 \
  --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching $ALARM_ACTIONS
```

The `container-down` and `disk-high` alarms need a few minutes of metric data before they stop
showing `INSUFFICIENT_DATA`: the heartbeat and the CloudWatch agent both report once a minute.

### B14. Go live (after a dry-run rehearsal)

```bash
aws lambda update-function-configuration --function-name autotriage-triage \
  --environment "$(aws lambda get-function-configuration --function-name autotriage-triage \
     --query 'Environment' --output json | python3 -c 'import json,sys; e=json.load(sys.stdin); e["Variables"]["DRY_RUN"]="false"; print(json.dumps(e))')"
```

---

## Part C — AWS Console

Same resources, clicked through. Fill in `<REGION>`, `<ACCOUNT_ID>`, `<INSTANCE_ID>` (after step 7),
and `<YOUR_IP>` as you go. Console steps are in the same order as the CLI steps above.

### C1. S3 bucket

**S3 → Create bucket**: name `autotriage-artifacts-<ACCOUNT_ID>`, region `<REGION>`, keep **Block all public access**
on, default encryption **SSE-S3**. Create. Then create a folder `runbooks/` and upload `runbooks/runbook.md`.

### C2. DynamoDB table

**DynamoDB → Create table**: name `autotriage-audit-log`, partition key `alarm_name` (String), sort key
`timestamp` (String), capacity **On-demand**. Create.

### C3. SNS topics and email

**SNS → Topics → Create topic** (Standard), twice: `autotriage-triage-trigger` and `autotriage-escalation`.

Open **each** topic → **Create subscription** → Protocol **Email** → your address. Confirm both from your inbox.

### C4. IAM role for EC2

**IAM → Roles → Create role** → **AWS service** → **EC2**. Attach `AmazonSSMManagedInstanceCore` and
`CloudWatchAgentServerPolicy`. Name `autotriage-ec2-role`. Create.

The console creates an instance profile with the same name automatically (`autotriage-ec2-role`).
Terraform and the CLI name it `autotriage-ec2-profile`. Either works; the instance just needs *a* profile
with that role attached.

### C5. Security group

**EC2 → Security groups → Create security group**: name `autotriage-app-sg`, default VPC. Inbound: SSH 22 and
Custom TCP 8000, both from `<YOUR_IP>/32`. Outbound: default. Create.

### C6. SSM documents

**Systems Manager → Documents → Create command document** (twice):
- `autotriage-restart-container` — paste `ssm/restart-container.yml` into the **YAML** editor.
- `autotriage-prune-disk` — paste `ssm/prune-disk.yml`.

### C7. Launch the instance

**EC2 → Launch instance**:
- Name `autotriage-app`; AMI **Amazon Linux 2023**; type `t3.micro`; key pair `<KEY_PAIR>`
- Network: existing security group `autotriage-app-sg`
- Advanced → IAM instance profile `autotriage-ec2-role`
- Advanced → User data: paste the output of `python3 render_user_data.py <REGION> /autotriage/app autotriage-app`
  (the script is in B9). The console accepts up to 16 KB; the rendered script is about 8 KB.

Note the **Instance ID** and **Public IPv4**.

### C8. Log group, metric filter, API key

**CloudWatch → Log groups → Create log group**: `/autotriage/app`, retention **14 days**.

Same log group → **Metric filters → Create metric filter**: pattern `{ $.status_code = 500 }`, metric
namespace `AutoTriage`, metric name `HTTP5xxCount`, value `1`, default `0`, filter name `autotriage-http-5xx`.

**Systems Manager → Parameter Store → Create parameter**: `/autotriage/anthropic-api-key`, type
**SecureString**, your Anthropic key.

### C9. Lambda role

**IAM → Roles → Create role** → **AWS service** → **Lambda**. Attach `AWSLambdaBasicExecutionRole`.
Name `autotriage-triage-lambda-role`. Then **Add permissions → Create inline policy → JSON**, paste the
policy from B10 with the variables filled in, and name it `autotriage-triage-lambda-policy`.

### C10. Lambda function

Build the zip on your machine (same command as B11), then:

**Lambda → Create function** → author from scratch, `autotriage-triage`, **Python 3.12**, use existing role
`autotriage-triage-lambda-role`.

- **Code → Upload from .zip** → `triage_lambda.zip`. Runtime settings → handler `handler.lambda_handler`.
- **Configuration → General** → memory **256 MB**, timeout **10 min 0 sec** (600 s).
- **Configuration → Environment variables** — same values as `lambda-env.json` in B11:

| Key | Value |
|---|---|
| `LOG_GROUP_NAME` | `/autotriage/app` |
| `INSTANCE_ID` | `<INSTANCE_ID>` |
| `S3_BUCKET` | `autotriage-artifacts-<ACCOUNT_ID>` |
| `RUNBOOK_KEY` | `runbooks/runbook.md` |
| `DYNAMODB_TABLE` | `autotriage-audit-log` |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` |
| `ANTHROPIC_API_KEY_PARAM` | `/autotriage/anthropic-api-key` |
| `ESCALATION_TOPIC_ARN` | `arn:aws:sns:<REGION>:<ACCOUNT_ID>:autotriage-escalation` |
| `SSM_DOC_RESTART` | `autotriage-restart-container` |
| `SSM_DOC_PRUNE` | `autotriage-prune-disk` |
| `DRY_RUN` | `true` (set `false` in C13) |
| `POST_ACTION_WAIT_SECONDS` | `360` |
| `RATE_LIMIT_WINDOW_MINUTES` | `5` (test; `30` for production) |

### C11. Connect the trigger topic

**Lambda → `autotriage-triage` → Add trigger → SNS** → topic `autotriage-triage-trigger` → Add.
This creates both the subscription and the SNS invoke permission, which Terraform and the CLI do as two steps.

### C12. Alarms

**CloudWatch → Alarms → Create alarm**, three times. For each, under **Configure actions**, add
**In alarm** and **OK** actions → SNS topic `autotriage-triage-trigger`.

| Alarm | Metric | Dimensions | Statistic / period | Threshold | Evaluation periods | Missing data |
|---|---|---|---|---|---|---|
| `autotriage-http-5xx` | `AutoTriage` / `HTTP5xxCount` | — | Sum / 1 min | `>` 5 | 1 | Not breaching |
| `autotriage-container-down` | `AutoTriage` / `ContainerRunning` | `InstanceId` | Minimum / 1 min | `<` 1 | 2 | **Breaching** |
| `autotriage-disk-high` | `CWAgent` / `disk_used_percent` | `InstanceId`, `path=/`, `fstype=xfs` | Average / 1 min | `>` 85 | 2 | Not breaching |

Check `fstype` on the instance with `df -T /` (the root filesystem should show `xfs`). If the disk alarm
sits in `INSUFFICIENT_DATA` long after the agent has started, a mismatched dimension is the usual cause.

### C13. Go live

**Lambda → Configuration → Environment variables** → set `DRY_RUN` to `false`, after a rehearsal.

---

## Part D — Verify and test

### D1. Basic health

```bash
APP=http://$PUBLIC_IP:8000        # or: $(terraform -chdir=terraform output -raw app_base_url)
curl -s $APP/health               # {"status":"ok"}
curl -s $APP/docs -o /dev/null -w "docs %{http_code}\n"   # docs 200 (Swagger UI)
```

The Swagger UI at `$APP/docs` lists the test endpoints.

### D2. Alarm scenarios

Each scenario is one request through Swagger (`$APP/docs`) or the equivalent `curl`. Run them one at a
time and wait for the alarm to return to `OK` before the next.

| Scenario | Trigger | Alarm | Expected decision | Notes |
|---|---|---|---|---|
| Container crash | `POST /chaos/crash` | `autotriage-container-down` within ~2–3 min | `auto`, `restart_container` (≈0.95) | Connection drops — expected |
| 5xx errors | `POST /chaos/500`, then 12× `GET /health` | `autotriage-http-5xx` within ~1 min | `auto`, `restart_container` | Recovery takes ~4–5 min because of log-metric lag |
| Disk full | `POST /chaos/disk` | `autotriage-disk-high` within ~3 min | `auto`, `prune_disk` | Fills to 90%; the call takes ~30–60 s |

```bash
curl -s -X POST $APP/chaos/crash
curl -s -X POST $APP/chaos/500 && for i in $(seq 12); do curl -s -o /dev/null $APP/health; done
curl -s -m 600 -X POST $APP/chaos/disk
```

Expected sequence for an automatic fix: alarm → `ALARM` → Lambda runs → SSM command → wait
(`POST_ACTION_WAIT_SECONDS`) → alarm `OK` → audit row with `recovered=true`.

### D3. Where to look

```bash
# Decisions (one row per run)
aws dynamodb scan --table-name autotriage-audit-log \
  --query 'Items[].[timestamp.S,alarm_name.S,decision.S,verdict.M.action.S,verdict.M.confidence.N,recovered.BOOL,action_result.M.status.S]' --output table

# RCA reports
aws s3 ls s3://$BUCKET/rca/ --recursive | tail
aws s3 cp s3://$BUCKET/rca/<alarm-name>/<file>.md -

# The Lambda's own trace
aws logs tail /aws/lambda/autotriage-triage --since 30m

# Alarm history
aws cloudwatch describe-alarm-history --alarm-name autotriage-container-down --max-records 5
```

### D4. Manual Lambda test (no chaos needed)

With a **healthy** app, the model should choose `none` and escalate, which proves the whole pipeline
without touching the instance:

```bash
cat > event.json <<'EOF'
{"Records":[{"Sns":{"Message":"{\"AlarmName\":\"autotriage-container-down\",\"NewStateReason\":\"manual test\",\"NewStateValue\":\"ALARM\"}"}}]}
EOF
aws lambda invoke --function-name autotriage-triage --cli-binary-format raw-in-base64-out \
  --payload file://event.json out.json && cat out.json
```

If the app is **down** when you run this and `DRY_RUN=false`, the model may choose
`restart_container` and actually restart it. That is intended behaviour, but be aware of it.

### D5. What the test results should look like

- Every run writes **one audit row** and **one RCA**, including escalations and approvals.
- `escalate` rows also send an email to the escalation topic subscriber.
- `approval` means the rate limit blocked an automatic fix (one auto-fix per alarm per
  `RATE_LIMIT_WINDOW_MINUTES`). Fix it by hand, using the same SSM document:

  ```bash
  aws ssm send-command --instance-ids $INSTANCE_ID --document-name autotriage-restart-container \
    --comment "human-approved restart"
  ```

- `recovered=false` after an `auto` row means the restart worked but the alarm had not cleared within
  `POST_ACTION_WAIT_SECONDS`. Check `aws cloudwatch describe-alarms --alarm-names <name>`; if it has since cleared,
  the wait is too short for that alarm.

---

## Part E — Cleanup

### E1. Terraform

```bash
cd terraform
terraform destroy \
  -var="key_name=$KEY_NAME" -var="alert_email=$ALERT_EMAIL" -var="ssh_ingress_cidr=$MY_CIDR"
```

The S3 bucket is not emptied automatically. If `destroy` fails on the bucket, empty it first:
`aws s3 rm s3://$BUCKET --recursive`.

### E2. CLI — reverse order

```bash
aws lambda delete-function --function-name autotriage-triage
aws sns list-subscriptions-by-topic --topic-arn $TRIGGER_TOPIC_ARN --query 'Subscriptions[].SubscriptionArn' --output text | tr '\t' '\n' | xargs -r -n1 aws sns unsubscribe --subscription-arn
aws sns delete-topic --topic-arn $TRIGGER_TOPIC_ARN
aws sns delete-topic --topic-arn $ESCALATION_TOPIC_ARN
for A in autotriage-http-5xx autotriage-container-down autotriage-disk-high; do aws cloudwatch delete-alarms --alarm-names $A; done
aws logs delete-log-group --log-group-name /autotriage/app
aws ssm delete-document --name autotriage-restart-container
aws ssm delete-document --name autotriage-prune-disk
aws ssm delete-parameter --name /autotriage/anthropic-api-key
aws ec2 terminate-instances --instance-ids $INSTANCE_ID && aws ec2 wait instance-terminated --instance-ids $INSTANCE_ID
aws ec2 delete-security-group --group-id $SG_ID
aws iam delete-role-policy --role-name autotriage-triage-lambda-role --policy-name autotriage-triage-lambda-policy
aws iam detach-role-policy --role-name autotriage-triage-lambda-role --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name autotriage-triage-lambda-role
aws iam remove-role-from-instance-profile --instance-profile-name autotriage-ec2-profile --role-name autotriage-ec2-role
aws iam delete-instance-profile --instance-profile-name autotriage-ec2-profile
aws iam detach-role-policy --role-name autotriage-ec2-role --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam detach-role-policy --role-name autotriage-ec2-role --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy
aws iam delete-role --role-name autotriage-ec2-role
aws dynamodb delete-table --table-name autotriage-audit-log
aws s3 rm s3://$BUCKET --recursive && aws s3api delete-bucket --bucket $BUCKET
```

### E3. Console

Delete in this order: Lambda function → SNS subscriptions and topics → alarms → log group → SSM documents and
parameter → EC2 instance → security group → IAM roles → DynamoDB table → emptied S3 bucket.

---

## What changed (so you know what you're building)

These fixes are all in the current code and Terraform. Anyone rebuilding from scratch gets them automatically.

| Area | Problem found | Fix |
|---|---|---|
| Lambda → DynamoDB | `verdict.confidence` is a float; boto3 refuses floats, so the audit write crashed the whole run | `to_dynamo_safe()` converts floats to `Decimal` before the write (`handler.py`) |
| Lambda timeouts | Live timeout was 68 s; the remediation path needs up to ~8 min | `timeout = 600` (`lambda.tf`) |
| Recovery check | 120 s was too short for the log-based 5xx alarm to clear (~4 min) | `post_action_wait_seconds = 360` |
| Recovery notifications | Alarms also publish OK transitions to the trigger topic; the Lambda was treating those as incidents | `lambda_handler` ignores anything that isn't `ALARM` |
| Metric context | CloudWatch returns datapoints newest-first; the model read the pre-crash `1.0`s as a recovery | `ScanBy=TimestampAscending` and labelled timestamps (`context.py`) |
| Disk alarm | The CloudWatch agent publishes `disk_used_percent` with an `fstype` dimension; the live alarm didn't include it, so it never fired | `fstype = xfs` on the alarm and in the Lambda's disk query |
| 5xx alarm | Missing live; the name `autotriage-http-5xx-rate` was refused by AWS | Created as `autotriage-http-5xx` (`alarms.tf`) |
| Rate limit | Dry-run "auto" rows counted against the real limit; the window was hard-coded | Dry-run rows are ignored; window is `RATE_LIMIT_WINDOW_MINUTES` (default 30, test 5) |
| Model | Config still pointed at a retired model | `claude-haiku-4-5-20251001` |
| Escalation email | The escalation topic had no subscriber live | Terraform subscribes it; confirm it (A3) |
| Alarm email | The trigger topic's email subscription wasn't in Terraform | Added `aws_sns_topic_subscription.trigger_email` |
| `dry_run` default | Was `true` | Now `false` (rehearse with `-var dry_run=true`) |

## Things that are still true (by design)

- `docker run --restart=no`: the app does not come back on its own after a crash, so the alarm and the
  fix are needed. The trade-off is that a **reboot** of the instance also leaves the app down until the
  Lambda or a human restarts it.
- The triage Lambda only acts on what `policy.py` allows: `restart_container` and `prune_disk`, at
  confidence ≥ 0.8 and risk `low`. Everything else is escalated.
- `RATE_LIMIT_WINDOW_MINUTES=5` is a test setting. Set it to `30` (Terraform: `rate_limit_window_minutes`)
  for production.
- Log-based alarms (`autotriage-http-5xx`) lag by a few minutes. That is why the post-action wait is 360 s.
