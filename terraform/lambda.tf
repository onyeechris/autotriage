data "archive_file" "triage_lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/triage"
  output_path = "${path.module}/build/triage_lambda.zip"
  excludes    = ["requirements.txt", "README.md", "__pycache__"]
}

resource "aws_lambda_function" "triage" {
  function_name    = "${local.name_prefix}-triage"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 480 # covers Logs Insights query + Anthropic API call + up to 90s SSM polling + 120s post-action wait
  memory_size      = 256
  filename         = data.archive_file.triage_lambda.output_path
  source_code_hash = data.archive_file.triage_lambda.output_base64sha256
  tags             = local.common_tags

  environment {
    variables = {
      LOG_GROUP_NAME           = aws_cloudwatch_log_group.app.name
      INSTANCE_ID              = aws_instance.app.id
      S3_BUCKET                = aws_s3_bucket.artifacts.bucket
      RUNBOOK_KEY              = aws_s3_object.runbook.key
      DYNAMODB_TABLE           = aws_dynamodb_table.audit_log.name
      ANTHROPIC_MODEL          = var.anthropic_model
      ANTHROPIC_API_KEY_PARAM  = aws_ssm_parameter.anthropic_api_key.name
      ESCALATION_TOPIC_ARN     = aws_sns_topic.escalation.arn
      SSM_DOC_RESTART          = aws_ssm_document.restart_container.name
      SSM_DOC_PRUNE            = aws_ssm_document.prune_disk.name
      DRY_RUN                  = tostring(var.dry_run)
      POST_ACTION_WAIT_SECONDS = tostring(var.post_action_wait_seconds)
    }
  }
}

resource "aws_lambda_permission" "allow_sns" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.triage.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.triage_trigger.arn
}

resource "aws_sns_topic_subscription" "triage_lambda" {
  topic_arn = aws_sns_topic.triage_trigger.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.triage.arn
}
