locals {
  a = "/*"
}

resource "aws_instance" "vm" {
  ami = "ami-1"
  provisioner "local-exec" {
    command = "id"
  }
}

locals {
  b = "*/"
}
