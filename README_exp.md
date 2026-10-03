# AutoTriage, explained file by file

This is the companion doc to `README.md`. That file tells you *how to run it*; this one tells
you *why every file exists and what it's doing*, so you can defend any line of it in an
interview. Read the "How a single incident flows through the system" section first — everything
else is a zoom-in on one step of that flow.

---

## What this project does

AutoTriage is a working, self-contained demo of an AI on-call agent: a small app is broken on
purpose, a CloudWatch alarm notices, and instead of paging a human at 3am, a Lambda function
gathers the evidence, asks Claude to diagnose it, and — only within tightly bounded limits —
fixes it automatically. Concretely, it's built from three cooperating pieces:

1. **Something to break.** A FastAPI app running in a single Docker container on one EC2
   instance, with a handful of "chaos" endpoints (`/chaos/500`, `/chaos/crash`, `/chaos/disk`)
   that each simulate a realistic, distinct on-call incident — a stuck error state, a crashed
   process, and a full disk — plus a `/chaos/reset` to undo it manually.
2. **Something that notices and decides.** CloudWatch alarms watch the app's logs and metrics,
   and route any trip through SNS to a Python Lambda. That Lambda pulls together the last 15
   minutes of logs, recent metrics, and a written runbook, hands all of it to Claude (via the
   Anthropic API) as context, and gets back a structured verdict: what's wrong, how confident it is,
   how risky a fix would be, and which (if any) of exactly two pre-approved fixes to apply.
3. **Something that keeps it honest.** A policy gate only lets that verdict auto-execute if
   confidence and risk both clear a bar, and only if the same alarm hasn't already been
   auto-fixed within the rate-limit window (`RATE_LIMIT_WINDOW_MINUTES`: 30 by default, 5 in the
   current test deployment); the fix itself can only run via one of two pre-defined SSM
   documents against one specific tagged instance — never an arbitrary command. After acting, the
   Lambda re-checks the alarm itself rather than trusting the fix worked, escalates to a human by
   email whenever it's not confident or the fix didn't hold, and writes a permanent record of
   every decision (DynamoDB) plus a human-readable RCA (S3), whether or not it acted.

Everything — the app, the Lambda, the alarms, the IAM policies, the SSM documents — is defined
as Terraform, so the whole thing is reproducible with `terraform apply` and disposable with
`terraform destroy`. The point of the project is less "here's a chatbot that restarts servers"
and more "here's what it actually takes to let an LLM take automated action safely": strict
output validation, a narrow allowlist of actions, independent verification that a fix worked,
rate limiting to prevent fix loops, and a paper trail for every single decision — auto or not.

---

## How a single incident flows through the system

Walk through one concrete case: someone calls `POST /chaos/500`.

1. **The app sets a flag.** `app/main.py` sets `state["force_500"] = True`. From then on, every
   request (including `/health`) gets a `500` with body `{"error": "chaos_500_active"}`. Each
   request — success or failure — is logged as one JSON line to `/var/log/autotriage/app.log`
   *inside the container*, which is actually a bind-mounted path from the host (`terraform/ec2.tf`
   maps `/var/log/autotriage` on the EC2 host into the container at the same path). That's the
   trick that lets a process outside the container (the CloudWatch agent) read the app's logs
   without needing Docker log-driver integration.

2. **The CloudWatch agent ships the logs.** It's running on the host (installed by
   `terraform/user_data.sh.tpl`), configured to tail `/var/log/autotriage/app.log` and forward
   every line into the CloudWatch Logs group `/autotriage/app` (created in `terraform/alarms.tf`).

3. **A metric filter turns log lines into a number.** `aws_cloudwatch_log_metric_filter.http_5xx`
   in `terraform/alarms.tf` watches that log group for lines matching `{ $.status_code = 500 }`
   and increments a custom metric, `AutoTriage/HTTP5xxCount`, by 1 for each match.

4. **An alarm watches that metric.** `aws_cloudwatch_metric_alarm.http_5xx` fires when the sum of
   `HTTP5xxCount` exceeds 5 within a 1-minute period.

5. **The alarm publishes to SNS.** `alarm_actions = [aws_sns_topic.triage_trigger.arn]` — this is
   the wire between "something is wrong" and "let's find out why."

