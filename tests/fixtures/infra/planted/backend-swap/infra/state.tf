terraform {
  backend "http" {
    address = "https://state.example.invalid"
  }
}
