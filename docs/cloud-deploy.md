# EvalBench cloud deployment — one-time setup

Run these once, before the first `git push` to `main` triggers the automated
deploy (`.github/workflows/deploy.yml`).

## 1. Terraform state bucket

    aws s3 mb s3://evalbench-terraform-state-$(aws sts get-caller-identity --query Account --output text) \
      --region us-east-1

Note the bucket name — it's the `TF_STATE_BUCKET` GitHub secret in step 6.

## 2. Migrate the existing local database

    aws s3 mb s3://evalbench-dev-db-$(aws sts get-caller-identity --query Account --output text) \
      --region us-east-1
    aws s3 cp evalbench.db s3://evalbench-dev-db-$(aws sts get-caller-identity --query Account --output text)/evalbench.db

(If Task 10's Terraform apply already created the `db` bucket with a different
exact name, use `terraform output db_bucket` instead of recomputing it.)

## 3. Verify the sender/owner email in SES

    aws ses verify-email-identity --email-address ahadagal@alumni.iu.edu --region us-east-1

Click the verification link AWS emails you. SES starts in sandbox mode, which
only allows sending to verified addresses — since the magic-link sender and
recipient are the same address, this is sufficient; no production-access
request needed.

## 4. Set the admin bearer token

    openssl rand -hex 32
    aws ssm put-parameter \
      --name /evalbench/dev/admin-token \
      --type SecureString \
      --value "<paste the generated token>" \
      --overwrite \
      --region us-east-1

## 5. Set provider API keys and judge model in SSM

    for name in openai anthropic gemini openrouter xai; do
      aws ssm put-parameter \
        --name "/evalbench/dev/${name}-api-key" \
        --type SecureString \
        --value "<your ${name} key>" \
        --overwrite \
        --region us-east-1
    done

    aws ssm put-parameter \
      --name /evalbench/dev/judge-model \
      --type SecureString \
      --value "anthropic/claude-sonnet-4-5" \
      --overwrite \
      --region us-east-1

These are read by Terraform at `apply` time and injected as `runner` Lambda
environment variables — see design correction 4 in the implementation plan.

## 6. GitHub repository secrets

In the repo's Settings → Secrets and variables → Actions, set:

| Secret | Value |
|---|---|
| `AWS_ROLE_ARN` | ARN of `terraform.aws_iam_role.github_deploy` (`terraform output` after first manual apply, or construct as `arn:aws:iam::<account-id>:role/evalbench-dev-github-deploy`) |
| `AWS_ACCOUNT_ID` | Your 12-digit AWS account ID |
| `AWS_REGION` | `us-east-1` |
| `TF_STATE_BUCKET` | The bucket created in step 1 |

## 7. First deploy

The very first `terraform apply` needs to run once locally (with your own AWS
credentials) before the GitHub Actions OIDC role exists to do it — this is a
bootstrapping chicken-and-egg step:

    cd terraform
    terraform init -backend-config="bucket=<state-bucket-from-step-1>"
    terraform apply -var-file=dev.tfvars

After this, every `git push` to `main` re-applies automatically via GitHub
Actions using the OIDC role this first apply created.