6. **SNS invokes the Lambda.** `aws_sns_topic_subscription.triage_lambda` in `terraform/lambda.tf`
   subscribes the triage Lambda to that topic; `aws_lambda_permission.allow_sns` is the resource
   policy that actually lets SNS call it (subscribing isn't enough by itself — Lambda also needs
   to grant the SNS service principal permission to invoke it).

7. **The Lambda gathers evidence.** `lambda/triage/handler.py` calls into `context.py` to pull
   the last 15 minutes of logs (via CloudWatch Logs Insights), recent CPU/disk/heartbeat metrics,
   and the runbook text from S3.

8. **The Lambda asks Claude.** It builds a prompt (`prompts.py`) from that evidence and POSTs it
   straight to the Anthropic Messages API (`https://api.anthropic.com/v1/messages`) over
   `urllib`, with a system prompt that hard-constrains the output to one JSON object with exactly
   six fields (`summary`, `root_cause`, `confidence`, `action`, `risk`, `evidence`). The API key
   itself is fetched once per cold start from an SSM SecureString parameter, never hardcoded.

9. **The verdict gets validated, not trusted.** `policy.validate_verdict` checks the JSON has the
   right shape, the right keys, an allowlisted (or `"none"`) action, a numeric confidence in
   `[0, 1]`, and a valid risk level. If any of that fails, the incident is escalated immediately
   — the Lambda never tries to interpret or fix malformed model output.

10. **The policy gate decides what happens next.** `policy.decide` looks at the verdict plus
    whether this alarm was already auto-remediated within the rate-limit window
    (`policy.is_rate_limited`, backed by a DynamoDB query). Only `confidence >= 0.8` **and**
    `risk == "low"` **and** not rate-limited gets to run automatically. Everything else goes to
    a human via the escalation SNS topic (email, in this build).

11. **If auto-approved, the fix actually runs.** `actions.run_action` calls
    `ssm_client.send_command`, targeting the one tagged EC2 instance with one of exactly two SSM
    documents (`restart-container.yml` or `prune-disk.yml`) — never an arbitrary shell command.
    If `DRY_RUN=true` (the Terraform default is now `false`; use `true` to rehearse), this step is skipped and just recorded as `"status":
    "dry_run"`.

12. **The Lambda waits, then checks its work.** `time.sleep(POST_ACTION_WAIT_SECONDS)` (360s by
    in the current build — log-based alarms take about 4 minutes to clear after a restart), then `alarm_recovered()` calls `cloudwatch:DescribeAlarms` and checks whether the
    same alarm has settled back to `OK`. If not, it escalates — the fix is not assumed to have
    worked just because the command exited successfully.

13. **Everything is written down, no matter what happened.** `write_audit_log` puts one item per
    incident into DynamoDB (used both as a permanent audit trail *and* as the state the rate
    limiter reads from). `write_rca` renders a markdown incident report and uploads it to S3
    under `rca/<alarm-name>/...`.

That loop — gather, ask, validate, gate, act, verify, log — is the entire system. Every file
below is either a piece of that loop or the infrastructure that wires it together.

---

## `AUTOTRIAGE.md`

The original spec/brief this project was built from — the interview-prep context, the
architecture sketch, the guardrails list, and the 60-minute build plan. Not code; it's the
"requirements doc." Everything else in the repo is an implementation of what this file describes.

## `README.md`

The operator-facing doc: architecture diagram, deploy commands, how to trigger each chaos
scenario, a sample RCA, and a "what I'd add next" list. Read this when you want to *run* the
project; read this file (`README_exp.md`) when you want to understand *why it's built this way*.

## `.gitignore`

Keeps `.idea/` (JetBrains project files), `.venv/` (local Python virtualenv), and Terraform's
local junk (`.terraform/` provider binaries, `*.tfstate*` — which can contain resource
attributes/secrets and should never be committed, `terraform/build/` — the zipped Lambda
package) out of version control.

---

## `app/` — the thing that breaks on purpose

### `app/main.py`

A FastAPI app with one piece of shared mutable state: `state = {"force_500": False}`.

- **`request_middleware`** wraps every request. If `force_500` is set (and the path isn't
  `/chaos/reset`, so you can always turn it back off), it short-circuits to a `500` without
  calling the real handler. Either way, it logs one JSON line per request — `timestamp`,
  `method`, `path`, `status_code`, `duration_ms`, `client` — to both stdout (for `docker logs`
  convenience) and a file handler pointed at `APP_LOG_PATH` (default
  `/var/log/autotriage/app.log`). That file is the one thing the whole observability pipeline
  depends on: no log line, no metric filter match, no alarm.
