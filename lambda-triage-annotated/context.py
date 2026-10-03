"""Gathers the evidence the triage prompt needs: recent logs, recent metrics, and the runbook."""
# A module docstring: what this file's three functions have in common -- each one fetches one
# kind of "evidence" from a different AWS service, to hand to the LLM as context.

import time
# Used here for reading the current time and for pausing between checks, same as in actions.py.

LOGS_INSIGHTS_QUERY = """
fields @timestamp, @message
| sort @timestamp desc
| limit 200
"""
# A multi-line string holding a query written in CloudWatch Logs Insights' OWN query language
# (not Python -- Python just treats this whole thing as one block of text). In plain English:
# "give me the timestamp and message fields, sorted newest-first, and stop at 200 rows."


def get_recent_logs(logs_client, log_group_name: str, minutes: int = 15) -> str:
    # "minutes: int = 15" means the minutes parameter defaults to 15 if the caller doesn't specify
    # one -- so get_recent_logs(client, "/my/log-group") is valid and just uses 15 automatically.
    end_time = int(time.time())
    # time.time() returns a float (fractional seconds, e.g. 1234567.89); int(...) rounds it down
    # to a whole number, since the AWS API expects whole-second timestamps.
    start_time = end_time - minutes * 60
    # Python follows normal math order of operations, so this is end_time - (minutes * 60):
    # convert "15 minutes" into seconds, and subtract that from the end time to get the start time.
    try:
        start = logs_client.start_query(
            # Kick off an asynchronous log search in CloudWatch -- it hands back an ID right away
            # and does the actual searching in the background, which is why we have to poll for
            # results a bit further down.
            logGroupName=log_group_name,
            startTime=start_time,
            endTime=end_time,
            queryString=LOGS_INSIGHTS_QUERY,
        )
    except logs_client.exceptions.ResourceNotFoundException:
        # This specific error means the log group doesn't exist yet -- e.g. the app hasn't logged
        # anything, or the CloudWatch agent hasn't started shipping logs to it yet.
        return "(log group not found)"
        # Give back a plain, readable placeholder string instead of letting the error crash the
        # whole triage run.

    query_id = start["queryId"]
    # Pull the query's ID out of AWS's response, so we can ask about its progress next.
    result = None
    # Start with "no result yet", before the loop below runs.
    for _ in range(15):
        # range(15) produces the numbers 0 through 14, so this loop runs (at most) 15 times. The
        # underscore _ is a Python convention meaning "I don't actually need this loop variable's
        # value, I just want to repeat something N times."
        time.sleep(1)
        # Wait one second between each check.
        result = logs_client.get_query_results(queryId=query_id)
        # Ask AWS: "is the query done yet, and if so, what did it find?"
        if result["status"] in ("Complete", "Failed", "Cancelled"):
            # `in (...)` checks membership against a tuple of possible values -- any of these
            # three statuses mean the query has finished, one way or another.
            break
            # Stop polling early once we have a final answer, instead of always waiting the full
            # 15 tries.

    if result is None or result["status"] != "Complete":
        # Covers two problem cases at once: (1) the loop somehow never ran (not possible here, but
        # defensive coding), or (2) it ran out of tries without the query ever finishing.
        status = result["status"] if result else "unknown"
        # A one-line if/else, called a "conditional expression": use result["status"] if result
        # actually exists, otherwise use the plain string "unknown".
        return f"(log query did not complete: {status})"

    lines = []
    # An empty list we'll fill in, one line of text per matching log entry.
    for row in result.get("results", []):
        # result.get("results", []) reads the "results" key, or an empty list if it's missing, so
        # the loop below simply doesn't run instead of crashing if there's nothing there.
        fields = {f["field"]: f["value"] for f in row}
        # A "dict comprehension": build a new dictionary by looping over `row` (itself a list of
        # small {"field": ..., "value": ...} dictionaries from the AWS API) and picking out just
        # the field name and value from each one. This turns AWS's clunky format into something
        # simple, like {"@timestamp": "...", "@message": "..."}.
        lines.append(f"{fields.get('@timestamp', '')} {fields.get('@message', '')}")
        # Build one readable line of text per log row, and add it onto the end of our list.
    return "\n".join(lines) if lines else f"(no log lines in the last {minutes} minutes)"
    # "\n".join(lines) glues every entry in the list together into one big string, with a newline
    # character between each one. The "if lines else ..." part means: only do that if we actually
    # found some lines; otherwise return a friendly placeholder message instead.


