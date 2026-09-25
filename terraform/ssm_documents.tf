resource "aws_ssm_document" "restart_container" {
  name            = "${local.name_prefix}-restart-container"
  document_type   = "Command"
  document_format = "YAML"
  content         = file("${path.module}/../ssm/restart-container.yml")
  tags            = local.common_tags
}

resource "aws_ssm_document" "prune_disk" {
  name            = "${local.name_prefix}-prune-disk"
  document_type   = "Command"
  document_format = "YAML"
  content         = file("${path.module}/../ssm/prune-disk.yml")
  tags            = local.common_tags
}