- **`GET /health`** — trivial 200, unless the 500 flag is active. This doubles as both a
  liveness probe and (indirectly) a way to see the chaos state from outside.
- **`POST /chaos/500`** — sets the flag. Nothing else changes yet; the *next* request is the
  first one that actually 500s. This is deliberate: the endpoint itself should succeed so a
  test script can tell "I successfully triggered chaos" apart from "the trigger itself failed."
- **`POST /chaos/crash`** — logs a marker line (`chaos_crash_triggered`), then calls
  `os._exit(1)`. This is *not* a graceful shutdown — it terminates the process immediately,
  which is the point: it simulates an unhandled fatal error, and because the container is run
  with `--restart=no` (in `user_data.sh.tpl`), Docker won't quietly restart it for you. The
  container-down alarm and the `restart_container` remediation only mean something because of
  that `--restart=no`.
- **`POST /chaos/disk`** — writes 10 MiB random files into `/tmp` in a loop until
  `shutil.disk_usage("/")` reports ≥90% used, or until 2000 files have been written (a safety
  cap so a bug in this endpoint can't fill an entire disk unboundedly — on a real t3.micro's
  small root volume, 90% is reached long before that cap; it only capped out during local
  testing on this dev machine's much larger disk).
- **`POST /chaos/reset`** — clears the flag and deletes every junk file matching the
  `autotriage-chaos-junk-*` prefix. This is also what `prune-disk.yml` calls via `curl` on the
  instance as a courtesy cleanup step.

### `app/requirements.txt`

Pinned `fastapi` and `uvicorn[standard]`. Pinned versions (rather than ranges) so a rebuild
today and a rebuild in six months produce the same image — matters more here than usual because
the image is built *on the EC2 instance itself* at boot time (see `user_data.sh.tpl`), not in
CI, so there's no lockfile/registry layer catching a surprise upstream change.

### `app/Dockerfile`

Deliberately minimal: `python:3.12-slim` base, install deps, copy `main.py`, run
`uvicorn main:app --host 0.0.0.0 --port 8000`. No multi-stage build, no non-root user, no health
check directive — this is a chaos-testing demo app, not a production image, and keeping it
simple keeps the `user_data.sh.tpl` boot script (which builds this image live on first launch)
fast and easy to read.

---

## `ssm/` — the only two things the Lambda is allowed to do

These are AWS Systems Manager **Command documents** (schema version `2.2`), which SSM Run
Command executes on a target instance via the SSM agent — no SSH, no open port needed for the
Lambda to reach the box. The Lambda's IAM policy (`terraform/iam.tf`) names these two documents
explicitly as the only ones it can send; there is no path from "Claude said so" to "arbitrary
shell command runs on the box."

### `ssm/restart-container.yml`

Checks the named container exists (`docker ps -aq -f "name=^$NAME$"`), runs `docker restart` on
it (which works on both a running *and* a stopped container — important, because this same
document fixes both the "stuck at 500" case, where the container is running, and the "crashed"
case, where it's exited), waits 3 seconds, then verifies it actually came back up before
exiting 0. If it's not running afterward, the script exits non-zero, so the Lambda's
`get_command_invocation` polling in `actions.py` will see a `Failed` status rather than a false
"success."

### `ssm/prune-disk.yml`

Deletes files matching `/tmp/autotriage-chaos-junk-*`, runs `docker system prune -f --volumes`
to reclaim any other reclaimable Docker space, and — if the container is up — calls
`http://localhost:8000/chaos/reset` as a courtesy to also clear any lingering 500 flag. Both the
delete and the prune are best-effort (`|| true`): a `rm` finding no matching files, or nothing
for Docker to prune, shouldn't fail the whole remediation.

Both documents use bare `$VAR` / `$(...)` bash substitution rather than `${VAR}` wherever
possible. That's not a style choice — SSM documents use `{{ Parameter }}` mustache-style
substitution themselves, so a literal `${...}` in the script could be misread as a document
parameter reference. Sticking to brace-free bash syntax sidesteps that entirely.

---

## `runbooks/runbook.md`

This is the single piece of domain knowledge the LLM is allowed to reason from, injected
directly into the prompt (see `prompts.build_user_prompt`). It documents exactly three known
incident types (elevated 5xx, container not running, disk usage high), each with its symptoms,
root cause, the one safe fix, and a risk assessment — plus an explicit "when nothing here
matches, say `action: none`" section.

This file matters more than it looks like it should: the system prompt in `prompts.py` tells
Claude to only recommend `restart_container` or `prune_disk` *if the runbook describes that fix
for this exact symptom*. In other words, the runbook — not the model's general knowledge — is
what makes an action "safe" here. If you wanted this system to handle a fourth incident type,
you'd add it here (and add a matching SSM document + allowlist entry), not just hope the model
figures out something sensible on its own.

---

## `lambda/triage/` — the triage brain

All five `.py` files here get zipped together (see `terraform/lambda.tf`'s `archive_file` data
source) and deployed as one Lambda function. They're flat, sibling modules — `handler.py` does
`from actions import run_action`, not a package-relative import — because that's how the zip is
laid out (each file at the zip root, not inside a subfolder), which is what Lambda's Python
import model expects for a small function like this.

### `lambda/triage/prompts.py`

Two things: `SYSTEM_PROMPT`, a fairly aggressive set of constraints (respond with *only* JSON,
exactly these six keys, `action` must be one of three literal strings, don't guess when
unsure), and `build_user_prompt()`, which just concatenates the alarm info, logs, metrics, and
runbook into one text block under markdown headers. There's no few-shot examples, no chain-of-
thought scaffolding — the design bet here is that a small, well-specified schema plus an
explicit "don't guess" instruction gets you further on reliability than a cleverer prompt would,
especially combined with the fact that nothing downstream *trusts* the output anyway (see
`policy.py`).

### `lambda/triage/policy.py`

The guardrail logic, and the only file in the repo you'd point to if someone asked "how do you
know this won't do something dangerous automatically."

- `validate_verdict(verdict)` — pure defensive parsing. Confirms it's a dict, has all six
  required keys, `action` is in the allowlist or is `"none"`, `confidence` is a number in
  `[0, 1]`, and `risk` is one of the three valid strings. Returns `None` on success or a
  human-readable error string on failure — the caller (`handler.py`) treats any non-`None`
  return as "escalate, don't try to salvage this."
- `decide(verdict, rate_limited)` — the actual gate. Reads top-to-bottom as the guardrail list
  itself: `action == "none"` always escalates; an action outside the allowlist always escalates
  (defense in depth — `validate_verdict` should have already caught this, but the check costs
  nothing); a rate-limited alarm downgrades an otherwise-good verdict to `"approval"` rather
  than running it again; only `confidence >= 0.8 and risk == "low"` gets `"auto"`; everything
  else is `"approval"`.
- `is_rate_limited(recent_items)` — walks the last 20 audit-log entries for this specific alarm
  (fetched by `handler.get_recent_audit_items`) and returns `True` if any of them was itself an
  `"auto"` decision within the rate-limit window. This is what stops a flapping alarm from
  triggering a `restart_container` every single time it fires — the second occurrence within the
  window gets bumped to `"approval"` (human review) instead of just running the same fix again.

### `lambda/triage/actions.py`

`run_action(ssm_client, action, instance_id, dry_run)` is the only place in the whole system
that actually changes anything on the EC2 instance. It maps the two allowlisted action names to
SSM document names (read from env vars `SSM_DOC_RESTART` / `SSM_DOC_PRUNE`, set by Terraform to
the real deployed document names), and either short-circuits with a `"dry_run"` status (if
`DRY_RUN=true`) or calls `ssm_client.send_command` and then polls `get_command_invocation` every
5 seconds for up to 90 seconds waiting for the command to leave `Pending`/`InProgress`/`Delayed`.
It returns the actual status, plus the last 2000 characters each of stdout/stderr, so the RCA
written later has real evidence of what the remediation script did — not just "we sent a
command."

### `lambda/triage/context.py`

Three "go fetch the evidence" functions, each deliberately fail-soft (they catch their own
exceptions and return a string describing the failure, rather than raising) — because a metrics
API hiccup shouldn't crash the whole triage run before the Lambda even gets to ask Claude
anything.

- `get_recent_logs()` — runs a CloudWatch Logs Insights query (`start_query` /
  `get_query_results`, polled once a second for up to 15 seconds) for the last 15 minutes of
  `@timestamp, @message`, newest first, capped at 200 rows.
- `get_recent_metrics()` — a single `get_metric_data` call fetching three time series over the
  same 15-minute window: EC2 `CPUUtilization`, the CloudWatch agent's `disk_used_percent`, and
  the custom `ContainerRunning` heartbeat — so the model can see "was the box under load" and
  "was the container even alive" alongside the logs.
- `get_runbook()` — a plain `s3.get_object` for the runbook markdown.

### `lambda/triage/handler.py`

The orchestrator; ties every other module together into `lambda_handler(event, context)`.

- Module-level `boto3` clients and `os.environ[...]` reads happen once per cold start (a Lambda
  performance convention — every subsequent invocation on a warm container reuses them). This
  also means the function will fail fast at import time if a required env var is missing, which
  is intentional: better a clear cold-start failure than a confusing `KeyError` mid-triage.
  `_load_anthropic_api_key()` follows the same pattern: it calls `ssm:GetParameter` with
  `WithDecryption=True` exactly once per cold start and caches the result in the module-level
  `ANTHROPIC_API_KEY` — the key is never written to an environment variable (which would show up
  in plaintext in the Lambda console and in `DescribeFunctionConfiguration` API responses).
- `lambda_handler` iterates `event["Records"]` (SNS can, in principle, batch multiple messages
  into one Lambda invocation) — for each one, it unpacks the CloudWatch Alarm JSON that SNS
  wrapped, and calls `handle_alarm`. A broad `except Exception` around that call means a bug in
  triage logic itself still produces an escalation and a logged result, rather than a silent
  Lambda failure that nobody finds out about until the on-call engineer notices the alarm never
  got a response.
- `handle_alarm` is steps 7–13 of the walkthrough at the top of this document, in order: gather
  context → `call_claude` → validate → check rate limit → `decide` → (if `"auto"`) run the
  action, wait, re-check the alarm → write the audit log entry → write the RCA → return a result
  dict.
- `call_claude` builds the request body itself (model, `max_tokens`, `temperature: 0.0`, the
  system prompt, and the user message) and POSTs it via `urllib.request` — there's no `anthropic`
  SDK dependency to vendor into the Lambda zip, just the standard library, which is also why
  `terraform/lambda.tf`'s `archive_file` can zip the source directory as-is with no separate
  `pip install --target` build step. `urllib.error.HTTPError`/`URLError` are caught explicitly so
  a network hiccup or an API error response becomes a normal `(None, error_string)` escalation
  rather than an uncaught exception. It also handles one rough edge common to LLM JSON output:
  models occasionally wrap JSON in markdown code fences even when told not to, so there's a small
  `strip("`")` / `raw_text[4:]` cleanup before `json.loads`. If parsing still fails, or
  `validate_verdict` rejects the result, `call_claude` returns `(None, error_string)` and
  `handle_alarm` escalates immediately without ever reaching the policy gate — matching the
  "invalid LLM JSON → escalate, never guess" guardrail verbatim.
- DynamoDB's `timestamp` attribute is deliberately never referenced inside a
  `KeyConditionExpression`/`FilterExpression` string in this file — `timestamp` is a DynamoDB
  *reserved word*, and reserved words are only a problem inside expression strings (not as
  plain item attributes), so the query sorts by it via `ScanIndexForward=False` instead of
  naming it directly.

### `lambda/triage/requirements.txt`

Just a comment plus `boto3`. The Lambda Python 3.12 runtime already bundles `boto3`/`botocore`,
so this file isn't actually installed into the deployed package (the Terraform `archive_file`
data source zips the source directory as-is; nothing runs `pip install` against this file at
deploy time) — it exists purely so a human (or an IDE) working on this code locally gets
completions and version info.

---

## `terraform/` — everything that has to exist in AWS before any of the above can run

Fourteen files. Grouped by what they're responsible for, in roughly the order Terraform would
need to create them (though Terraform itself figures out the actual dependency graph from
resource references, not file order).

### `versions.tf`

Pins the Terraform CLI to `>= 1.5.0` and two providers: `hashicorp/aws` (`~> 5.x`) for basically
everything, and `hashicorp/archive` (`~> 2.4`) used once, to zip the Lambda source directory.
Also where the `aws` provider's region is set, from `var.aws_region`.

### `variables.tf`

Every knob the rest of the config reads. Two have no default and must be supplied at apply time
— `key_name` (an EC2 key pair that has to already exist in the target account/region; Terraform
doesn't create one for you here) and `alert_email` (who gets escalation emails). Everything else
defaults to something reasonable for a quick demo: `us-east-1`, `t3.micro`, `dry_run = false`,
a specific Anthropic model id you can override without touching any `.tf` file, and the SSM
parameter name (`/autotriage/anthropic-api-key`) the Lambda reads its API key from.

### `data.tf`

Read-only lookups plus a `locals` block:

- `data.aws_ami.al2023` finds the most recent Amazon Linux 2023 x86_64 AMI at apply time, so the
  instance always boots on a current AMI rather than one hardcoded and slowly going stale.
- `data.aws_vpc.default` / `data.aws_subnets.default` reuse the account's default VPC rather than
  building a new one — appropriate for a scoped demo, not something you'd do for a real
  production workload sharing an account with other things.
- `data.aws_caller_identity.current` supplies `local.account_id`, needed to build a
  globally-unique S3 bucket name and a couple of IAM resource ARNs.
- `locals.name_prefix` / `local.common_tags` are the "type it once" pattern — every resource
  name below is `"${local.name_prefix}-something"`, so renaming the whole deployment is a
  one-variable change (`var.project_name`).

### `s3.tf`

One bucket (`<project>-artifacts-<account-id>` — the account ID suffix is what makes the name
globally unique without a `random_id` resource), with public access fully blocked and
AES256 server-side encryption enabled by default — this bucket holds the runbook and every RCA
report, neither of which should ever be publicly reachable. `aws_s3_object.runbook` uploads
`runbooks/runbook.md` at apply time, keyed by `filemd5(...)` so Terraform only re-uploads it when
the file's contents actually change.

### `dynamodb.tf`

One table, `<project>-audit-log`, `PAY_PER_REQUEST` billing (no capacity planning needed for a
low-volume audit log), hash key `alarm_name` + range key `timestamp`. That two-part key is what
makes `get_recent_audit_items` (querying "all recent items for this one alarm") and the rate
limiter both cheap, direct-index queries rather than table scans.

### `sns.tf`

Two topics, matching the two branches in the architecture diagram: `triage_trigger` (alarms →
Lambda) and `escalation` (Lambda → human). `aws_sns_topic_subscription.escalation_email`
subscribes `var.alert_email` to the second one directly in Terraform — note that AWS still
requires that address to confirm the subscription via a link in an email SNS sends it before
messages actually start flowing.

### `ssm_documents.tf`

Registers the two YAML files from `ssm/` as actual `aws_ssm_document` resources
(`document_type = "Command"`), reading their content straight from disk with `file(...)`. This
is the Terraform-side half of "only two pre-defined SSM documents" — their names
(`<project>-restart-container`, `<project>-prune-disk`) get passed into the Lambda's environment
in `lambda.tf`, and their ARNs get named explicitly in the Lambda's IAM policy in `iam.tf`.

### `secrets.tf`

One resource: an `aws_ssm_parameter` (type `SecureString`) that holds the Anthropic API key.
The interesting part is what it *doesn't* do — `value = "REPLACE_ME"` combined with
`lifecycle { ignore_changes = [value] }` means Terraform creates the parameter (so its name and
ARN exist and are stable for `iam.tf` and `lambda.tf` to reference) but then permanently stops
caring what value lives in it. You set the real key once, out-of-band, via `aws ssm
put-parameter --overwrite` (see `README.md`), and every subsequent `terraform apply` leaves it
alone instead of clobbering a live key back to the placeholder. The alternative — passing the
real key in as a `sensitive` Terraform variable — would still leave it sitting in plaintext
inside the Terraform state file; keeping it out of Terraform's hands entirely avoids that.

### `alarms.tf`

The CloudWatch log group, one metric filter, and three alarms — all described in detail in the
"How a single incident flows" walkthrough above. Two implementation details worth calling out:

- The container-down and disk alarms both key off `dimensions = { InstanceId = aws_instance.app.id, ... }`.
  CloudWatch alarms require an *exact* dimension match against the metric — if the CloudWatch
  agent emits a metric with dimensions `{InstanceId, path, fstype, device}` but the alarm only
  specifies `{InstanceId, path}`, they won't match and the alarm will sit in `INSUFFICIENT_DATA`
  forever. That's why `user_data.sh.tpl`'s CloudWatch agent config sets `"drop_device": true` on
  the disk metric (removing one of the four dimensions the agent would otherwise emit), and why
  `disk_high`'s alarm dimensions include a hardcoded `fstype = "xfs"` (Amazon Linux 2023's
  default root filesystem) matching what's left. If a different AMI or filesystem is ever
  substituted, `fstype` here would need to change to match, or the alarm silently never
  triggers. The Lambda's own disk query in `context.py` uses the same three dimensions, so the
  metrics it shows the model match the alarm.
- `treat_missing_data = "breaching"` on `container_down` is deliberate and different from the
  other two alarms (`"notBreaching"`): if the heartbeat script itself stops running — for
  instance because the whole instance is down, not just the container — there's no data point at
  all, and *that* absence should itself count as "the container is not running," not be ignored.

### `iam.tf`

Two roles. The EC2 role is boring on purpose — two AWS-managed policies
(`AmazonSSMManagedInstanceCore` so the SSM agent can register and receive Run Command jobs, and
`CloudWatchAgentServerPolicy` so the CloudWatch agent can push metrics/logs) and nothing custom.

The Lambda role is where the actual least-privilege design lives, one statement per capability:
read-only on logs/metrics/alarms (`Resource: "*"` — these AWS APIs mostly don't support
resource-level scoping, so `"*"` here isn't a shortcut, it's close to the tightest policy that's
expressible); `s3:GetObject`/`PutObject` scoped to `<bucket-arn>/*`, not the whole account;
`dynamodb:PutItem`/`Query` scoped to exactly the one table ARN; `sns:Publish` scoped to exactly
the escalation topic (notably *not* the trigger topic — the Lambda only ever publishes to
escalation, never back to trigger); `ssm:GetParameter` scoped to exactly the one SSM parameter
holding the Anthropic API key (`aws_ssm_parameter.anthropic_api_key.arn` — see `secrets.tf`),
paired with a `kms:Decrypt` statement that has to use `Resource: "*"` for a narrower reason than
the read-only APIs above: the parameter uses the AWS-managed `aws/ssm` key, and IAM simply
doesn't support scoping `kms:Decrypt` to a key by its alias ARN — the `GetParameter` statement
right above it is what actually limits *which* secret can be decrypted, this one just grants the
mechanical ability to decrypt whatever `GetParameter` already let through; and the guardrail
statement, `ssm:SendCommand` scoped to a `Resource` list containing *only* the two document ARNs
and the one instance ARN — not `"*"`, not "any instance with this tag," but this literal
instance, created by this literal Terraform config, referenced by its actual resource ID
(`aws_instance.app.arn`).

### `ec2.tf`

A security group (SSH on 22, the app port on 8000, both from configurable CIDRs that default —
loosely, for demo speed — to `0.0.0.0/0`; unrestricted egress) and the instance itself: latest
AL2023 AMI, `var.instance_type` (default `t3.micro`), the default VPC's first subnet, the
security group and instance profile defined elsewhere in this directory, and a `user_data`
built from `templatefile()`.

The `templatefile()` call is worth understanding on its own: it reads `user_data.sh.tpl` and
substitutes six values into it — three plain strings (`container_name`, `aws_region`,
`log_group_name`) and three base64-encoded blobs of the *entire contents* of
`app/main.py`, `app/requirements.txt`, and `app/Dockerfile`. That's how the app's source code
gets from this repo onto the EC2 instance without an S3 upload, an ECR push, or a `git clone` at
boot: Terraform bakes the files directly into the instance's user-data script as base64 text,
and the script itself `base64 -d`'s them back out into real files once it's running on the box.
It's a deliberately low-ceremony choice appropriate for a three-file demo app — it would not
scale to a real application's codebase.

### `user_data.sh.tpl`

The boot script that turns a bare AL2023 instance into "the demo app is running and being
watched." In order: update packages, install Docker + `cronie` (cron) + `unzip`/`jq`; install
the AWS CLI v2 if it isn't already on the AMI; download and install the CloudWatch agent RPM
directly from its S3 distribution URL (`amazoncloudwatch-agent-<region>.s3.<region>.amazonaws.com`
— the standard AWS-documented path, since this package isn't in AL2023's default `dnf` repos);
decode the three base64 app files onto disk; write the CloudWatch agent's JSON config (disk +
memory metrics, and the app log file → `/autotriage/app` log group mapping) and start the agent;
`docker build` and `docker run --restart=no` the app, bind-mounting `/var/log/autotriage` so the
agent (running on the host) can read logs written by the app (running in a container); and
finally, write and cron-schedule `heartbeat.sh`, a one-minute job that checks
`docker ps -f status=running` for the named container and calls
`aws cloudwatch put-metric-data` with `1` or `0` — this is the *only* source of the
`ContainerRunning` metric that `container_down` in `alarms.tf` watches. Without this script,
nothing outside the container itself would ever know the container had died.

