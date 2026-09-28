# This module holds the actual text we send to Claude -- the "prompt". Keeping it in its own
# file (instead of mixed into handler.py) makes it easy to find and tweak the wording without
# reading through any of the AWS/networking code.

SYSTEM_PROMPT = """You are AutoTriage, an on-call SRE assistant for a small FastAPI service running in a single \
Docker container on one EC2 instance. You are given a CloudWatch alarm, the last 15 minutes of application logs, \
recent CPU/disk metrics, and the team's runbook. Your job is to diagnose the root cause and recommend ONE safe \
action.

Rules:
- Respond with ONLY a single JSON object. No markdown fences, no prose before or after it.
- The JSON object MUST have exactly these keys: summary, root_cause, confidence, action, risk, evidence.
- "action" MUST be one of: "restart_container", "prune_disk", "none". Never invent a new action.
- "confidence" is a float between 0.0 and 1.0 reflecting how sure you are of the root cause AND that the action \
fixes it.
- "risk" MUST be one of: "low", "medium", "high" -- the risk of running "action" on this system right now.
- "evidence" is a short list of strings citing the specific log lines or metric values that support your diagnosis.
- If the logs/metrics are inconclusive, ambiguous, or do not match a known runbook scenario, set action to "none" \
and confidence low -- do NOT guess an action you are not confident about.
- Only recommend "restart_container" or "prune_disk" if the runbook describes that fix for this exact symptom.
"""
# SYSTEM_PROMPT is a plain string variable (written in ALL_CAPS by convention, to signal "this is
# a constant", though Python itself doesn't actually stop you from changing it later). The triple
# double-quotes (""") let a string span many lines instead of needing one line per sentence. A
# backslash \ at the very end of a source line tells Python "don't insert a newline here, glue
# this line onto the next one" -- that's purely a formatting trick, so the finished text doesn't
# have odd mid-sentence line breaks, while this SOURCE FILE still stays readable at a normal
# width. This whole block gets sent to Claude before every request, as its standing instructions
# -- it's what keeps every response in the same strict JSON shape, no matter which incident it's
# looking at.


def build_user_prompt(alarm_name: str, alarm_reason: str, logs: str, metrics: str, runbook: str) -> str:
    # "def" starts a function definition; "build_user_prompt" is its name. Each parameter in the
    # parentheses is an input the caller must supply. The ": str" after each one is a "type hint"
    # -- a note for humans/editors saying "this should be text"; Python itself won't stop you
    # from passing something else. "-> str" after the closing parenthesis says this function's
    # output will also be text.
    return f"""## CloudWatch Alarm
name: {alarm_name}
reason: {alarm_reason}

## Application logs (last 15 minutes)
{logs or "(no matching log lines found)"}

## Metrics
{metrics}

## Runbook
{runbook}

Diagnose the root cause and return the JSON verdict now.
"""
    # The f right before the opening """ makes this an "f-string": anything inside {curly braces}
    # is evaluated as a real Python expression and its result is dropped into the string.
    # `logs or "(no matching log lines found)"` is a common shortcut: Python treats an empty
    # string (or None) as "falsy", so if `logs` is empty it uses the text on the right instead;
    # otherwise it uses `logs` itself. The "##" lines are just markdown headings -- plain text as
    # far as Python is concerned, included so the finished prompt is easy for a human (or the
    # model) to visually scan.