def get_recent_metrics(cloudwatch_client, instance_id: str) -> str:
    end_time = int(time.time())
    start_time = end_time - 15 * 60
    # Same pattern as get_recent_logs above, but hardcoded to a fixed 15-minute window this time.
    queries = [
        # A list containing three dictionaries -- each one describes one metric we want
        # CloudWatch to fetch, all in a single batched API call (more efficient than three
        # separate calls, one per metric).
        {
            "Id": "cpu",
            # A short nickname WE choose, so we can match this query back up with its results
            # later in the response.
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/EC2",
                    # Which AWS service's metrics we're looking in -- EC2's own built-in metrics.
                    "MetricName": "CPUUtilization",
                    "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                    # "Dimensions" narrow a metric down to one specific resource -- here, one
                    # specific EC2 instance, identified by its ID.
                },
                "Period": 300,
                # Group data into 5-minute (300-second) buckets.
                "Stat": "Average",
                # Within each bucket, give us the average value.
            },
        },
        {
            "Id": "disk",
            "MetricStat": {
                "Metric": {
                    "Namespace": "CWAgent",
                    # A different namespace: metrics published by the CloudWatch agent running ON
                    # the instance itself (things EC2 doesn't track by default, like disk usage).
                    "MetricName": "disk_used_percent",
                    "Dimensions": [
                        {"Name": "InstanceId", "Value": instance_id},
                        # Which instance...
                        {"Name": "path", "Value": "/"},
                        # ...which disk path ("/", the root filesystem)...
                        {"Name": "fstype", "Value": "xfs"},
                        # ...and which filesystem type. The agent always publishes all three, and a
                        # metric is only found when every dimension matches exactly, so omitting
                        # fstype here would silently return no data. (Same three dimensions as
                        # the disk-high alarm in terraform/alarms.tf.)
                    ],
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
                    # Our own custom namespace, for the heartbeat metric this project publishes
                    # itself (see terraform/user_data.sh.tpl's heartbeat.sh script).
                    "MetricName": "ContainerRunning",
                    "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                },
                "Period": 60,
                # A smaller, 1-minute bucket size, since the heartbeat itself reports every minute.
                "Stat": "Minimum",
                # The minimum matters here: if the container was down for even one second inside a
                # bucket, we want that bucket to show 0, not an average that would hide the outage.
            },
        },
    ]
    try:
        response = cloudwatch_client.get_metric_data(
            MetricDataQueries=queries, StartTime=start_time, EndTime=end_time,
            ScanBy="TimestampAscending",
            # By default CloudWatch returns the newest datapoint first. Ask for oldest-first so the
            # list reads like a timeline. Otherwise the model sees the pre-crash "1.0" values at
            # the end of the list and wrongly concludes the container has recovered.
        )
        # Send all three queries to AWS in one request, and get all three sets of results back
        # together in one response.
    except Exception as exc:  # noqa: BLE001 - best-effort context, never fail triage over metrics
        # "except Exception" is a very broad catch -- it matches almost any kind of error. This is
        # deliberate here: metrics are "nice to have" context for the LLM, not critical, so any
        # failure here should be swallowed and turned into a text message rather than crashing the
        # whole triage run. "as exc" gives us a name to refer to the actual error object.
        return f"(failed to fetch metrics: {exc})"

    lines = []
    for result in response.get("MetricDataResults", []):
        # Loop over each of the three query results in the response.
        points = ", ".join(
            f"{ts:%H:%M}={v:.1f}" for ts, v in zip(result.get("Timestamps", []), result.get("Values", []))
        )
        # zip() pairs each timestamp with its value. For each pair, the f-string writes the time as
        # HH:MM (":%H:%M" is a datetime format) and the value with 1 decimal place, giving text
        # like "17:12=0.0". join() glues the pairs together with ", ".
        latest = f"latest={result['Values'][-1]:.1f}" if result.get("Values") else "latest=none"
        # The most recent value on its own line, so the model doesn't have to work it out from a
        # long list. [-1] is Python's way of saying "the last item in the list".
        lines.append(f"{result['Id']} ({latest}), oldest->newest UTC: [{points or 'no data points'}]")
        # One line per metric, e.g. "container_running (latest=0.0), oldest->newest UTC: [17:10=1.0, 17:12=0.0]".
    return "\n".join(lines)
    # Join every metric's line into one multi-line string to hand back to the caller.


def get_runbook(s3_client, bucket: str, key: str) -> str:
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        # Download the runbook file's metadata, plus a stream to read its contents from S3.
        return obj["Body"].read().decode("utf-8")
        # obj["Body"] is a stream of raw bytes; .read() pulls all of them into memory at once, and
        # .decode("utf-8") converts those raw bytes into a normal, readable Python text string.
    except Exception as exc:  # noqa: BLE001
        return f"(failed to load runbook from s3://{bucket}/{key}: {exc})"
        # Same "don't crash the whole triage run over optional context" philosophy as above.
