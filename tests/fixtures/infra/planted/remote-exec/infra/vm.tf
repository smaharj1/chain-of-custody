resource "aws_instance" "vm" {
  ami = "ami-1"

  provisioner "remote-exec" {
    inline = ["id"]
  }
}
