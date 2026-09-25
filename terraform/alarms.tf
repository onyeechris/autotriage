resource "aws_cloudwatch_log_group" "app" {
  name              = "/${local.name_prefix}/app"
  retention_in_days = 14
  tags              = local.common_tags
}

# --- 5xx rate --------------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "http_5xx" {
  name           = "${local.name_prefix}-http-5xx"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.status_code = 500 }"

  metric_transformation {
    name          = "HTTP5xxCount"
    namespace     = "AutoTriage"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "http_5xx" {
  alarm_name          = "${local.name_prefix}-http-5xx-rate"
  alarm_description   = "More than 5 HTTP 5xx responses in 1 minute"
  namespace           = "AutoTriage"
  metric_name         = "HTTP5xxCount"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.triage_trigger.arn]
  ok_actions          = [aws_sns_topic.triage_trigger.arn]
  tags                = local.common_tags
}

# --- container down ---------------------------------------------------------
# A cron heartbeat on the instance publishes AutoTriage/ContainerRunning (1 or 0) every minute.

resource "aws_cloudwatch_metric_alarm" "container_down" {
  alarm_name          = "${local.name_prefix}-container-down"
  alarm_description   = "The demo app container is not running"
  namespace           = "AutoTriage"
  metric_name         = "ContainerRunning"
  dimensions          = { InstanceId = aws_instance.app.id }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.triage_trigger.arn]
  ok_actions          = [aws_sns_topic.triage_trigger.arn]
  tags                = local.common_tags
}

# --- disk usage --------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "disk_high" {
  alarm_name          = "${local.name_prefix}-disk-high"
  alarm_description   = "Root volume disk usage above 85%"
  namespace           = "CWAgent"
  metric_name         = "disk_used_percent"
  dimensions          = { InstanceId = aws_instance.app.id, path = "/", fstype = "xfs" }
  statistic           = "Average"
  period              = 60
  evaluation_periods  = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.triage_trigger.arn]
  ok_actions          = [aws_sns_topic.triage_trigger.arn]
  tags                = local.common_tags
}
