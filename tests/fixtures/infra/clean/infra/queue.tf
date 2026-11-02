# An ordinary change: a new resource of a provider the base already uses, a pinned registry module and a git module pinned to a commit.
resource "aws_sqs_queue" "jobs" {
  name = "jobs"
}

module "assets" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "4.1.2"
}

module "shared" {
  source = "git::https://example.com/acme/shared.git//queue?ref=0123456789abcdef0123456789abcdef01234567"
}

module "more" {
  source = "./modules/thing"
}
