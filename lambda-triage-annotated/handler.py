"""SNS-triggered triage Lambda: gather context -> ask Claude -> policy gate -> act -> verify -> log."""
# A module docstring: a one-sentence description of this entire file's job.

import datetime as dt
# Built-in module for working with dates/times; nicknamed "dt" so we can write dt.datetime
# instead of the longer datetime.datetime everywhere below.
import json
# Built-in module for converting between Python objects (dicts, lists, etc.) and JSON text --
# used constantly here, since AWS's APIs, Claude's response, and our own log lines are all JSON.
import logging
# Built-in module for structured "print statements" that integrate with CloudWatch Logs.
import os
# Built-in module used here to read environment variables (settings injected by Terraform).
import time
# Built-in module for pausing execution (time.sleep) and reading the clock.
import urllib.error
# Part of Python's built-in HTTP toolkit -- specifically, the exception types it can raise.
import urllib.request
# The other half of that toolkit -- for actually making an HTTP request (here, to Anthropic's API).
import uuid
# Built-in module for generating universally unique IDs, one per incident, so two different
# triage runs never accidentally share an identifier.
from decimal import Decimal
# Decimal is an exact number type from Python's standard library. DynamoDB needs it for
# non-integer numbers: boto3 refuses plain floats (see to_dynamo_safe below).

import boto3
# The AWS SDK for Python. This is a third-party library (not part of core Python), but it comes
# pre-installed in the Lambda runtime, so it doesn't need to be bundled with our own code.

from actions import run_action
# Import one specific function, run_action, from our own actions.py file sitting next to this one.
from context import get_recent_logs, get_recent_metrics, get_runbook
# Import three functions from our own context.py file.
from policy import decide, is_rate_limited, validate_verdict
# Import three functions from our own policy.py file.
from prompts import SYSTEM_PROMPT, build_user_prompt
# Import one constant and one function from our own prompts.py file.

logger = logging.getLogger()
# Get the "root" logger object -- the one Lambda automatically wires up to CloudWatch Logs.
logger.setLevel(logging.INFO)
# Only show log messages at INFO severity or more serious (INFO, WARNING, ERROR...); ignore
# anything more verbose, like DEBUG.

LOG_GROUP_NAME = os.environ["LOG_GROUP_NAME"]
# Read an environment variable by name using square brackets -- this CRASHES immediately if the
# variable isn't set. That's intentional: better to fail loudly the moment the Lambda starts than
# to fail confusingly partway through processing a real alarm.
INSTANCE_ID = os.environ["INSTANCE_ID"]
S3_BUCKET = os.environ["S3_BUCKET"]
RUNBOOK_KEY = os.environ.get("RUNBOOK_KEY", "runbooks/runbook.md")
# Unlike the ones above, this uses .get() with a fallback default, so it's optional to set.
DYNAMODB_TABLE = os.environ["DYNAMODB_TABLE"]
ANTHROPIC_MODEL = os.environ["ANTHROPIC_MODEL"]
ANTHROPIC_API_KEY_PARAM = os.environ["ANTHROPIC_API_KEY_PARAM"]
# Note this holds the NAME of an SSM Parameter Store parameter, not the API key itself -- the
# real key is fetched separately below, so it never sits in a plain environment variable.
ESCALATION_TOPIC_ARN = os.environ["ESCALATION_TOPIC_ARN"]
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
# Environment variables are ALWAYS plain text, even for something conceptually true/false, so we
# read it as text (defaulting to "true"), lowercase it defensively, and compare it to the string
# "true" -- the whole expression evaluates to a real Python boolean, True or False.
POST_ACTION_WAIT_SECONDS = int(os.environ.get("POST_ACTION_WAIT_SECONDS", "120"))
# Same idea: read as text (default "120"), then convert that text into a whole number with int(...).

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
# The web address we'll send requests to when asking Claude for a triage verdict.
ANTHROPIC_API_VERSION = "2023-06-01"
# A version string Anthropic's API requires in a request header, so it knows which version of the
# API's behavior to use when handling our request.

