variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix used for naming/tagging every resource"
  type        = string
  default     = "autotriage"
}

variable "instance_type" {
  description = "EC2 instance type for the demo app"
  type        = string
  default     = "t3.micro"
}

variable "key_name" {
  description = "Existing EC2 key pair name, for SSH access to the demo instance while debugging"
  type        = string
}

variable "ssh_ingress_cidr" {
  description = "CIDR allowed to SSH (port 22) into the demo instance. Restrict this to your own IP/32."
  type        = string
  default     = "0.0.0.0/0"
}

variable "app_ingress_cidr" {
  description = "CIDR allowed to reach the app (port 8000) directly for manual testing/chaos calls."
  type        = string
  default     = "0.0.0.0/0"
}

variable "alert_email" {
  description = "Email address subscribed to the escalation SNS topic (approvals + unresolved incidents)"
  type        = string
}

variable "anthropic_model" {
  description = "Anthropic model id the triage Lambda calls directly via the Messages API (see https://docs.anthropic.com/en/docs/about-claude/models for current ids)"
  type        = string
  default     = "claude-haiku-4-5-20251001"
}

variable "anthropic_api_key_parameter_name" {
  description = "SSM Parameter Store name (SecureString) holding the Anthropic API key. Terraform creates the parameter but never sets its real value -- set that out-of-band (see README)."
  type        = string
  default     = "/autotriage/anthropic-api-key"
}

variable "dry_run" {
  description = "If true, the triage Lambda reasons and logs normally but never actually sends an SSM command"
  type        = bool
  default     = false
}

variable "container_name" {
  description = "Name of the demo app's docker container on the EC2 instance"
  type        = string
  default     = "autotriage-app"
}

variable "post_action_wait_seconds" {
  description = "How long the Lambda waits after an auto-remediation before re-checking the alarm. Must cover the alarm's evaluation lag (log-based 5xx alarm ~4 min); the Lambda timeout (600s, lambda.tf) must stay above this plus ~150s of other work."
  type        = number
  default     = 360
}

variable "rate_limit_window_minutes" {
  description = "Minimum minutes between two real auto-remediations for the same alarm (fix-loop guard). 5 is a test setting; use 30 for production."
  type        = number
  default     = 5
}
