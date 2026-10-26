provider "aws" {
  region = "us-east-1"
}

resource "aws_s3_bucket" "logs" {
  bucket = "acme-logs"
}

module "net" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.1.0"
}

module "local" {
  source = "./modules/thing"
}
