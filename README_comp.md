# AutoTriage — how the whole thing works, end to end

This is the "explain it like I'm new to this" version. It walks through one incident from start to
finish, says what each AWS service is doing and why it's there, and spends the most time on
**DynamoDB**, which is the piece people most often ask about.

For setup, see [`aws_readme.md`](aws_readme.md). For the design rationale in more depth, see
[`README_exp.md`](README_exp.md). For the code itself, see [`lambda-triage-annotated/`](lambda-triage-annotated/),
which explains every line.

---

## The one-paragraph version

A small web app runs on an EC2 server. When something goes wrong with it (the app crashes, starts
returning errors, or the disk fills up), a CloudWatch **alarm** fires. The alarm sends a message to
an SNS **topic**, which wakes up a **Lambda** function. The Lambda gathers evidence (logs, metrics,
and a runbook), asks **Claude** what's wrong and what the safe fix is, and then decides whether it may
apply that fix by itself. If it may, it runs one of two pre-approved commands on the server through
**SSM**, checks that the alarm cleared, and writes everything down. If it may not, it emails a human.
Every single run, whatever it decided, leaves a record in **DynamoDB** (the audit log) and a written
report in **S3** (the RCA).

---

## The cast

| Piece | What it is | Why it's here |
|---|---|---|
| **EC2 instance + Docker app** | A `t3.micro` server running a small FastAPI app in a container | The thing that breaks, on purpose, so we can test the whole loop |
| **CloudWatch Logs** | Central log storage | The app's JSON log lines land here; the Lambda reads them back to understand the incident |
| **CloudWatch Metrics** | Numbers over time (CPU, disk, "is the container running?") | The alarms watch these numbers; the Lambda also reads them as context |
| **CloudWatch Alarms** | "Fire when metric X crosses threshold Y" | The trigger for everything. Three of them: 5xx errors, container down, disk full |
| **SNS topics** | Pub/sub message channels | `triage-trigger` carries alarm events to the Lambda; `escalation` carries "a human needs to look" to email |
| **Lambda** (`autotriage-triage`) | Code that runs on demand, with no server to manage | The brain: gather, ask, decide, act, verify, record |
| **Claude** (Anthropic API) | The language model | Reads the evidence and returns a structured diagnosis: what's wrong, how sure, how risky the fix is |
| **SSM Run Command** | "Run this pre-written script on that instance" | The hands. Only two scripts exist: restart the container, prune the disk |
| **DynamoDB** | A managed key-value/NoSQL database | The Lambda's **memory and audit trail** (see the long section below) |
| **S3** | File storage | Holds the runbook the model is told to trust, and one RCA report per incident |
| **SSM Parameter Store** | Encrypted config storage | Holds the Anthropic API key, so it's not in code or plain env vars |
| **IAM** | Permissions | Limits what each piece may touch. The Lambda can only run the two named scripts on this one instance |

---

## What happens during one incident

Take the most common case: someone (or a chaos test) crashes the app.

**1. The app dies.** The process calls `os._exit(1)`. Docker was started with `--restart=no`, so
nothing brings it back. That's deliberate: a dead app has to stay dead long enough for the alarm
to notice, or there's nothing to triage.

