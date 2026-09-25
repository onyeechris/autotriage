output "instance_id" {
  value = aws_instance.app.id
}

output "instance_public_ip" {
  value = aws_instance.app.public_ip
}

output "ssh_command" {
  value = "ssh -i <path-to-${var.key_name}.pem> ec2-user@${aws_instance.app.public_ip}"
}

output "app_base_url" {
  value = "http://${aws_instance.app.public_ip}:8000"
}

output "triage_trigger_topic_arn" {
  value = aws_sns_topic.triage_trigger.arn
}

output "escalation_topic_arn" {
  value = aws_sns_topic.escalation.arn
}

output "triage_lambda_name" {
  value = aws_lambda_function.triage.function_name
}

output "audit_log_table" {
  value = aws_dynamodb_table.audit_log.name
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "app_log_group" {
  value = aws_cloudwatch_log_group.app.name
}
