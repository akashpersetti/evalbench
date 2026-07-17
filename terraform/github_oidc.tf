# IAM OIDC providers are an account-wide singleton per issuer URL. This
# account already has one (owned/tagged by a different project) - reference
# it read-only instead of trying to create/own/modify it, so this project
# never touches another project's shared trust anchor.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "github_deploy" {
  name = "${local.name_prefix}-github-deploy"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = data.aws_iam_openid_connect_provider.github.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # GitHub's actual sub claim embeds the immutable numeric owner/repo
          # IDs alongside the names: "repo:OWNER@OWNER_ID/REPO@REPO_ID:ref:..."
          # not the plain "repo:OWNER/REPO:..." form most docs show. A
          # condition on the plain-name form never matches.
          "token.actions.githubusercontent.com:sub" = "repo:${split("/", var.github_repo)[0]}@*/${split("/", var.github_repo)[1]}@*:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_deploy" {
  name = "${local.name_prefix}-github-deploy-policy"
  role = aws_iam_role.github_deploy.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Actual resource names are "evalbench-api"/"evalbench-runner" etc -
        # there's no "-dev-" environment segment in the naming convention
        # despite local.name_prefix suggesting one.
        Effect = "Allow"
        Action = [
          "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration",
          "lambda:GetFunction",
          "lambda:CreateFunction",
          "lambda:DeleteFunction",
          "lambda:AddPermission",
          "lambda:RemovePermission",
          "lambda:InvokeFunction",
          "lambda:TagResource",
          "lambda:ListTags",
          "lambda:ListVersionsByFunction",
          "lambda:GetFunctionCodeSigningConfig",
        ]
        Resource = "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-*"
      },
      {
        Effect = "Allow"
        Action = ["s3:*"]
        Resource = [
          "arn:aws:s3:::${var.project_name}-*",
          "arn:aws:s3:::${var.project_name}-*/*",
        ]
      },
      {
        # Terraform state bucket - separate from the project's own
        # evalbench-* resource buckets above, so needs its own grant.
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${var.tf_state_bucket}",
          "arn:aws:s3:::${var.tf_state_bucket}/*",
        ]
      },
      {
        # Table names are literal ("magic_tokens", "run_status") with no
        # project prefix at all, so they can't be wildcard-matched - list
        # them explicitly instead.
        Effect = "Allow"
        Action = ["dynamodb:*"]
        Resource = [
          aws_dynamodb_table.magic_tokens.arn,
          aws_dynamodb_table.run_status.arn,
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["apigateway:*"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["cloudfront:*"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["iam:*"]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.project_name}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:PutParameter", "ssm:AddTagsToResource"]
        Resource = "arn:aws:ssm:*:${data.aws_caller_identity.current.account_id}:parameter/evalbench/*"
      },
      {
        Effect = "Allow"
        Action = [
          "ses:VerifyEmailIdentity",
          "ses:TagResource",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["route53:ChangeResourceRecordSets"]
        Resource = "arn:aws:route53:::hostedzone/*"
      },
      {
        # Every remaining permission Terraform needs is a read-only
        # Describe/List/Get call made implicitly during plan/refresh
        # (OIDC provider metadata, SSM parameter attrs, Lambda code-signing
        # config, ACM/Route53 cert+zone lookups, SES verification status,
        # etc) - these don't support meaningful resource-level scoping and
        # new ones keep surfacing one at a time, so grant the read verbs
        # broadly across the services this stack touches instead of
        # enumerating each call as it's hit.
        Effect = "Allow"
        Action = [
          "iam:Get*",
          "iam:List*",
          "ssm:Get*",
          "ssm:Describe*",
          "ssm:List*",
          "ses:Get*",
          "ses:List*",
          "route53:Get*",
          "route53:List*",
          "acm:Describe*",
          "acm:List*",
          "acm:Get*",
          "lambda:Get*",
          "lambda:List*",
        ]
        Resource = "*"
      },
    ]
  })
}
