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

variable "bedrock_model_id" {
  description = "Bedrock model id (or inference-profile id) the triage Lambda calls for the JSON verdict"
  type        = string
  default     = "anthropic.claude-3-5-sonnet-20241022-v2:0"
}

variable "dry_run" {
  description = "If true, the triage Lambda reasons and logs normally but never actually sends an SSM command"
  type        = bool
  default     = true
}

variable "container_name" {
  description = "Name of the demo app's docker container on the EC2 instance"
  type        = string
  default     = "autotriage-app"
}

variable "post_action_wait_seconds" {
  description = "How long the Lambda waits after an auto-remediation before re-checking the alarm"
  type        = number
  default     = 120
}