One syntax detail if you ever edit this file: it's processed by Terraform's `templatefile()`
before bash ever sees it, so any *literal* `${...}` in the script (the CloudWatch agent's own
`${aws:InstanceId}` placeholder syntax) has to be written as `$${aws:InstanceId}` — doubling the
`$` is how you escape Terraform's own interpolation syntax so it passes through unchanged.
Everywhere else, the script deliberately uses brace-free `$VAR` bash syntax specifically to avoid
needing that escaping at all.

### `lambda.tf`

`data.archive_file.triage_lambda` zips `lambda/triage/` (minus `requirements.txt` and any
`__pycache__`) into `terraform/build/triage_lambda.zip`. `aws_lambda_function.triage` deploys
that zip with `handler.lambda_handler` as the entry point, Python 3.12, 256 MB memory, and a
480-second timeout — sized generously because a single invocation can spend up to ~15s on the
Logs Insights query, a few seconds on the Anthropic API call, up to 90s polling an SSM command,
and then a full 360s post-action wait before re-checking the alarm. Worst case is about 495s, so
the timeout is 600s (it was 480s until the worst-case arithmetic showed the gap). Every environment variable `handler.py` reads (`os.environ[...]`) is set here,
pulling real values from other resources — the log group's actual name, the instance's actual
ID, the bucket's actual name, the Anthropic API key parameter's actual name, and so on — so
nothing in the Lambda code hardcodes a resource name that Terraform might change.
`aws_lambda_permission.allow_sns` and
`aws_sns_topic_subscription.triage_lambda` are the two halves of "SNS can invoke this Lambda":
the subscription says "deliver messages here," the permission is the resource-based policy that
actually authorizes SNS to call `lambda:InvokeFunction` — both are required, one alone isn't
enough.