logs_client = boto3.client("logs")
# Create a "client" object -- basically a remote control -- for talking to CloudWatch Logs.
cloudwatch_client = boto3.client("cloudwatch")
# A client for CloudWatch's metrics and alarms features (a separate service from Logs, despite
# the shared "CloudWatch" name).
s3_client = boto3.client("s3")
# A client for S3 (AWS's file storage service).
ssm_client = boto3.client("ssm")
# A client for Systems Manager -- used both to run remote commands AND, further down, to read the
# Anthropic API key out of Parameter Store.
sns_client = boto3.client("sns")
# A client for SNS, the notification/messaging service used to send escalation emails.
dynamodb_table = boto3.resource("dynamodb").Table(DYNAMODB_TABLE)
# boto3.resource("dynamodb") gives a higher-level DynamoDB object (as opposed to .client(), which
# is more low-level); .Table(...) then points it at our one specific table by name.

# Every line above this comment runs exactly ONCE per Lambda "cold start" -- the first time AWS
# spins up this function. Any later invocation on the same still-warm container skips straight to
# whichever function AWS calls (lambda_handler, at the very bottom of this file), reusing these
# same client objects and environment values without re-creating them. This is a standard,
# recommended Lambda performance pattern.


def _load_anthropic_api_key() -> str:
    # A leading underscore in a function's name is a Python convention meaning "this is an
    # internal helper, not meant to be imported and used from other files."
    response = ssm_client.get_parameter(Name=ANTHROPIC_API_KEY_PARAM, WithDecryption=True)
    # Ask Parameter Store for the parameter by name. WithDecryption=True is required because the
    # value is stored as an encrypted "SecureString" -- without this flag, AWS refuses to hand
    # back the real, decrypted value.
    return response["Parameter"]["Value"]
    # Dig into the nested response dictionary to pull out just the actual secret text.


# Fetched once per cold start, like the boto3 clients above -- every warm invocation reuses it.
ANTHROPIC_API_KEY = _load_anthropic_api_key()
# Call the function immediately (note the parentheses) and store its result in a constant, at
# import time -- so the API key is already sitting ready before any alarm ever comes in.


def now_iso() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    # dt.datetime.utcnow() gets the current date/time in UTC (not the server's local timezone,
    # which would make timestamps ambiguous across regions). .strftime(...) formats that moment
    # into a text string following the given pattern -- this particular pattern is "ISO 8601", a
    # standard, sortable date format like "2026-09-28T14:03:00Z".


