# Data sources for SSM parameter store secrets
# These are referenced by Lambda functions and are read-only in Terraform.
# Initial values must be set manually via AWS console or CLI.

data "aws_ssm_parameter" "admin_token" {
  name = "/evalbench/admin_token"
}

data "aws_ssm_parameter" "openai_api_key" {
  name = "/evalbench/openai_api_key"
}

data "aws_ssm_parameter" "anthropic_api_key" {
  name = "/evalbench/anthropic_api_key"
}

data "aws_ssm_parameter" "gemini_api_key" {
  name = "/evalbench/gemini_api_key"
}

data "aws_ssm_parameter" "openrouter_api_key" {
  name = "/evalbench/openrouter_api_key"
}

data "aws_ssm_parameter" "xai_api_key" {
  name = "/evalbench/xai_api_key"
}

data "aws_ssm_parameter" "judge_model" {
  name = "/evalbench/judge_model"
}
