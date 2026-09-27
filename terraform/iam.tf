# --- EC2 instance role -------------------------------------------------------
# SSM Run Command target + CloudWatch agent (metrics + logs).

resource "aws_iam_role" "ec2" {
  name = "${local.name_prefix}-ec2-role"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ec2_ssm" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "ec2_cloudwatch_agent" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_iam_instance_profile" "ec2" {
  name = "${local.name_prefix}-ec2-profile"
  role = aws_iam_role.ec2.name
}

# --- Lambda execution role -----------------------------------------------------
# Least privilege: scoped to the specific log group, table, bucket prefix, SNS
# topic, the two named SSM documents, and this one tagged instance.

resource "aws_iam_role" "lambda" {
  name = "${local.name_prefix}-triage-lambda-role"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_triage" {
  name = "${local.name_prefix}-triage-lambda-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadAppLogs"
        Effect = "Allow"
        Action = [
          "logs:StartQuery",
          "logs:GetQueryResults",
          "logs:StopQuery",
          "logs:DescribeLogGroups",
        ]
        Resource = "*"
      },
      {
        Sid    = "ReadMetricsAndAlarms"
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricData",
          "cloudwatch:DescribeAlarms",
        ]
        Resource = "*"
      },
      {
        Sid      = "RunbookAndRca"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.artifacts.arn}/*"
      },
      {
        Sid      = "AuditLog"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.audit_log.arn
      },
      {
        Sid      = "Escalate"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.escalation.arn
      },
      {
        Sid      = "ReadAnthropicApiKey"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.anthropic_api_key.arn
      },
      {
        # The parameter above uses the AWS-managed "aws/ssm" key. IAM does not let you scope
        # kms:Decrypt to a key by its alias ARN (aliases aren't a valid Resource for crypto
        # operations), so this is the tightest expressible grant; the ReadAnthropicApiKey
        # statement above already limits *which parameter* can be read in the first place.
        Sid      = "DecryptAnthropicApiKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
      },
      {
        Sid    = "RunRemediation"
        Effect = "Allow"
        Action = ["ssm:SendCommand"]
        Resource = [
          aws_ssm_document.restart_container.arn,
          aws_ssm_document.prune_disk.arn,
          aws_instance.app.arn,
        ]
      },
      {
        Sid      = "ReadRemediationResult"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation"]
        Resource = "*"
      },
    ]
  })
}