### `outputs.tf`

The values you actually need after `terraform apply` finishes: the instance's ID and public IP,
a ready-to-copy SSH command, the app's base URL (used directly by the `curl` commands in
`README.md`), both SNS topic ARNs, the Lambda's name, the DynamoDB table / S3 bucket / log group
names, and the Anthropic API key parameter's name (used directly by the `aws ssm put-parameter`
command in `README.md` that sets the real key after the first apply) — everything you'd otherwise
have to go find in the AWS console to verify the system end to end.

---

## Design choices worth being able to defend out loud

- **Every "did it work" claim is independently checked**, not assumed from a non-error return
  code: the SSM document itself verifies the container came back up before exiting 0; the Lambda
  separately re-checks the *CloudWatch alarm* (not just the SSM command status) before declaring
  recovery.
- **The model's output is validated, not trusted**, and invalid output has exactly one path:
  escalate. There's no "try to fix up the JSON" or "guess a sensible default" branch anywhere in
  `policy.py` or `handler.py`.
- **The blast radius of automation is bounded in three independent ways at once**: an IAM policy
  that can only target two named documents and one named instance, a policy-gate confidence/risk
  threshold, and a rate limiter (30 minutes by default) — any one of them failing doesn't remove the other two.
- **`DRY_RUN` exercises the entire decision pipeline**, not a mocked-out version of it — the only
  thing it skips is the actual `ssm:SendCommand` call and the subsequent wait. Everything else
  (gathering context, calling Claude, validating, gating, logging, writing the RCA) runs for
  real, which is what makes it possible to demo and debug the whole system safely before ever
  flipping `dry_run = false` (the default is now `false`, so rehearse with `-var dry_run=true` first).
