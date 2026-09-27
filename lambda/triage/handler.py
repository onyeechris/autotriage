"""SNS-triggered triage Lambda: gather context -> ask Claude -> policy gate -> act -> verify -> log."""
import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid

import boto3

from actions import run_action
from context import get_recent_logs, get_recent_metrics, get_runbook
from policy import decide, is_rate_limited, validate_verdict
from prompts import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger()
logger.setLevel(logging.INFO)

LOG_GROUP_NAME = os.environ["LOG_GROUP_NAME"]
INSTANCE_ID = os.environ["INSTANCE_ID"]
S3_BUCKET = os.environ["S3_BUCKET"]
RUNBOOK_KEY = os.environ.get("RUNBOOK_KEY", "runbooks/runbook.md")
DYNAMODB_TABLE = os.environ["DYNAMODB_TABLE"]
ANTHROPIC_MODEL = os.environ["ANTHROPIC_MODEL"]
ANTHROPIC_API_KEY_PARAM = os.environ["ANTHROPIC_API_KEY_PARAM"]
ESCALATION_TOPIC_ARN = os.environ["ESCALATION_TOPIC_ARN"]
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
POST_ACTION_WAIT_SECONDS = int(os.environ.get("POST_ACTION_WAIT_SECONDS", "120"))

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

logs_client = boto3.client("logs")
cloudwatch_client = boto3.client("cloudwatch")
s3_client = boto3.client("s3")
ssm_client = boto3.client("ssm")
sns_client = boto3.client("sns")
dynamodb_table = boto3.resource("dynamodb").Table(DYNAMODB_TABLE)


def _load_anthropic_api_key() -> str:
    response = ssm_client.get_parameter(Name=ANTHROPIC_API_KEY_PARAM, WithDecryption=True)
    return response["Parameter"]["Value"]


# Fetched once per cold start, like the boto3 clients above -- every warm invocation reuses it.
ANTHROPIC_API_KEY = _load_anthropic_api_key()