def call_claude(alarm_name: str, alarm_reason: str, logs: str, metrics: str, runbook: str):
    # No "-> ..." return type hint here, because this function can hand back two different shapes
    # depending on success or failure (see the docstring below) -- Python allows leaving it off.
    """Returns (verdict_dict, None) on success, or (None, error_message) on any failure."""
    user_prompt = build_user_prompt(alarm_name, alarm_reason, logs, metrics, runbook)
    # Call our helper from prompts.py to assemble the big block of text describing this incident.
    payload = json.dumps(
        {
            "model": ANTHROPIC_MODEL,
            # Which Claude model to use, e.g. "claude-3-5-sonnet-20241022".
            "max_tokens": 1024,
            # The maximum length of Claude's reply, measured in "tokens" (roughly, word pieces).
            "temperature": 0.0,
            # How random/creative the model's output should be; 0.0 means "as deterministic and
            # consistent as possible", which is what we want for a machine-parsed JSON answer.
            "system": SYSTEM_PROMPT,
            # The standing instructions imported from prompts.py.
            "messages": [{"role": "user", "content": user_prompt}],
            # The actual conversation: a list containing one message, from the "user" role,
            # containing the incident details we just built above.
        }
    ).encode("utf-8")
    # json.dumps(...) converts the Python dictionary above into a JSON text string. .encode("utf-8")
    # then converts that text into raw bytes, since that's what an HTTP request body has to be.

    request = urllib.request.Request(
        # Build a description of the HTTP request we want to send (this line doesn't send it yet,
        # it just constructs the object).
        ANTHROPIC_API_URL,
        # The web address to send it to.
        data=payload,
        # The request body -- the bytes we just built above.
        method="POST",
        # The HTTP verb: POST means "here's some data, please process it" (as opposed to GET,
        # which just asks for data back without sending any).
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            # Anthropic's way of checking who's calling -- effectively a password for this API.
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
            # Tells the server "the data I'm sending you is JSON text."
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            # Actually send the request over the network and wait for a response, giving up after
            # 30 seconds if nothing comes back. "with ... as resp" is a "context manager" -- it
            # guarantees the network connection is cleanly closed afterward, even if something
            # inside this block goes wrong.
            body = json.loads(resp.read().decode("utf-8"))
            # resp.read() gets the raw response bytes; .decode("utf-8") turns them into text;
            # json.loads(...) parses that text back into a Python dictionary.
    except urllib.error.HTTPError as exc:
        # Raised when the server DOES respond, but with an error status code (like 401
        # Unauthorized or 500 Internal Server Error).
        detail = exc.read().decode("utf-8", "replace")[:500]
        # Read whatever error details the server sent back. The "replace" option tells .decode()
        # to swap out any bytes it can't understand instead of crashing on them. [:500] keeps only
        # the first 500 characters, so a huge error page can't bloat our logs.
        return None, f"Anthropic API returned HTTP {exc.code}: {detail}"
        # Return the "failure" shape described in the docstring: (None, error message).
    except urllib.error.URLError as exc:
        # A broader networking failure -- e.g. no connectivity, DNS lookup failed, etc. (HTTPError
        # is actually a more specific subtype of URLError, which is why it's listed first: Python
        # checks except clauses in order, top to bottom, and uses the first one that matches.)
        return None, f"failed to reach the Anthropic API: {exc.reason}"

    raw_text = body["content"][0]["text"].strip()
    # Anthropic's response puts the model's reply inside body["content"], a list of content
    # blocks; [0] grabs the first (and here, only) one; ["text"] reads its text; .strip() trims
    # any leading/trailing whitespace or blank lines.

    if raw_text.startswith("```"):
        # Sometimes models wrap their JSON answer in a markdown "code fence" even when told not
        # to. .startswith(...) checks whether the text begins with three backtick characters.
        raw_text = raw_text.strip("`")
        # .strip("`") removes backtick characters from BOTH ends of the string (not the middle).
        if raw_text.lower().startswith("json"):
            # Code fences are sometimes written as ```json -- after stripping the backticks, that
            # would leave a leading "json" word behind to clean up too. .lower() makes this check
            # case-insensitive.
            raw_text = raw_text[4:].strip()
            # [4:] is a "slice": it keeps everything FROM index 4 onward, dropping the first 4
            # characters (the word "json"). .strip() then cleans up any leftover whitespace.

    try:
        verdict = json.loads(raw_text)
        # Attempt to parse the (hopefully cleaned-up) text as JSON, turning it into a Python dict.
    except json.JSONDecodeError as exc:
        # Raised if the text isn't valid JSON at all.
        return None, f"model did not return valid JSON: {exc}. raw={raw_text[:500]!r}"
        # Include a snippet of the actual raw text in the error (capped at 500 characters), to
        # help debug what went wrong later without flooding the logs.

    error = validate_verdict(verdict)
    # Hand the parsed dictionary to our policy.py function, which checks it has the right shape.
    if error:
        # validate_verdict returns None when everything's fine, or a non-empty error string when
        # it's not -- a non-empty string is "truthy" in Python, so this only runs on a real problem.
        return None, f"model returned invalid verdict: {error}. raw={raw_text[:500]!r}"

    return verdict, None
    # Everything checked out: return the "success" shape described in the docstring.


def get_recent_audit_items(alarm_name: str) -> list:
    response = dynamodb_table.query(
        # .query() asks DynamoDB for items matching a condition -- more efficient than scanning
        # the entire table.
        KeyConditionExpression="alarm_name = :a",
        # The condition itself: "alarm_name must equal whatever :a turns out to be" -- :a is a
        # placeholder, filled in by the next argument.
        ExpressionAttributeValues={":a": alarm_name},
        # Fills in the placeholder: :a becomes the actual alarm_name we were given.
        ScanIndexForward=False,
        # False means "sort results newest-first" (DynamoDB sorts by the table's sort key, which
        # for this table is the item's timestamp).
        Limit=20,
        # Only fetch, at most, the 20 most recent matching items -- we don't need this alarm's
        # entire history just to check for recent rate-limiting.
    )
    return response.get("Items", [])
    # DynamoDB's response is a dict with an "Items" key holding the matches; default to an empty
    # list if, for some reason, that key isn't present at all.


def to_dynamo_safe(value):
    # Converts a value so DynamoDB will accept it. boto3's DynamoDB resource rejects Python floats
    # with "TypeError: Float types are not supported", and Claude's verdict contains a float
    # ("confidence", e.g. 0.85). This crashed every triage run until it was fixed.
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)
    # Step 1: json.dumps turns the value into JSON text (default=str turns anything unusual into
    # text too). Step 2: json.loads turns that text back into Python objects, but with
    # parse_float=Decimal, so every decimal number comes back as an exact Decimal instead of a float.