**2. Something notices.** A cron job on the instance runs every minute and publishes a number to
CloudWatch: `ContainerRunning = 1` if the container is up, `0` if not. (It has to be a cron job
because a dead app can't report its own death.)

**3. The alarm fires.** `autotriage-container-down` says "the metric has been below 1 for two minutes."
It moves from `OK` to `ALARM`.

**4. The alarm tells the Lambda.** The alarm publishes to the `triage-trigger` SNS topic, which
invokes the Lambda with the alarm's name and reason. (Alarms also publish when they return to `OK`.
The Lambda ignores those, since recovery isn't an incident.)

**5. The Lambda gathers evidence.** Three things, every time:
- **Logs:** the last 15 minutes of app log lines, from CloudWatch Logs.
- **Metrics:** CPU, disk, and the container heartbeat, from CloudWatch Metrics, oldest-first.
- **Runbook:** a markdown file from S3 that describes each known failure and its safe fix.
  This is the *only* source of truth the model is told to trust.

**6. The model diagnoses.** All of that goes to Claude with a strict instruction: reply with JSON
only, with exactly these fields:

| Field | Meaning |
|---|---|
| `summary` / `root_cause` | What's wrong, in plain words |
| `confidence` | 0.0–1.0, how sure it is about the cause *and* that the fix will work |
| `action` | One of `restart_container`, `prune_disk`, or `none`. Nothing else is allowed |
| `risk` | `low`, `medium`, or `high`: the risk of running that action right now |
| `evidence` | The specific log lines or metric values it relied on |

If the reply isn't valid JSON, or a field is missing or out of range, the Lambda doesn't try to fix
it. It escalates. Malformed output is never guessed at.

**7. The policy gate decides.** This is the part that keeps automation safe. The model's opinion is
only a suggestion; the rules below have the final say, and they're deliberately boring:

- The action must be on the allowlist (`restart_container` or `prune_disk`).
- `confidence` must be at least 0.8, **and** `risk` must be `low`.
- The same alarm must not have been auto-fixed within the rate-limit window (more on this below).

If all of that passes, the decision is `auto`. If the model said `none`, or the action isn't
allowed, the decision is `escalate`. If the rules block an automatic fix (low confidence,
medium risk, or the rate limit), the decision is `approval`: a human must act.

**8. The fix runs (only if `auto`).** The Lambda calls SSM Run Command on the instance with the
matching document. The document is a short, fixed script in `ssm/`. For example,
`restart-container.yml` runs `docker restart autotriage-app`. The Lambda can't send any other
command.

**9. The Lambda checks its own work.** It waits (`POST_ACTION_WAIT_SECONDS`, 360s now), then asks
CloudWatch whether the alarm is back to `OK`. A fix that ran without error but didn't clear the
alarm counts as a failure. The sleep is long because log-based alarms are slow to clear.

**10. Everything is recorded.** Whatever happened (auto-fixed, needs approval, or escalated), the
Lambda writes:
- one row to **DynamoDB** (the audit log), and
- one markdown **RCA** to **S3**, a human-readable story of the incident.

The one gap: if the code itself crashes with an unexpected exception, the Lambda sends an escalation
email and logs the traceback, but it doesn't reach the audit write, so there's no DynamoDB row for
that run. The traceback in the Lambda's logs is the record in that case.

**11. Humans are told when needed.** Any run that didn't end in a successful automatic fix sends
an email through the `escalation` SNS topic, with the verdict attached.

That's the whole loop. An automatic fix looks like this in the logs:
`ALARM → Lambda → model says restart_container (0.95, low) → SSM restarts the app → 6 min wait →
alarm OK → audit row with recovered=true`.

---

## DynamoDB, in depth

### What it is

DynamoDB is AWS's managed database for simple, very fast lookups by key. There are no servers to
run, no schema to migrate, and you pay per request. Our table is:

- **Name:** `autotriage-audit-log`
- **Partition key:** `alarm_name` (which alarm this row is about)
- **Sort key:** `timestamp` (when the run wrote it, UTC, to the second)
- **Billing:** on-demand, so cost follows usage (a handful of rows per incident)

Think of it as a spreadsheet where every row is one triage run, grouped by alarm and sorted by time.

### Why it was introduced

The short answer: **the Lambda forgets everything between runs, and it needs to remember one thing.**

A Lambda function is stateless. Each invocation may run on a fresh container, two invocations can
run at the same time, and nothing in memory survives from one alarm to the next. So if the Lambda
needs to know "did I already restart this container a few minutes ago?", that answer has to live
somewhere outside the Lambda. DynamoDB is that place.

That's the job it was introduced for:

1. **Rate limiting (the one thing the code *reads* from it).** The rule is "at most one automatic
   fix per alarm per rate-limit window" (30 minutes by default, 5 in the test deployment). This
   is the guard against a fix loop: if restarting the container doesn't solve the problem, the
   alarm fires again, and without this rule the Lambda would restart the app forever.
   Before it decides anything, the Lambda asks DynamoDB for this alarm's most recent rows and
   checks whether any of them is a real, recent `auto` decision.

2. **An audit trail (what the code *writes*).** Every run, whatever it decided, leaves a row. You
   can answer "what did the system do last Tuesday, and why?" with one query, without reading
   logs. That matters for trust: an automation that acts on production should be easy to audit.

Why not the alternatives?

| Option | Why not |
|---|---|
| Keep it in the Lambda's memory | Lost between invocations; not shared between concurrent runs |
| Use the CloudWatch Logs | Logs are text. Asking "the last auto-fix for this alarm, within 30 minutes" means searching free text, slowly and fragilely. We need a keyed lookup |
| Use S3 | Good for files (we use it for the RCA reports), but there is no fast "give me this alarm's recent rows" query |
| A relational database (RDS) | Needs a running server, networking, and a schema. Far too much for a few rows per incident |

DynamoDB gives us a keyed lookup by alarm name, sorted by time, with no server to run. It fits
the access pattern exactly.

### What a row looks like

A row from our successful crash test (condensed):

```
alarm_name        autotriage-container-down
timestamp         2026-10-03T17:23:34Z          ← sort key
incident_id       fcaf891a-94be-4881-aeef-8d8ac8a27258
decision          auto                          ← auto | approval | escalate
decision_reason   confidence and risk within policy for automatic remediation
verdict           { action: restart_container, confidence: 0.95, risk: low, root_cause: "...", evidence: [...] }
action_result     { status: Success, document_name: autotriage-restart-container, output: "...", ... }
recovered         true                          ← did the alarm clear after the fix?
dry_run           false                         ← was this a real run?
```

Each attribute has a job:

| Attribute | Written by | Used for |
|---|---|---|
| `alarm_name` | every run | The partition key. Groups all rows for one alarm together |
| `timestamp` | every run | The sort key. Gives the time order, and drives the rate-limit window |
| `decision` | every run | `auto` rows are what the rate limiter looks for |
| `dry_run` | every run | A dry-run `auto` never ran anything, so it's skipped by the rate limiter |
| `recovered` | runs that auto-fixed | Whether the fix worked. `false` means the restart happened but the alarm didn't clear in time |
| `verdict`, `decision_reason`, `action_result` | every run | The full story, for humans reading the audit trail |
| `incident_id` | every run | Ties the row to its RCA file in S3, which has the same ID in its filename |

### How it's used, step by step

**On every run (the write).** After the decision is made and the action (if any) has finished, the
Lambda calls `put_item` with the row above. This happens for escalations and errors too, so the log
has no gaps.

**Before deciding (the read).** Once the model's verdict has been validated, and right before the
policy gate runs, the Lambda calls `query` for
this alarm's 20 most recent rows, newest first. It hands them to `is_rate_limited()`, which returns
true if any row is an `auto` decision, not a dry run, and within the window. If so, the new
decision is downgraded to `approval`. This is the only read the system does against the table.

The 20-row limit is a deliberate choice: the rate limit only ever needs the most recent rows, so
there's no reason to read the whole history on every run.

### A real example of the rate limiter at work

From our testing:

| Time (UTC) | Row | Effect |
|---|---|---|
| 16:23:57 | `auto`, **dry run** | Nothing ran. It counted against the limit until the fix; it's ignored now |
| 16:32:15 | `approval` | Blocked by the 16:23 dry run. The limiter at that point counted dry runs too; this was fixed right after |
| 16:40:29 | `auto`, real, `Success` | The first real fix in the window |
| 16:47:28 | `approval` | Blocked: the 16:40 real fix was still within the window |
| 17:23:34 | `auto`, real, `recovered=true` | The window had passed, so the fix ran |

The rows at 16:47 and 17:23 are the same kind of event, but only the second one was allowed to act.
That's the rate limiter working.

### Things to know about the table

- **Sort key precision is one second.** Two runs for the same alarm in the same second would write
  the same key, and the second would overwrite the first. Unlikely in practice (alarms are minutes
  apart), but it's a real edge case, and adding the `incident_id` to the sort key would close it.
- **The limiter only looks at 20 rows.** That's enough for the 5- or 30-minute window, but it means
  the limiter is only as good as the most recent activity.
- **Nothing expires.** Rows stay forever. Cheap at this volume, but a TTL attribute would be the
  first thing to add for a long-running system.
- **Reads return what's in the table, not what's true.** A row with `recovered=false` means the
  Lambda's check failed, not necessarily that the app is still broken. Check the alarm and the app
  before drawing conclusions.
- **`dry_run` is recorded on every row**, so you can tell rehearsal runs apart from real ones when
  reading the history.

### Useful queries

```bash
# All decisions, newest first, for every alarm
aws dynamodb scan --table-name autotriage-audit-log \
  --query 'Items[].[timestamp.S,alarm_name.S,decision.S,verdict.M.action.S,recovered.BOOL]' --output table

# One alarm's history (what the rate limiter itself looks at)
aws dynamodb query --table-name autotriage-audit-log \
  --key-condition-expression "alarm_name = :a" \
  --expression-attribute-values '{":a":{"S":"autotriage-container-down"}}' \
  --scan-index-forward false --max-items 20
```

---

## The other pieces, briefly

- **S3 (`autotriage-artifacts-<account>`)** holds two things. `runbooks/runbook.md` is the instruction
  manual the model is told to trust: it lists the known incidents, their symptoms, and their
  one safe fix. `rca/<alarm>/<timestamp>-<incident>.md` is one human-readable report per run,
  with the verdict, evidence, and action result. DynamoDB is for machines to query; S3 is for a
  person to read.

- **SSM Run Command** is the only way the system changes the server. The two documents in
  `ssm/` are fixed scripts. The Lambda can't pass arguments that change what they do, and it can
  only target the one tagged instance.

- **The chaos endpoints** (`/chaos/500`, `/chaos/crash`, `/chaos/disk`) are how you test the loop
  on purpose. Each one reproduces one of the three failures the runbook describes.

- **The escalation topic** is the hand-off to a human. Email arrives for anything not auto-fixed.
  Approvals are not clicked in the email: a human runs the same SSM document by hand, which is
  exactly what we did in testing.

- **The guardrails are layered on purpose.** IAM limits what can be touched. The policy gate limits
  what may run automatically. The rate limit stops loops. The post-fix check stops "it ran, so it
  must be fixed." Any one of them failing doesn't remove the other three.

---

## Where things are, and what to look at

| Question | Where to look |
|---|---|
| Did the alarm fire? | CloudWatch → Alarms, or `aws cloudwatch describe-alarm-history --alarm-name <name>` |
| What did the Lambda decide, and why? | DynamoDB `autotriage-audit-log` (the decision and verdict on each row) |
| What did it *say*, in words? | S3 `rca/<alarm>/...md` |
| What did it actually run on the server? | `action_result` on the row, or SSM → Run Command history |
| Why did it do what it did, step by step? | `aws logs tail /aws/lambda/autotriage-triage --since 30m` |
| Is the app up? | `curl http://<instance-ip>:8000/health`, or the Swagger page at `/docs` |

---

## Current state and rough edges

- **Live config today:** `DRY_RUN=false` (real fixes), a 5-minute rate-limit window (a test setting;
  use 30 for production), a 360-second post-fix wait, and a 600-second Lambda timeout.
- **Code is not committed yet.** The deployed Lambda, the Terraform, and the annotated copy all match
  each other, but the changes are still uncommitted in the working tree.
- **Alarm lag.** Log-based alarms (5xx) take a few minutes to clear after a fix, which is why the
  wait is long. A direct health check from the Lambda would be faster; that's listed as a next step
  in `README.md`.
- **Two configs drift easily.** The live AWS account was built partly by hand, so anything changed in
  the console or CLI and not in Terraform can drift. The `aws_readme.md` "What changed" section lists
  the known differences.
