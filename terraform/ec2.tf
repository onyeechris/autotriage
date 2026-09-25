resource "aws_security_group" "app" {
  name_prefix = "${local.name_prefix}-app-"
  description = "AutoTriage demo app instance"
  vpc_id      = data.aws_vpc.default.id
  tags        = local.common_tags

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_ingress_cidr]
  }

  ingress {
    description = "App (health + chaos endpoints)"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.app_ingress_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  key_name               = var.key_name
  subnet_id              = local.subnet_id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2.name

  user_data = templatefile("${path.module}/user_data.sh.tpl", {
    container_name           = var.container_name
    aws_region               = var.aws_region
    log_group_name           = aws_cloudwatch_log_group.app.name
    app_main_py_b64          = base64encode(file("${path.module}/../app/main.py"))
    app_requirements_txt_b64 = base64encode(file("${path.module}/../app/requirements.txt"))
    app_dockerfile_b64       = base64encode(file("${path.module}/../app/Dockerfile"))
  })

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-app"
    Role = "${local.name_prefix}-app"
  })
}
