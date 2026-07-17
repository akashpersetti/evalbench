"""Regression guard: Terraform Lambda env vars must name-match Settings fields.

The cloud deployment previously shipped with Terraform injecting SSM parameter
*names* (e.g. SSM_ADMIN_TOKEN_PARAM) instead of actual secret *values* under
the names Settings actually reads (ADMIN_TOKEN) — every credential silently
failed to reach the running Lambdas. This test does not (and cannot, without
a real AWS account) verify Terraform's runtime behavior; it only guards
against the env var *names* drifting apart again, which is the exact failure
mode that shipped undetected.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
TERRAFORM_DIR = REPO_ROOT / "terraform"
MAIN_TF = TERRAFORM_DIR / "main.tf"
OUTPUTS_TF = TERRAFORM_DIR / "outputs.tf"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
DEPLOY_PY = REPO_ROOT / "backend" / "deploy.py"

REQUIRED_ENV_VAR_NAMES = {
    "REQUIRE_AUTH",
    "OWNER_EMAIL",
    "ADMIN_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "XAI_API_KEY",
    "JUDGE_MODEL",
    "RUNNER_LAMBDA_FUNCTION",
    "S3_DB_BUCKET",
    "S3_DB_KEY",
    "DYNAMODB_MAGIC_TOKENS_TABLE",
    "DYNAMODB_RUN_STATUS_TABLE",
}


def test_main_tf_lambda_env_vars_match_settings_field_names() -> None:
    content = MAIN_TF.read_text()

    missing = [
        name
        for name in REQUIRED_ENV_VAR_NAMES
        if not re.search(rf"\b{name}\b\s*=", content)
    ]
    assert not missing, (
        f"terraform/main.tf is missing env var(s) {missing} under the exact "
        "name(s) evalbench.config.Settings expects (case-sensitive, no "
        "SSM_*_PARAM-style renaming) — Settings will silently see None/unset "
        "for these."
    )


def test_main_tf_uses_ssm_value_not_name_for_secrets() -> None:
    content = MAIN_TF.read_text()

    # A previous version injected `data.aws_ssm_parameter.X.name` (the
    # parameter's path) instead of `.value` (the decrypted secret) — every
    # credential silently became the SSM path string instead of the key.
    leaked_names = re.findall(r"data\.aws_ssm_parameter\.\w+\.name", content)
    assert not leaked_names, (
        f"terraform/main.tf reads {leaked_names} — SSM parameter *names* "
        "assigned directly as Lambda env var values. Use `.value` instead."
    )


def test_main_tf_owner_email_matches_confirmed_address() -> None:
    content = MAIN_TF.read_text()

    assert '"ahadagal@iu.edu"' not in content, (
        "terraform/main.tf sets the wrong owner email — the confirmed "
        "magic-link owner is ahadagal@alumni.iu.edu, not ahadagal@iu.edu."
    )
    assert content.count('"ahadagal@alumni.iu.edu"') >= 2, (
        "expected ahadagal@alumni.iu.edu on both the SES identity and the "
        "api Lambda's OWNER_EMAIL env var"
    )


def test_main_tf_lambda_functions_reference_the_shared_deploy_zip() -> None:
    """backend/deploy.py builds exactly one zip: backend/lambda-deployment.zip.

    A previous version had the api/runner Lambda resources point at
    api_lambda.zip/runner_lambda.zip, filenames nothing in the repo ever
    produces — terraform apply would fail with a file-not-found error before
    ever reaching AWS.
    """
    content = MAIN_TF.read_text()

    assert content.count("backend/lambda-deployment.zip") >= 2, (
        "expected both aws_lambda_function.api and aws_lambda_function.runner "
        "to reference ${path.module}/../backend/lambda-deployment.zip — the "
        "single zip backend/deploy.py actually builds"
    )
    assert "api_lambda.zip" not in content
    assert "runner_lambda.zip" not in content


def test_deploy_workflow_output_names_exist_in_outputs_tf() -> None:
    """Every `terraform output -raw <name>` in CI must name a real output."""
    outputs_content = OUTPUTS_TF.read_text()
    defined_outputs = set(re.findall(r'output "(\w+)"', outputs_content))

    workflow_content = DEPLOY_WORKFLOW.read_text()
    referenced_outputs = set(
        re.findall(r"terraform output -raw (\w+)", workflow_content)
    )

    missing = referenced_outputs - defined_outputs
    assert not missing, (
        f"deploy.yml references terraform output(s) {missing} that aren't "
        f"defined in outputs.tf (defined: {sorted(defined_outputs)}) — the "
        "'Set frontend build outputs' CI step would fail"
    )


def test_deploy_py_packages_the_data_directory() -> None:
    """Every suite (structured/rag/latency_cost) resolves its dataset via

    Path(__file__).resolve().parents[2] / "data" / <suite> - a sibling of
    the evalbench/ package, not inside it. deploy.py previously copied only
    evalbench/ into the zip, so RagSuite() (eagerly constructed at import
    time in registry.py) crashed every cold start with FileNotFoundError,
    taking down every route in the api Lambda.
    """
    content = DEPLOY_PY.read_text()
    assert re.search(r'BACKEND_DIR\s*/\s*"data"', content), (
        "backend/deploy.py must copy backend/data/ into the deployment zip "
        "alongside evalbench/ — suites load their datasets from there"
    )
