# Ordinary Terraform with the idioms a real repository has. The scanner must read all of it and flag none of it.
variable "environment" {
  type        = string
  description = "Deployment environment"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod."
  }
}

variable "subnets" {
  type = map(object({
    cidr = string
    az   = optional(string)
  }))
  default = {
    a = { cidr = "10.0.1.0/24", az = "us-east-1a" }
    b = {
      cidr = "10.0.2.0/24"
    }
  }
}

locals {
  name_prefix = "acme-${var.environment}"
  tags = merge(
    {
      Environment = var.environment
      ManagedBy   = "terraform"
    },
    var.environment == "prod" ? { Critical = "true" } : {},
  )
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = "arn:aws:s3:::${local.name_prefix}-assets/*"
    }]
  })
  user_data = <<-EOT
    #!/bin/bash
    echo "environment: ${var.environment}"
    cat <<'INNER'
    not a terminator
    INNER
    EOT
}

resource "aws_subnet" "this" {
  for_each = var.subnets

  vpc_id            = module.net.vpc_id
  cidr_block        = each.value.cidr
  availability_zone = each.value.az != null ? each.value.az : "us-east-1c"
  tags              = merge(local.tags, { Name = "${local.name_prefix}-${each.key}" })

  lifecycle {
    create_before_destroy = true
    precondition {
      condition     = can(cidrnetmask(each.value.cidr))
      error_message = "cidr must be valid."
    }
  }
}

resource "aws_security_group" "web" {
  name   = "${local.name_prefix}-web"
  vpc_id = module.net.vpc_id

  dynamic "ingress" {
    for_each = [80, 443]
    content {
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
}

resource "aws_iam_policy" "assets" {
  name   = "${local.name_prefix}-assets"
  policy = local.policy
}

resource "aws_s3_object" "index" {
  bucket       = aws_s3_bucket.logs.id
  key          = "index.html"
  content      = templatefile("${path.module}/templates/index.html.tpl", { env = var.environment })
  content_type = "text/html"
}

output "subnet_ids" {
  value = [for s in aws_subnet.this : s.id]
}

output "first_subnet" {
  value = values(aws_subnet.this)[*].id[0]
}
