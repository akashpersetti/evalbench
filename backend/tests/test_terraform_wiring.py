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

MAIN_TF = Path(__file__).parent.parent.parent / "terraform" / "main.tf"

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
