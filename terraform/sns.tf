# Alarms publish here; the triage Lambda is subscribed.
resource "aws_sns_topic" "triage_trigger" {
  name = "${local.name_prefix}-triage-trigger"
  tags = local.common_tags
}

# The Lambda publishes here for anything that isn't auto-remediated, and for unresolved auto-fixes.
resource "aws_sns_topic" "escalation" {
  name = "${local.name_prefix}-escalation"
  tags = local.common_tags
}

resource "aws_sns_topic_subscription" "escalation_email" {
  topic_arn = aws_sns_topic.escalation.arn
  protocol  = "email"
  endpoint  = var.alert_email
}
