provider "aws" {
  alias  = "west"
  region = "us-west-2"

  endpoints {
    sts = "https://sts.example.invalid"
  }
}