def write_audit_log(incident_id, alarm_name, verdict, decision, decision_reason, action_result, recovered):
    # No type hints on these parameters -- Python doesn't require them; they're purely optional
    # documentation for humans (and some tools). This function takes several separate pieces of
    # information describing one incident.
    dynamodb_table.put_item(
        # .put_item() writes (or fully overwrites) one row/item into the table.
        Item={
            "alarm_name": alarm_name,
            # The partition key (primary identifier) for this table.
            "timestamp": now_iso(),
            # The sort key: calling our now_iso() helper produces a fresh, current timestamp for
            # exactly when this record is being written.
            "incident_id": incident_id,
            "verdict": to_dynamo_safe(verdict),
            # The whole verdict object (or the {"error": ...} dict used on failure) is stored as a
            # nested map. Passed through to_dynamo_safe() first so its floats become Decimals.
            "decision": decision,
            "decision_reason": decision_reason,
            "action_result": action_result,
            "recovered": recovered,
            "dry_run": DRY_RUN,
        }
    )


def write_rca(incident_id, alarm_name, alarm_reason, verdict, decision, decision_reason, action_result, recovered):
    verdict = verdict or {}
    # If verdict is None (which happens when call_claude failed before ever producing one), swap
    # it for an empty dict instead, so the .get() calls just below don't crash on None.
    lines = [
        # Build the RCA report as a list of separate text lines, which we'll glue together into
        # one string at the very end. Building a list first (instead of growing one giant string
        # with += over and over) is a common, efficient pattern in Python.
        f"# RCA: {alarm_name}",
        # An f-string: the "#" here is literal markdown heading syntax (not a Python comment) --
        # inside a string, # has no special meaning to Python at all.
        "",
        # An empty string, which produces a blank line once everything is joined together later.
        f"- Incident ID: {incident_id}",
        f"- Time: {now_iso()}",
        f"- Alarm reason: {alarm_reason}",
        f"- Decision: {decision} ({decision_reason})",
        f"- Recovered: {recovered}",
        f"- Dry run: {DRY_RUN}",
        "",
        "## Verdict",
        f"- Summary: {verdict.get('summary', 'n/a')}",
        # .get(key, default) reads a dictionary key safely, falling back to "n/a" for display if
        # the verdict dict doesn't have that key (e.g. because it's the empty {} from a failure).
        f"- Root cause: {verdict.get('root_cause', 'n/a')}",
        f"- Confidence: {verdict.get('confidence', 'n/a')}",
        f"- Action: {verdict.get('action', 'n/a')}",
        f"- Risk: {verdict.get('risk', 'n/a')}",
        "- Evidence:",
    ]
    for item in verdict.get("evidence") or ["(none provided)"]:
        # verdict.get("evidence") returns None if there's no "evidence" key (or an empty list is
        # also treated as falsy by "or"); either way, we fall back to a one-item placeholder list,
        # so the loop below still runs once with a sensible message instead of doing nothing.
        lines.append(f"  - {item}")
        # .append() adds one new item onto the end of the existing "lines" list, in place.
    lines += ["", "## Action result", "```", json.dumps(action_result, indent=2, default=str), "```"]
    # += on a list means "extend this list with all the items from that other list" (different
    # from += on numbers, which just adds). json.dumps(..., indent=2) pretty-prints the action
    # result dict with 2-space indentation; default=str tells it "if you hit a value you don't
    # already know how to turn into JSON, just convert it to plain text instead of crashing."

    key = f"rca/{alarm_name}/{now_iso().replace(':', '-')}-{incident_id}.md"
    # Build the S3 object key (essentially, its file path). .replace(':', '-') swaps colons for
    # dashes, because a colon in a timestamp looks odd inside an S3 key/URL, even though it's
    # technically legal.
    s3_client.put_object(
        Bucket=S3_BUCKET, Key=key, Body="\n".join(lines).encode("utf-8"), ContentType="text/markdown"
    )
    # Upload the file: "\n".join(lines) glues our list of text lines into one big string with real
    # newline characters between them; .encode("utf-8") turns that into bytes, since S3 objects
    # are stored as raw bytes. ContentType tells anything that later downloads this file "treat
    # this as a markdown document."
    return key
    # Hand back the S3 key, so the caller can include it in this function's own return value.


