"""Executes an allowlisted remediation action via SSM Run Command against the tagged instance."""
# A module docstring: a one-line summary of what this whole file is responsible for.

import os
# Python's built-in module for talking to the operating system -- used here to read environment
# variables (settings passed in from outside the program, e.g. by Terraform/Lambda configuration).

import time
# Python's built-in module for anything clock-related: reading the current time, or pausing.

SSM_DOCUMENTS = {
    # A dictionary: maps a short action name (the "key") to the real AWS SSM document name (the
    # "value") that performs it. Curly braces with key: value pairs make a dict -- unlike the
    # sets seen elsewhere in this project, which have no colons.
    "restart_container": os.environ.get("SSM_DOC_RESTART", "autotriage-restart-container"),
    # os.environ is a dict-like object holding every environment variable available to this
    # program. .get(name, default) reads "SSM_DOC_RESTART" if it's set, otherwise falls back to
    # the literal text "autotriage-restart-container".
    "prune_disk": os.environ.get("SSM_DOC_PRUNE", "autotriage-prune-disk"),
    # Same pattern, for the other allowed action.
}

COMMAND_POLL_INTERVAL_SECONDS = 5
# How many seconds to wait between each check of "is the remote command done yet?".

COMMAND_TIMEOUT_SECONDS = 90
# The maximum total time we'll spend waiting before giving up on a command.


def run_action(ssm_client, action: str, instance_id: str, dry_run: bool) -> dict:
    # Defines a function with four inputs: an AWS SSM "client" object (a remote control used to
    # actually talk to AWS), the action name, which EC2 instance to target, and a True/False flag
    # for dry-run mode. It returns a dict -- a small report of what happened.
    document_name = SSM_DOCUMENTS.get(action)
    # Look up which real SSM document name corresponds to this action. .get() returns None if the
    # action isn't a recognized key, instead of crashing the way document_name = SSM_DOCUMENTS[action]
    # would if the key were missing.
    if not document_name:
        # None is "falsy" in Python, so this triggers only if the action wasn't found at all.
        return {"status": "skipped", "reason": f"no SSM document mapped for action {action!r}"}
        # Return early with an explanation -- nothing below this line runs in that case.

    if dry_run:
        # dry_run is already a plain True/False value, so it can be used directly as a condition.
        return {
            "status": "dry_run",
            "document_name": document_name,
            "instance_id": instance_id,
            "note": "DRY_RUN=true, no SSM command was sent",
        }
        # In dry-run mode we stop here and report what WOULD have happened, without touching AWS.

    send_result = ssm_client.send_command(
        # This is the line that actually calls AWS: "run this SSM document, on this instance,
        # right now."
        InstanceIds=[instance_id],
        # send_command expects a LIST of instance IDs, even though we're only ever passing one --
        # hence the square brackets around instance_id.
        DocumentName=document_name,
        # Which SSM document (remote script) to run.
        Comment=f"autotriage auto-remediation: {action}",
        # A human-readable note that shows up in the AWS console's command history.
    )
    command_id = send_result["Command"]["CommandId"]
    # AWS's response is a nested dictionary; we dig into it to grab the unique ID it assigned to
    # this command, so we can check on its progress later.

    deadline = time.time() + COMMAND_TIMEOUT_SECONDS
    # time.time() gives the current moment as a plain number (seconds since a fixed reference
    # point in the past). Adding COMMAND_TIMEOUT_SECONDS gives us "the moment we should give up".
    invocation = None
    # Start with "we don't have a result yet", before the loop below begins.
    while time.time() < deadline:
        # "while" repeats its indented block for as long as the condition stays true -- here, for
        # as long as we haven't reached the deadline yet.
        time.sleep(COMMAND_POLL_INTERVAL_SECONDS)
        # Pause for 5 seconds before checking again, so we're not hammering AWS with requests
        # every fraction of a second.
        try:
            invocation = ssm_client.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            # Ask AWS: "how is that command doing, on that instance?"
        except ssm_client.exceptions.InvocationDoesNotExist:
            # Right after sending a command, AWS sometimes hasn't registered it yet internally,
            # and this specific error means "too early, nothing to report yet."
            continue
            # Skip the rest of this loop pass and try again after the next sleep.
        if invocation["Status"] not in ("Pending", "InProgress", "Delayed"):
            # These three statuses all mean "still running". Anything else (Success, Failed,
            # Cancelled, TimedOut, ...) means the command has finished, one way or another.
            break
            # "break" exits the while loop immediately, even if the deadline hasn't passed yet.

    if invocation is None:
        # This only happens if we hit the deadline without ever successfully getting a status
        # back at all (e.g. every single check hit InvocationDoesNotExist).
        return {"status": "unknown", "command_id": command_id, "document_name": document_name}

    return {
        "status": invocation["Status"],
        # The final state AWS reported, e.g. "Success" or "Failed".
        "command_id": command_id,
        "document_name": document_name,
        "output": (invocation.get("StandardOutputContent") or "")[-2000:],
        # Grab whatever the remote script printed to standard output. `or ""` guards against the
        # value being None; [-2000:] is Python "slice" syntax meaning "the last 2000 characters",
        # which keeps this report from becoming huge if the script was very chatty.
        "error": (invocation.get("StandardErrorContent") or "")[-2000:],
        # Same idea, but for anything the script printed as an error.
    }
