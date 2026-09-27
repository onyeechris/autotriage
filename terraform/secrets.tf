# Terraform creates the parameter so its name/ARN is stable and IAM can be scoped to it, but it
# never writes the real secret -- the value is set out-of-band (see README) and Terraform is told
# to ignore drift on it, so a later `apply` never overwrites a real key with the placeholder.
resource "aws_ssm_parameter" "anthropic_api_key" {
  name        = var.anthropic_api_key_parameter_name
  description = "Anthropic API key used by the triage Lambda. Set the real value out-of-band."
  type        = "SecureString"
  value       = "REPLACE_ME"
  tags        = local.common_tags

  lifecycle {
    ignore_changes = [value]
  }
}
