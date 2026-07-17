terraform {
  backend "s3" {
    # Set via: terraform init -backend-config="bucket=<your-state-bucket>"
    key     = "evalbench/terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}
