terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket = "acme-state"
    key    = "main.tfstate"
    region = "us-east-1"
  }
}