def escalate(alarm_name: str, reason: str, verdict):
    sns_client.publish(
        # Send a message out through SNS -- anyone subscribed to this topic (here, an email
        # address) receives it.
        TopicArn=ESCALATION_TOPIC_ARN,
        Subject=f"[AutoTriage] Human review needed: {alarm_name}",
        # The email's subject line.
        Message=json.dumps({"alarm_name": alarm_name, "reason": reason, "verdict": verdict}, indent=2, default=str),
        # The email's body: a small dictionary describing the situation, converted into nicely
        # indented JSON text so a human can read it easily.
    )


def alarm_recovered(alarm_name: str) -> bool:
    response = cloudwatch_client.describe_alarms(AlarmNames=[alarm_name])
    # Ask CloudWatch for the current details of one specific alarm (again, the API wants a LIST of
    # names, even though we're only asking about one here).
    alarms = response.get("MetricAlarms", [])
    # Pull the list of matching alarms out of the response, defaulting to an empty list.
    if not alarms:
        # An empty list is falsy, so this triggers if, unexpectedly, no alarm was found at all.
        return False
    return alarms[0]["StateValue"] == "OK"
    # alarms[0] is the first (and only) alarm in the list; check whether its current state is
    # exactly the literal string "OK" -- CloudWatch's way of saying "not currently alarming".


def handle_alarm(alarm_name: str, alarm_reason: str) -> dict:
    incident_id = str(uuid.uuid4())
    # uuid.uuid4() generates a random, unique ID (as a special object); str(...) converts it into
    # its normal text form, something like "3f2c1a8e-...".

    logs = get_recent_logs(logs_client, LOG_GROUP_NAME)
    # Call our context.py helper to fetch the last 15 minutes of application logs.
    metrics = get_recent_metrics(cloudwatch_client, INSTANCE_ID)
    # Fetch recent CPU/disk/heartbeat metrics.
    runbook = get_runbook(s3_client, S3_BUCKET, RUNBOOK_KEY)
    # Fetch the runbook text from S3.

    verdict, error = call_claude(alarm_name, alarm_reason, logs, metrics, runbook)
    # Call Claude. Since call_claude always returns exactly two values, we can "unpack" them
    # straight into two separate variables in one line.
    if error:
        # If call_claude reported a problem (in which case verdict will be None)...
        logger.error(error)
        # Write the error message to CloudWatch Logs at ERROR severity.
        write_audit_log(incident_id, alarm_name, {"error": error}, "escalate", error, None, False)
        # Log this incident anyway, even though we never got a real verdict, so there's still a
        # permanent record that something happened.
        write_rca(incident_id, alarm_name, alarm_reason, None, "escalate", error, None, False)
        # Write an RCA report too, for the same reason.
        escalate(alarm_name, error, None)
        # Notify a human that this incident needs their attention.
        return {"incident_id": incident_id, "decision": "escalate", "reason": error}
        # Stop here (return) -- nothing below this point can run without a real verdict to act on.

    recent_items = get_recent_audit_items(alarm_name)
    # Look up this alarm's recent history from DynamoDB.
    rate_limited = is_rate_limited(recent_items)
    # Ask policy.py whether an automatic fix already ran recently for this same alarm.
    decision, decision_reason = decide(verdict, rate_limited)
    # Ask policy.py what to do next: "auto", "approval", or "escalate", plus why.

    action_result = None
    # Start with "nothing has happened yet" -- this gets filled in below only if we actually act.
    recovered = False
    # Start with "assume not recovered" as a safe default.

    if decision == "auto":
        action_result = run_action(ssm_client, verdict["action"], INSTANCE_ID, DRY_RUN)
        # Call actions.py to actually run (or, if DRY_RUN, just simulate) the remediation.
        if not DRY_RUN:
            # Only actually wait and re-check the alarm if we really sent a command.
            time.sleep(POST_ACTION_WAIT_SECONDS)
            # Pause (e.g. 120 seconds) to give the fix time to take effect and the alarm time to
            # re-evaluate.
            recovered = alarm_recovered(alarm_name)
            # Check whether the alarm has actually cleared.
            if not recovered:
                escalate(alarm_name, "auto-remediation did not clear the alarm within the wait window", verdict)
                # If it's still not OK, don't just assume it'll sort itself out -- get a human
                # involved.
        else:
            recovered = None
            # DRY_RUN: nothing was actually executed, so "recovered" isn't a meaningful True/False
            # here -- None means "not applicable / unknown", distinct from an actual False.
    else:
        escalate(alarm_name, decision_reason, verdict)
        # Any decision other than "auto" (i.e. "approval" or "escalate") always notifies a human.

    write_audit_log(incident_id, alarm_name, verdict, decision, decision_reason, action_result, recovered)
    # Record everything that happened in DynamoDB, no matter which path above was taken.
    rca_key = write_rca(
        incident_id, alarm_name, alarm_reason, verdict, decision, decision_reason, action_result, recovered
    )
    # Also always write a human-readable RCA report to S3.

    return {
        "incident_id": incident_id,
        "decision": decision,
        "decision_reason": decision_reason,
        "action_result": action_result,
        "recovered": recovered,
        "rca_key": rca_key,
    }
    # Hand back a summary dict describing exactly what this function did, for logging/debugging.


