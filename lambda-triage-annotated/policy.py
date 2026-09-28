"""Policy gate: decides whether a verdict can be auto-remediated, needs human approval, or must escalate.

Guardrails encoded here (see AUTOTRIAGE.md):
- only restart_container / prune_disk are ever auto-run, and only via pre-defined SSM documents
- auto-run requires confidence >= 0.8 AND risk == "low"
- at most one auto-remediation per alarm per 30 minutes, to avoid fix loops
- invalid or unrecognized LLM output always escalates -- never guess
"""
# Everything between the triple quotes at the very top of the file is a "module docstring" -- a
# special kind of comment that documents what this whole file is for. Tools (and other
# developers) can read it without opening the file, e.g. via help(policy) in a Python shell.

import datetime as dt
# "import" brings in code someone else already wrote so we can reuse it. "datetime" is one of
# Python's built-in modules, for working with dates and times. "as dt" gives it a short nickname
# so we can write dt.datetime instead of datetime.datetime everywhere below.

ALLOWLISTED_ACTIONS = {"restart_container", "prune_disk"}
# A variable holding a "set" -- curly braces with comma-separated values and NO key:value pairs.
# A set is like a list, but with no duplicates and no guaranteed order; it's ideal for "is X one
# of these allowed values?" checks. These two strings are the ONLY actions this entire system is
# ever allowed to run automatically.

CONFIDENCE_THRESHOLD = 0.8
# A plain number (a "float", i.e. a number with a decimal point). The model's confidence score
# has to be at least this high before we trust it enough to act without asking a human first.

RATE_LIMIT_WINDOW_MINUTES = 30
# Another plain number: how many minutes must pass before the same alarm is allowed to trigger a
# second automatic fix.

REQUIRED_KEYS = {"summary", "root_cause", "confidence", "action", "risk", "evidence"}
# Another set: the exact fields we require the model's JSON answer to contain.

VALID_RISK = {"low", "medium", "high"}
# The only three risk levels we accept from the model.


def validate_verdict(verdict) -> str | None:
    # "def" defines a function named validate_verdict, taking one input called "verdict" (no type
    # hint here, since it could arrive as almost anything before we've checked it). "-> str | None"
    # means "this returns either a string, or Python's built-in 'nothing' value, None" -- the |
    # means "or" inside a type hint like this.
    """Returns an error string if the verdict is malformed, else None."""
    # A docstring just for this one function, explaining what it does in plain English.
    if not isinstance(verdict, dict):
        # isinstance(x, dict) asks "is x a dictionary?" (Python's key -> value data structure,
        # which is what a parsed JSON object turns into). The leading "not" flips True to False
        # and vice versa.
        return "verdict is not a JSON object"
        # If it isn't even a dictionary, stop here and hand back an error message explaining why.
    missing = REQUIRED_KEYS - verdict.keys()
    # verdict.keys() lists the dictionary's keys. Subtracting one set from another (set - set)
    # gives "everything in the first set that is NOT in the second" -- so this finds any required
    # key the verdict is missing.
    if missing:
        # An empty set counts as "false" in an if-check, so this line only runs when something is
        # genuinely missing.
        return f"missing keys: {sorted(missing)}"
        # sorted(missing) turns the set into a predictable, ordered list, purely to make the error
        # message read consistently.
    if verdict["action"] not in ALLOWLISTED_ACTIONS | {"none"}:
        # verdict["action"] reads the value stored under the "action" key. The | between two sets
        # means "combine them" (a union) -- so this checks that "action" is restart_container,
        # prune_disk, or none, and rejects anything else.
        return f"unknown action: {verdict['action']!r}"
        # The !r inside an f-string means "show this value exactly as Python would type it" (so a
        # string is shown with its quote marks) -- it makes debugging output a little clearer.
    confidence = verdict["confidence"]
    # Pull the confidence value into its own variable, so we don't have to keep re-typing
    # verdict["confidence"] below.
    if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        # This checks two things joined by "or": (1) is confidence actually a number (a whole
        # number "int" or decimal "float")? and (2) once converted to a float, is it between 0.0
        # and 1.0 inclusive? If either check fails, this whole condition is True and we reject it.
        return f"confidence out of range: {confidence!r}"
    if verdict["risk"] not in VALID_RISK:
        # Make sure "risk" is exactly one of "low", "medium", or "high".
        return f"unknown risk: {verdict['risk']!r}"
    return None
    # Reaching this line means every check above passed, so there's no error -- return None to
    # mean "everything's fine".


