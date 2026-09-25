resource "aws_dynamodb_table" "audit_log" {
  name         = "${local.name_prefix}-audit-log"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "alarm_name"
  range_key    = "timestamp"

  attribute {
    name = "alarm_name"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  tags = local.common_tags
}
