resource "aws_instance" "vm" {
  ami = "ami-1"

  provisioner "file" {
    source      = "a"
    destination = "/tmp/a"
  }
}
