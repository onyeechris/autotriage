"""Gathers the evidence the triage prompt needs: recent logs, recent metrics, and the runbook."""
import time

LOGS_INSIGHTS_QUERY = """
fields @timestamp, @message
| sort @timestamp desc
| limit 200
"""


def get_recent_logs(logs_client, log_group_name: str, minutes: int = 15) -> str:
    end_time = int(time.time())
    start_time = end_time - minutes * 60
    try:
        start = logs_client.start_query(
            logGroupName=log_group_name,
            startTime=start_time,
            endTime=end_time,
            queryString=LOGS_INSIGHTS_QUERY,
        )
    except logs_client.exceptions.ResourceNotFoundException:
        return "(log group not found)"

    query_id = start["queryId"]
    result = None
    for _ in range(15):
        time.sleep(1)
        result = logs_client.get_query_results(queryId=query_id)
        if result["status"] in ("Complete", "Failed", "Cancelled"):
            break

    if result is None or result["status"] != "Complete":
        status = result["status"] if result else "unknown"
        return f"(log query did not complete: {status})"

    lines = []
    for row in result.get("results", []):
        fields = {f["field"]: f["value"] for f in row}
        lines.append(f"{fields.get('@timestamp', '')} {fields.get('@message', '')}")
    return "\n".join(lines) if lines else f"(no log lines in the last {minutes} minutes)"


def get_recent_metrics(cloudwatch_client, instance_id: str) -> str:
    end_time = int(time.time())
    start_time = end_time - 15 * 60
    queries = [
        {
            "Id": "cpu",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/EC2",
                    "MetricName": "CPUUtilization",
                    "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                },
                "Period": 300,
                "Stat": "Average",
            },
        },
        {
            "Id": "disk",
            "MetricStat": {
                "Metric": {
                    "Namespace": "CWAgent",
                    "MetricName": "disk_used_percent",
                    "Dimensions": [{"Name": "InstanceId", "Value": instance_id}, {"Name": "path", "Value": "/"}],
                },
                "Period": 300,
                "Stat": "Average",
            },
        },
        {
            "Id": "container_running",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AutoTriage",
                    "MetricName": "ContainerRunning",
                    "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                },
                "Period": 60,
                "Stat": "Minimum",
            },
        },
    ]
    try:
        response = cloudwatch_client.get_metric_data(
            MetricDataQueries=queries, StartTime=start_time, EndTime=end_time
        )
    except Exception as exc:  # noqa: BLE001 - best-effort context, never fail triage over metrics
        return f"(failed to fetch metrics: {exc})"

    lines = []
    for result in response.get("MetricDataResults", []):
        values = ", ".join(f"{v:.1f}" for v in result.get("Values", []))
        lines.append(f"{result['Id']}: [{values or 'no data points'}]")
    return "\n".join(lines)


def get_runbook(s3_client, bucket: str, key: str) -> str:
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        return f"(failed to load runbook from s3://{bucket}/{key}: {exc})"
