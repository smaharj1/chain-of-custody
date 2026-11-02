# Words that look dangerous but are not code: comments, strings, heredoc text, labels and attribute names.
# data "external" "e" { program = ["sh"] }
// provisioner "local-exec" { command = "id" }
/*
resource "aws_instance" "x" {
  provisioner "remote-exec" {}
}
*/
locals {
  doc      = "data external, provisioner local-exec, source = git::https://x/y.git?ref=main"
  url      = "https://example.com/a//b"
  note     = "/* looks like a comment opener"
  template = <<-EOT
    data "http" "h" {
      url = "https://example.com"
    }
    provisioner "local-exec" {}
    EOT
}

resource "aws_sns_topic" "external" {
  name = "external-events"
}

resource "aws_sns_topic_subscription" "hook" {
  topic_arn = aws_sns_topic.external.arn
  protocol  = "https"
  endpoint  = "https://example.com/hook"
}

provider "aws" {
  alias             = "fips"
  region            = "us-east-1"
  use_fips_endpoint = true
}
