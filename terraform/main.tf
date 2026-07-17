# Data source for current AWS account ID
data "aws_caller_identity" "current" {}

# DynamoDB table for magic_tokens
resource "aws_dynamodb_table" "magic_tokens" {
  name         = "magic_tokens"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "token"

  attribute {
    name = "token"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = {
    Name        = "magic_tokens"
    Environment = var.environment
  }
}

# DynamoDB table for run_status
resource "aws_dynamodb_table" "run_status" {
  name         = "run_status"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"

  attribute {
    name = "run_id"
    type = "S"
  }

  tags = {
    Name        = "run_status"
    Environment = var.environment
  }
}

# S3 bucket for db (private)
resource "aws_s3_bucket" "db" {
  bucket = "evalbench-db-${data.aws_caller_identity.current.account_id}"

  tags = {
    Name        = "evalbench-db"
    Environment = var.environment
  }
}

# Block all public access to S3 db bucket
resource "aws_s3_bucket_public_access_block" "db" {
  bucket = aws_s3_bucket.db.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Enable versioning for S3 bucket
resource "aws_s3_bucket_versioning" "db" {
  bucket = aws_s3_bucket.db.id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSM parameter for admin_token
resource "aws_ssm_parameter" "admin_token" {
  name  = "/evalbench/admin_token"
  type  = "SecureString"
  value = "placeholder"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name        = "admin_token"
    Environment = var.environment
  }
}

# IAM role for the API Lambda function
resource "aws_iam_role" "api_lambda_role" {
  name = "evalbench-api-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name        = "evalbench-api-lambda-role"
    Environment = var.environment
  }
}

# IAM policy for API Lambda: CloudWatch Logs
resource "aws_iam_role_policy" "api_lambda_logs" {
  name = "evalbench-api-lambda-logs"
  role = aws_iam_role.api_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/*"
      }
    ]
  })
}

# IAM policy for API Lambda: SSM Parameter read
resource "aws_iam_role_policy" "api_lambda_ssm" {
  name = "evalbench-api-lambda-ssm"
  role = aws_iam_role.api_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParameter"
        ]
        Resource = [
          data.aws_ssm_parameter.admin_token.arn,
          data.aws_ssm_parameter.openai_api_key.arn,
          data.aws_ssm_parameter.anthropic_api_key.arn,
          data.aws_ssm_parameter.gemini_api_key.arn,
          data.aws_ssm_parameter.openrouter_api_key.arn,
          data.aws_ssm_parameter.xai_api_key.arn,
          data.aws_ssm_parameter.judge_model.arn
        ]
      }
    ]
  })
}

# IAM policy for API Lambda: DynamoDB read/write on both tables
resource "aws_iam_role_policy" "api_lambda_dynamodb" {
  name = "evalbench-api-lambda-dynamodb"
  role = aws_iam_role.api_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:DeleteItem"
        ]
        Resource = [
          aws_dynamodb_table.magic_tokens.arn,
          aws_dynamodb_table.run_status.arn
        ]
      }
    ]
  })
}

# IAM policy for API Lambda: S3 read/write on db bucket
resource "aws_iam_role_policy" "api_lambda_s3" {
  name = "evalbench-api-lambda-s3"
  role = aws_iam_role.api_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject"
        ]
        Resource = "${aws_s3_bucket.db.arn}/*"
      }
    ]
  })
}

# IAM policy for API Lambda: SES send
resource "aws_iam_role_policy" "api_lambda_ses" {
  name = "evalbench-api-lambda-ses"
  role = aws_iam_role.api_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ses:SendEmail",
          "ses:SendRawEmail"
        ]
        Resource = "*"
      }
    ]
  })
}

# IAM policy for API Lambda: invoke runner Lambda
resource "aws_iam_role_policy" "api_lambda_invoke_runner" {
  name = "evalbench-api-lambda-invoke-runner"
  role = aws_iam_role.api_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction"
        ]
        Resource = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:evalbench-runner"
      }
    ]
  })
}

# IAM role for the Runner Lambda function
resource "aws_iam_role" "runner_lambda_role" {
  name = "evalbench-runner-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name        = "evalbench-runner-lambda-role"
    Environment = var.environment
  }
}

# IAM policy for Runner Lambda: CloudWatch Logs
resource "aws_iam_role_policy" "runner_lambda_logs" {
  name = "evalbench-runner-lambda-logs"
  role = aws_iam_role.runner_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/*"
      }
    ]
  })
}

# IAM policy for Runner Lambda: SSM read (provider keys + judge model only)
resource "aws_iam_role_policy" "runner_lambda_ssm" {
  name = "evalbench-runner-lambda-ssm"
  role = aws_iam_role.runner_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParameter"
        ]
        Resource = [
          data.aws_ssm_parameter.openai_api_key.arn,
          data.aws_ssm_parameter.anthropic_api_key.arn,
          data.aws_ssm_parameter.gemini_api_key.arn,
          data.aws_ssm_parameter.openrouter_api_key.arn,
          data.aws_ssm_parameter.xai_api_key.arn,
          data.aws_ssm_parameter.judge_model.arn
        ]
      }
    ]
  })
}

# IAM policy for Runner Lambda: S3 read/write on db bucket
resource "aws_iam_role_policy" "runner_lambda_s3" {
  name = "evalbench-runner-lambda-s3"
  role = aws_iam_role.runner_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = "${aws_s3_bucket.db.arn}/*"
      }
    ]
  })
}

# IAM policy for Runner Lambda: DynamoDB read/write on run_status only
resource "aws_iam_role_policy" "runner_lambda_dynamodb" {
  name = "evalbench-runner-lambda-dynamodb"
  role = aws_iam_role.runner_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem"
        ]
        Resource = aws_dynamodb_table.run_status.arn
      }
    ]
  })
}

# Lambda deployment package, staged to S3 - litellm's dependency tree pushes
# the zip past Lambda's 50MB direct-upload limit, so both functions deploy
# via S3 (250MB unzipped ceiling) instead of a local `filename` upload.
resource "aws_s3_object" "lambda_deployment" {
  bucket = aws_s3_bucket.db.id
  key    = "lambda/lambda-deployment.zip"
  source = "${path.module}/../backend/lambda-deployment.zip"
  etag   = filemd5("${path.module}/../backend/lambda-deployment.zip")
}

# API Lambda function
resource "aws_lambda_function" "api" {
  s3_bucket        = aws_s3_object.lambda_deployment.bucket
  s3_key           = aws_s3_object.lambda_deployment.key
  source_code_hash = filebase64sha256("${path.module}/../backend/lambda-deployment.zip")
  function_name    = "evalbench-api"
  role             = aws_iam_role.api_lambda_role.arn
  handler          = "evalbench.lambda_handler.handler"
  runtime          = "python3.12"
  timeout          = 29
  # Lambda's INIT phase (module-level imports - litellm alone is heavy, plus
  # FastAPI/SQLAlchemy/boto3 and eager suite construction in registry.py) has
  # a hard, non-configurable 10s cap separate from `timeout` above. 512MB's
  # CPU share wasn't enough; more memory buys more CPU during cold start.
  memory_size      = 2048

  environment {
    variables = {
      REQUIRE_AUTH                = "true"
      OWNER_EMAIL                 = "ahadagal@alumni.iu.edu"
      DYNAMODB_MAGIC_TOKENS_TABLE = aws_dynamodb_table.magic_tokens.name
      DYNAMODB_RUN_STATUS_TABLE   = aws_dynamodb_table.run_status.name
      S3_DB_BUCKET                = aws_s3_bucket.db.id
      S3_DB_KEY                   = "evalbench.db"
      ADMIN_TOKEN                 = data.aws_ssm_parameter.admin_token.value
      OPENAI_API_KEY              = data.aws_ssm_parameter.openai_api_key.value
      ANTHROPIC_API_KEY           = data.aws_ssm_parameter.anthropic_api_key.value
      GEMINI_API_KEY              = data.aws_ssm_parameter.gemini_api_key.value
      OPENROUTER_API_KEY          = data.aws_ssm_parameter.openrouter_api_key.value
      XAI_API_KEY                 = data.aws_ssm_parameter.xai_api_key.value
      JUDGE_MODEL                 = data.aws_ssm_parameter.judge_model.value
      RUNNER_LAMBDA_FUNCTION      = aws_lambda_function.runner.function_name
    }
  }

  tags = {
    Name        = "evalbench-api"
    Environment = var.environment
  }

  depends_on = [
    aws_iam_role_policy.api_lambda_logs,
    aws_iam_role_policy.api_lambda_ssm,
    aws_iam_role_policy.api_lambda_dynamodb,
    aws_iam_role_policy.api_lambda_s3,
    aws_iam_role_policy.api_lambda_ses,
    aws_iam_role_policy.api_lambda_invoke_runner
  ]
}

# Runner Lambda function
resource "aws_lambda_function" "runner" {
  s3_bucket        = aws_s3_object.lambda_deployment.bucket
  s3_key           = aws_s3_object.lambda_deployment.key
  source_code_hash = filebase64sha256("${path.module}/../backend/lambda-deployment.zip")
  function_name    = "evalbench-runner"
  role             = aws_iam_role.runner_lambda_role.arn
  handler          = "evalbench.runner_lambda.handler"
  runtime          = "python3.12"
  timeout          = 900
  # Same 10s non-configurable INIT-phase cap as the api Lambda (see its
  # comment) - runner_lambda.py imports the same heavy evalbench package.
  memory_size      = 2048

  environment {
    variables = {
      DYNAMODB_RUN_STATUS_TABLE = aws_dynamodb_table.run_status.name
      S3_DB_BUCKET              = aws_s3_bucket.db.id
      S3_DB_KEY                 = "evalbench.db"
      OPENAI_API_KEY            = data.aws_ssm_parameter.openai_api_key.value
      ANTHROPIC_API_KEY         = data.aws_ssm_parameter.anthropic_api_key.value
      GEMINI_API_KEY            = data.aws_ssm_parameter.gemini_api_key.value
      OPENROUTER_API_KEY        = data.aws_ssm_parameter.openrouter_api_key.value
      XAI_API_KEY               = data.aws_ssm_parameter.xai_api_key.value
      JUDGE_MODEL               = data.aws_ssm_parameter.judge_model.value
    }
  }

  tags = {
    Name        = "evalbench-runner"
    Environment = var.environment
  }

  depends_on = [
    aws_iam_role_policy.runner_lambda_logs,
    aws_iam_role_policy.runner_lambda_ssm,
    aws_iam_role_policy.runner_lambda_s3,
    aws_iam_role_policy.runner_lambda_dynamodb
  ]
}

# API Gateway HTTP API
resource "aws_apigatewayv2_api" "main" {
  name          = "evalbench-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    allow_headers = ["*"]
  }

  tags = {
    Name        = "evalbench-api"
    Environment = var.environment
  }
}

# API Gateway Integration with Lambda
resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

# API Gateway Route for all requests
resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

# API Gateway Stage (default)
resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true

  tags = {
    Name        = "evalbench-api-default"
    Environment = var.environment
  }
}

# Lambda permission for API Gateway to invoke api Lambda
resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

# SES email identity for sending magic link emails
resource "aws_ses_email_identity" "owner" {
  email = "ahadagal@alumni.iu.edu"
}

# S3 bucket for frontend (static Next.js export)
resource "aws_s3_bucket" "frontend" {
  bucket = "evalbench-frontend-${data.aws_caller_identity.current.account_id}"

  tags = {
    Name        = "evalbench-frontend"
    Environment = var.environment
  }
}

# Block all public access to S3 frontend bucket (CloudFront will access via OAC)
resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# CloudFront origin access control for secure S3 access
resource "aws_cloudfront_origin_access_control" "s3_oac" {
  name                              = "evalbench-s3-oac"
  description                       = "OAC for EvalBench S3 frontend bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# S3 bucket policy to allow CloudFront access
resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowCloudFrontOAC"
        Effect = "Allow"
        Principal = {
          Service = "cloudfront.amazonaws.com"
        }
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.frontend.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = "arn:aws:cloudfront::${data.aws_caller_identity.current.account_id}:distribution/${aws_cloudfront_distribution.main.id}"
          }
        }
      }
    ]
  })
}

# CloudFront distribution for serving the frontend
resource "aws_cloudfront_distribution" "main" {
  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "s3-frontend"
    origin_access_control_id = aws_cloudfront_origin_access_control.s3_oac.id
  }

  enabled = true

  # Default root object
  default_root_object = "index.html"

  # Default cache behavior
  default_cache_behavior {
    allowed_methods  = ["GET", "HEAD"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = "s3-frontend"

    forwarded_values {
      query_string = false

      cookies {
        forward = "none"
      }
    }

    viewer_protocol_policy = "redirect-to-https"
    min_ttl                = 0
    default_ttl            = 3600
    max_ttl                = 86400
  }

  # Ordered cache behaviors are evaluated in order; first match wins
  ordered_cache_behavior {
    path_pattern     = "/index.html"
    allowed_methods  = ["GET", "HEAD"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = "s3-frontend"

    forwarded_values {
      query_string = false

      cookies {
        forward = "none"
      }
    }

    viewer_protocol_policy = "redirect-to-https"
    min_ttl                = 0
    default_ttl            = 0
    max_ttl                = 0
  }

  ordered_cache_behavior {
    path_pattern     = "/run*"
    allowed_methods  = ["GET", "HEAD"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = "s3-frontend"

    forwarded_values {
      query_string = true

      cookies {
        forward = "all"
      }
    }

    viewer_protocol_policy = "redirect-to-https"
    min_ttl                = 0
    default_ttl            = 0
    max_ttl                = 0
  }

  # Handle 404s by serving index.html for client-side routing
  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = {
    Name        = "evalbench-distribution"
    Environment = var.environment
  }
}