def lambda_handler(event, context):
    # This is the exact function name AWS Lambda is configured to call (see
    # terraform/lambda.tf's `handler = "handler.lambda_handler"`). AWS always calls it with two
    # arguments: "event" (the data that triggered this run) and "context" (metadata about this
    # particular Lambda execution, which this function doesn't happen to use).
    results = []
    # We'll collect one result dictionary per alarm processed, in case SNS ever batches more than
    # one notification into a single Lambda call.
    for record in event.get("Records", []):
        # SNS-triggered Lambda events always arrive wrapped in a list under the "Records" key,
        # even when there's only a single notification.
        message = json.loads(record["Sns"]["Message"])
        # The actual CloudWatch Alarm details are themselves JSON text, nested inside SNS's own
        # wrapper structure -- so we have to parse it a second time here.
        alarm_name = message.get("AlarmName", "unknown-alarm")
        # Pull the alarm's name out, with a fallback in case the field is ever missing.
        alarm_reason = message.get("NewStateReason", "")
        # Pull out CloudWatch's own explanation of why the alarm changed state.
        if message.get("NewStateValue", "ALARM") != "ALARM":
            # The alarms also publish their OK transitions (recoveries) to this same topic. Those
            # are not incidents, so skip them rather than starting a full triage run. The default
            # of "ALARM" means a message without the field is still treated as an incident.
            logger.info(json.dumps({"event": "alarm_ignored", "alarm_name": alarm_name, "state": message.get("NewStateValue")}))
            results.append({"decision": "ignored", "reason": f"state {message.get('NewStateValue')} is not ALARM"})
            continue
            # "continue" jumps to the next SNS record without running the triage steps below.
        logger.info(json.dumps({"event": "alarm_received", "alarm_name": alarm_name}))
        # Log a small structured JSON line noting that we've started processing this alarm --
        # useful later for tracing what happened, when reading through CloudWatch Logs.
        try:
            result = handle_alarm(alarm_name, alarm_reason)
            # Do the actual work: gather context, ask Claude, decide, maybe act, log, report.
        except Exception as exc:  # noqa: BLE001 - never let a triage bug crash past the handler unlogged
            # A deliberately broad safety net: if ANYTHING above raises an unexpected error (a
            # bug, an AWS outage, anything), we still want to notify a human rather than have this
            # whole Lambda invocation just fail silently with no record of what happened.
            logger.exception("triage_failed")
            # logger.exception() logs the message AND automatically includes the full error
            # traceback, which is extremely useful when debugging this later.
            escalate(alarm_name, f"triage lambda raised an exception: {exc}", None)
            result = {"decision": "escalate", "reason": str(exc)}
            # Build a fallback result dict, so the rest of this loop can carry on normally.
        results.append(result)
        # Add this alarm's outcome onto our running list, whichever branch above produced it.
    return {"results": results}
    # Lambda doesn't strictly require a return value for this trigger type, but returning a
    # summary is useful whenever you're reading invocation logs or testing this by hand from the
    # AWS console.
