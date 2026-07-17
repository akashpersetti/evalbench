variable "project_name" {
  type    = string
  default = "evalbench"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "owner_email" {
  type        = string
  description = "Only email allowed to request a magic sign-in link."
  default     = "ahadagal@alumni.iu.edu"
}

variable "admin_token_ssm_param" {
  type        = string
  description = "SSM parameter name holding the admin bearer token (value set manually, not by Terraform)."
  default     = "/evalbench/dev/admin-token"
}

variable "ses_sender_email" {
  type        = string
  description = "SES sender identity for magic-link emails. Must NOT share owner_email's domain: SES only does per-address verification here (no domain-level DKIM DNS records), so a domain enforcing DMARC (e.g. iu.edu) rejects mail claiming to be From: that domain."
  default     = "akash.hp@icloud.com"
}

variable "github_repo" {
  type        = string
  description = "owner/repo allowed to assume the deploy role via OIDC."
  default     = "akashpersetti/evalbench"
}

locals {
  name_prefix = "${var.project_name}-${var.environment}"
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
