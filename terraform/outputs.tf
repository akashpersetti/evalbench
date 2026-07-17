output "cloudfront_domain_name" {
  description = "CloudFront distribution domain name for accessing the frontend"
  value       = aws_cloudfront_distribution.main.domain_name
}

output "frontend_url" {
  description = "Full HTTPS URL of the deployed frontend"
  value       = "https://evalbench.akashpersetti.com"
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID for cache invalidation"
  value       = aws_cloudfront_distribution.main.id
}

output "api_gateway_endpoint" {
  description = "API Gateway HTTP API endpoint URL"
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "s3_frontend_bucket" {
  description = "S3 bucket name for frontend assets"
  value       = aws_s3_bucket.frontend.id
}

output "s3_db_bucket" {
  description = "S3 bucket name for database storage"
  value       = aws_s3_bucket.db.id
}

output "dynamodb_magic_tokens_table" {
  description = "DynamoDB table name for magic tokens"
  value       = aws_dynamodb_table.magic_tokens.name
}

output "dynamodb_run_status_table" {
  description = "DynamoDB table name for run status"
  value       = aws_dynamodb_table.run_status.name
}

output "ses_email_identity" {
  description = "SES email identity for sending magic link emails"
  value       = aws_ses_email_identity.owner.email
}

output "api_lambda_function_name" {
  description = "Name of the API Lambda function"
  value       = aws_lambda_function.api.function_name
}

output "runner_lambda_function_name" {
  description = "Name of the Runner Lambda function"
  value       = aws_lambda_function.runner.function_name
}
