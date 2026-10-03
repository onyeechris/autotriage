"""Policy gate: decides whether a verdict can be auto-remediated, needs human approval, or must escalate.

Guardrails encoded here (see AUTOTRIAGE.md):
- only restart_container / prune_disk are ever auto-run, and only via pre-defined SSM documents
- auto-run requires confidence >= 0.8 AND risk == "low"
- at most one auto-remediation per alarm per 30 minutes, to avoid fix loops
- invalid or unrecognized LLM output always escalates -- never guess
"""
import datetime as dt
import os

ALLOWLISTED_ACTIONS = {"restart_container", "prune_disk"}
CONFIDENCE_THRESHOLD = 0.8
RATE_LIMIT_WINDOW_MINUTES = int(os.environ.get("RATE_LIMIT_WINDOW_MINUTES", "30"))

REQUIRED_KEYS = {"summary", "root_cause", "confidence", "action", "risk", "evidence"}
VALID_RISK = {"low", "medium", "high"}


def validate_verdict(verdict) -> str | None:
    """Returns an error string if the verdict is malformed, else None."""
    if not isinstance(verdict, dict):
        return "verdict is not a JSON object"
    missing = REQUIRED_KEYS - verdict.keys()
    if missing:
        return f"missing keys: {sorted(missing)}"
    if verdict["action"] not in ALLOWLISTED_ACTIONS | {"none"}:
        return f"unknown action: {verdict['action']!r}"
    confidence = verdict["confidence"]
    if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        return f"confidence out of range: {confidence!r}"
    if verdict["risk"] not in VALID_RISK:
        return f"unknown risk: {verdict['risk']!r}"
    return None


def decide(verdict: dict, rate_limited: bool) -> tuple[str, str]:
    """Returns (decision, reason). decision is one of: auto, approval, escalate."""
    action = verdict["action"]
    confidence = float(verdict["confidence"])
    risk = verdict["risk"]

    if action == "none":
        return "escalate", "model recommended no automated action"

    if action not in ALLOWLISTED_ACTIONS:
        return "escalate", f"action {action!r} is not allowlisted"

    if rate_limited:
        return "approval", "auto-remediation rate limit reached for this alarm in the last 30 minutes"

    if confidence >= CONFIDENCE_THRESHOLD and risk == "low":
        return "auto", "confidence and risk within policy for automatic remediation"

    return "approval", f"confidence={confidence} risk={risk} does not meet auto-remediation policy"


def is_rate_limited(recent_items: list[dict], now: dt.datetime | None = None) -> bool:
    """recent_items: prior audit log items for this alarm, each with 'decision', 'dry_run' and ISO 'timestamp'."""
    now = now or dt.datetime.utcnow()
    cutoff = now - dt.timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
    for item in recent_items:
        # Dry-run "auto" decisions never ran an action, so they must not block a real remediation.
        if item.get("decision") != "auto" or item.get("dry_run"):
            continue
        ts = item.get("timestamp")
        if not ts:
            continue
        try:
            item_time = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
        if item_time >= cutoff:
            return True
    return False
