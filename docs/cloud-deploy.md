# EvalBench cloud deployment — one-time setup

Run these once, before the first `git push` to `main` triggers the automated
deploy (`.github/workflows/deploy.yml`).

## 1. Terraform state bucket

    aws s3 mb s3://evalbench-terraform-state-$(aws sts get-caller-identity --query Account --output text) \
      --region us-east-1

Note the bucket name — it's the `TF_STATE_BUCKET` GitHub secret in step 6.

## 2. Verify the sender/owner email in SES

    aws ses verify-email-identity --email-address ahadagal@alumni.iu.edu --region us-east-1

Click the verification link AWS emails you. SES starts in sandbox mode, which
only allows sending to verified addresses — since the magic-link sender and
recipient are the same address, this is sufficient; no production-access
request needed.

## 3. Set the admin bearer token

    openssl rand -hex 32
    aws ssm put-parameter \
      --name /evalbench/admin_token \
      --type SecureString \
      --value "<paste the generated token>" \
      --overwrite \
      --region us-east-1

## 4. Set provider API keys and judge model in SSM

Paths must match `terraform/api_keys.tf` exactly (no `/dev/` segment,
underscores not hyphens):

    for name in openai anthropic gemini openrouter xai; do
      aws ssm put-parameter \
        --name "/evalbench/${name}_api_key" \
        --type SecureString \
        --value "<your ${name} key>" \
        --overwrite \
        --region us-east-1
    done

    aws ssm put-parameter \
      --name /evalbench/judge_model \
      --type SecureString \
      --value "anthropic/claude-sonnet-4-5" \
      --overwrite \
      --region us-east-1

These are read by Terraform at `apply` time (`terraform/api_keys.tf`'s
`data "aws_ssm_parameter"` blocks) and injected as real values into both
Lambdas' environment variables — see `terraform/main.tf`.

## 5. GitHub repository secrets

In the repo's Settings → Secrets and variables → Actions, set:

| Secret | Value |
|---|---|
| `AWS_ROLE_ARN` | ARN of `terraform.aws_iam_role.github_deploy` (`terraform output` after first manual apply, or construct as `arn:aws:iam::<account-id>:role/evalbench-dev-github-deploy`) |
| `AWS_ACCOUNT_ID` | Your 12-digit AWS account ID |
| `AWS_REGION` | `us-east-1` |
| `TF_STATE_BUCKET` | The bucket created in step 1 |

## 6. Build the Lambda deployment package

    uv run python backend/deploy.py

Requires Docker running locally. Produces `backend/lambda-deployment.zip`,
which both the `api` and `runner` Lambda resources in `terraform/main.tf`
reference.

## 7. First deploy

The very first `terraform apply` needs to run once locally (with your own AWS
credentials) before the GitHub Actions OIDC role exists to do it — this is a
bootstrapping chicken-and-egg step:

    cd terraform
    terraform init -backend-config="bucket=<state-bucket-from-step-1>"
    terraform apply -var-file=dev.tfvars

After this, every `git push` to `main` re-applies automatically via GitHub
Actions using the OIDC role this first apply created.

## 8. Migrate the existing local database

Terraform creates the `db` bucket (`aws_s3_bucket.db`) as part of step 7 —
don't create it manually beforehand, or `terraform apply` will fail trying to
create a bucket that already exists. Once step 7 has run:

    aws s3 cp evalbench.db "s3://$(terraform output -raw s3_db_bucket)/evalbench.db"

Run this from the repo root (where `evalbench.db` lives), with `terraform`
pointed at the `terraform/` directory for the output lookup — e.g.
`terraform -chdir=terraform output -raw s3_db_bucket` if running from the
repo root directly.