def now_iso() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def call_claude(alarm_name: str, alarm_reason: str, logs: str, metrics: str, runbook: str):
    """Returns (verdict_dict, None) on success, or (None, error_message) on any failure."""
    user_prompt = build_user_prompt(alarm_name, alarm_reason, logs, metrics, runbook)
    payload = json.dumps(
        {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 1024,
            "temperature": 0.0,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=payload,
        method="POST",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        return None, f"Anthropic API returned HTTP {exc.code}: {detail}"
    except urllib.error.URLError as exc:
        return None, f"failed to reach the Anthropic API: {exc.reason}"

    raw_text = body["content"][0]["text"].strip()

    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.lower().startswith("json"):
            raw_text = raw_text[4:].strip()

    try:
        verdict = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return None, f"model did not return valid JSON: {exc}. raw={raw_text[:500]!r}"

    error = validate_verdict(verdict)
    if error:
        return None, f"model returned invalid verdict: {error}. raw={raw_text[:500]!r}"

    return verdict, None


def get_recent_audit_items(alarm_name: str) -> list:
    response = dynamodb_table.query(
        KeyConditionExpression="alarm_name = :a",
        ExpressionAttributeValues={":a": alarm_name},
        ScanIndexForward=False,
        Limit=20,
    )
    return response.get("Items", [])


def write_audit_log(incident_id, alarm_name, verdict, decision, decision_reason, action_result, recovered):
    dynamodb_table.put_item(
        Item={
            "alarm_name": alarm_name,
            "timestamp": now_iso(),
            "incident_id": incident_id,
            "verdict": verdict,
            "decision": decision,
            "decision_reason": decision_reason,
            "action_result": action_result,
            "recovered": recovered,
            "dry_run": DRY_RUN,
        }
    )


def write_rca(incident_id, alarm_name, alarm_reason, verdict, decision, decision_reason, action_result, recovered):
    verdict = verdict or {}
    lines = [
        f"# RCA: {alarm_name}",
        "",
        f"- Incident ID: {incident_id}",
        f"- Time: {now_iso()}",
        f"- Alarm reason: {alarm_reason}",
        f"- Decision: {decision} ({decision_reason})",
        f"- Recovered: {recovered}",
        f"- Dry run: {DRY_RUN}",
        "",
        "## Verdict",
        f"- Summary: {verdict.get('summary', 'n/a')}",
        f"- Root cause: {verdict.get('root_cause', 'n/a')}",
        f"- Confidence: {verdict.get('confidence', 'n/a')}",
        f"- Action: {verdict.get('action', 'n/a')}",
        f"- Risk: {verdict.get('risk', 'n/a')}",
        "- Evidence:",
    ]
    for item in verdict.get("evidence") or ["(none provided)"]:
        lines.append(f"  - {item}")
    lines += ["", "## Action result", "```", json.dumps(action_result, indent=2, default=str), "```"]

    key = f"rca/{alarm_name}/{now_iso().replace(':', '-')}-{incident_id}.md"
    s3_client.put_object(
        Bucket=S3_BUCKET, Key=key, Body="\n".join(lines).encode("utf-8"), ContentType="text/markdown"
    )
    return key


def escalate(alarm_name: str, reason: str, verdict):
    sns_client.publish(
        TopicArn=ESCALATION_TOPIC_ARN,
        Subject=f"[AutoTriage] Human review needed: {alarm_name}",
        Message=json.dumps({"alarm_name": alarm_name, "reason": reason, "verdict": verdict}, indent=2, default=str),
    )


def alarm_recovered(alarm_name: str) -> bool:
    response = cloudwatch_client.describe_alarms(AlarmNames=[alarm_name])
    alarms = response.get("MetricAlarms", [])
    if not alarms:
        return False
    return alarms[0]["StateValue"] == "OK"


def handle_alarm(alarm_name: str, alarm_reason: str) -> dict:
    incident_id = str(uuid.uuid4())

    logs = get_recent_logs(logs_client, LOG_GROUP_NAME)
    metrics = get_recent_metrics(cloudwatch_client, INSTANCE_ID)
    runbook = get_runbook(s3_client, S3_BUCKET, RUNBOOK_KEY)

    verdict, error = call_claude(alarm_name, alarm_reason, logs, metrics, runbook)
    if error:
        logger.error(error)
        write_audit_log(incident_id, alarm_name, {"error": error}, "escalate", error, None, False)
        write_rca(incident_id, alarm_name, alarm_reason, None, "escalate", error, None, False)
        escalate(alarm_name, error, None)
        return {"incident_id": incident_id, "decision": "escalate", "reason": error}

    recent_items = get_recent_audit_items(alarm_name)
    rate_limited = is_rate_limited(recent_items)
    decision, decision_reason = decide(verdict, rate_limited)

    action_result = None
    recovered = False

    if decision == "auto":
        action_result = run_action(ssm_client, verdict["action"], INSTANCE_ID, DRY_RUN)
        if not DRY_RUN:
            time.sleep(POST_ACTION_WAIT_SECONDS)
            recovered = alarm_recovered(alarm_name)
            if not recovered:
                escalate(alarm_name, "auto-remediation did not clear the alarm within the wait window", verdict)
        else:
            recovered = None  # DRY_RUN: nothing was actually executed
    else:
        escalate(alarm_name, decision_reason, verdict)

    write_audit_log(incident_id, alarm_name, verdict, decision, decision_reason, action_result, recovered)
    rca_key = write_rca(
        incident_id, alarm_name, alarm_reason, verdict, decision, decision_reason, action_result, recovered
    )

    return {
        "incident_id": incident_id,
        "decision": decision,
        "decision_reason": decision_reason,
        "action_result": action_result,
        "recovered": recovered,
        "rca_key": rca_key,
    }


def lambda_handler(event, context):
    results = []
    for record in event.get("Records", []):
        message = json.loads(record["Sns"]["Message"])
        alarm_name = message.get("AlarmName", "unknown-alarm")
        alarm_reason = message.get("NewStateReason", "")
        logger.info(json.dumps({"event": "alarm_received", "alarm_name": alarm_name}))
        try:
            result = handle_alarm(alarm_name, alarm_reason)
        except Exception as exc:  # noqa: BLE001 - never let a triage bug crash past the handler unlogged
            logger.exception("triage_failed")
            escalate(alarm_name, f"triage lambda raised an exception: {exc}", None)
            result = {"decision": "escalate", "reason": str(exc)}
        results.append(result)
    return {"results": results}
