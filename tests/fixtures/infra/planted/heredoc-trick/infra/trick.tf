locals {
  harmless = <<EOT
EOT is only text here
EOT
}

resource "aws_instance" "vm" {
  ami = "ami-1"
  provisioner "local-exec" {
    command = "id"
  }
}
