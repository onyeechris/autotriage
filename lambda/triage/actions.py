"""Executes an allowlisted remediation action via SSM Run Command against the tagged instance."""
import os
import time

SSM_DOCUMENTS = {
    "restart_container": os.environ.get("SSM_DOC_RESTART", "autotriage-restart-container"),
    "prune_disk": os.environ.get("SSM_DOC_PRUNE", "autotriage-prune-disk"),
}

COMMAND_POLL_INTERVAL_SECONDS = 5
COMMAND_TIMEOUT_SECONDS = 90


def run_action(ssm_client, action: str, instance_id: str, dry_run: bool) -> dict:
    document_name = SSM_DOCUMENTS.get(action)
    if not document_name:
        return {"status": "skipped", "reason": f"no SSM document mapped for action {action!r}"}

    if dry_run:
        return {
            "status": "dry_run",
            "document_name": document_name,
            "instance_id": instance_id,
            "note": "DRY_RUN=true, no SSM command was sent",
        }

    send_result = ssm_client.send_command(
        InstanceIds=[instance_id],
        DocumentName=document_name,
        Comment=f"autotriage auto-remediation: {action}",
    )
    command_id = send_result["Command"]["CommandId"]

    deadline = time.time() + COMMAND_TIMEOUT_SECONDS
    invocation = None
    while time.time() < deadline:
        time.sleep(COMMAND_POLL_INTERVAL_SECONDS)
        try:
            invocation = ssm_client.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ssm_client.exceptions.InvocationDoesNotExist:
            continue
        if invocation["Status"] not in ("Pending", "InProgress", "Delayed"):
            break

    if invocation is None:
        return {"status": "unknown", "command_id": command_id, "document_name": document_name}

    return {
        "status": invocation["Status"],
        "command_id": command_id,
        "document_name": document_name,
        "output": (invocation.get("StandardOutputContent") or "")[-2000:],
        "error": (invocation.get("StandardErrorContent") or "")[-2000:],
    }