def decide(verdict: dict, rate_limited: bool) -> tuple[str, str]:
    # This function takes an already-validated verdict dictionary plus a True/False flag saying
    # whether this alarm was already auto-fixed recently, and returns a "tuple" -- a small,
    # fixed-size group of values, here always exactly two strings.
    """Returns (decision, reason). decision is one of: auto, approval, escalate."""
    action = verdict["action"]
    # Pull the recommended action out of the dictionary into its own variable, for readability.
    confidence = float(verdict["confidence"])
    # Same for confidence, explicitly converting it to a float in case it arrived as a whole
    # number (an int) instead.
    risk = verdict["risk"]
    # Same for risk.

    if action == "none":
        # "==" checks for equality ("is action exactly the text 'none'?"). A single "=" would
        # instead try to ASSIGN a new value -- a common beginner mix-up that Python actually
        # forbids inside an "if" condition, precisely to catch this mistake.
        return "escalate", "model recommended no automated action"
        # Returning two comma-separated values like this automatically bundles them into a tuple.

    if action not in ALLOWLISTED_ACTIONS:
        # Defense in depth: even though validate_verdict already checked this earlier, double
        # check here too, in case decide() is ever called from somewhere that skipped validation.
        return "escalate", f"action {action!r} is not allowlisted"

    if rate_limited:
        # rate_limited is already a plain True/False value, so it can be used directly as a
        # condition.
        return "approval", "auto-remediation rate limit reached for this alarm in the last 30 minutes"

    if confidence >= CONFIDENCE_THRESHOLD and risk == "low":
        # "and" requires BOTH conditions to be true at once: confidence high enough, AND risk is
        # specifically "low".
        return "auto", "confidence and risk within policy for automatic remediation"

    return "approval", f"confidence={confidence} risk={risk} does not meet auto-remediation policy"
    # If none of the earlier "if" branches already returned, we fall through to here: something
    # about this verdict doesn't clear the bar for full automation, so ask a human instead.


def is_rate_limited(recent_items: list[dict], now: dt.datetime | None = None) -> bool:
    # Takes a list of dictionaries (recent audit-log entries) and an optional "now" time to
    # compare against (handy for tests -- you can pass a fixed time instead of the real clock).
    # "= None" gives that second parameter a default value, so callers are allowed to leave it out.
    """recent_items: prior audit log items for this alarm, each with 'decision' and ISO 'timestamp'."""
    now = now or dt.datetime.utcnow()
    # If the caller passed in a real "now", use it; otherwise (None is falsy) fall back to the
    # actual current time in UTC -- the same "x or y" fallback trick used elsewhere in this file.
    cutoff = now - dt.timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
    # dt.timedelta represents a span of time (here, 30 minutes). Subtracting it from "now" gives
    # the exact timestamp 30 minutes ago -- anything older than this cutoff is "too old to count".
    for item in recent_items:
        # A "for" loop: run the indented block once for every item in the list, one at a time.
        if item.get("decision") != "auto":
            # .get("decision") reads the "decision" key safely -- if the key is missing, it
            # returns None instead of crashing (unlike item["decision"], which would raise an
            # error).
            continue
            # "continue" skips the rest of this loop pass and jumps straight to the next item.
        ts = item.get("timestamp")
        # Read this item's recorded timestamp (a text string), safely, defaulting to None.
        if not ts:
            continue
            # Skip any item that somehow has no timestamp at all.
        try:
            # "try" starts a block where we attempt something that might fail (raise an error).
            item_time = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            # strptime parses a text string into a real datetime object, using the given pattern
            # to know where the year, month, day, etc. sit inside the string.
        except ValueError:
            # If the text doesn't match that exact pattern, Python raises a ValueError -- this
            # "catches" it instead of letting the whole program crash.
            continue
            # Treat an unparseable timestamp the same as a missing one: skip it and move on.
        if item_time >= cutoff:
            # Is this item's timestamp at or after the 30-minutes-ago cutoff (i.e. recent)?
            return True
            # As soon as we find ONE recent auto-remediation, we already know the alarm is
            # rate-limited -- no need to keep checking the rest of the list.
    return False
    # If the loop finishes without ever returning True, nothing recent was found, so this alarm
    # is not currently rate-limited.
