"""Shared pytest fixtures for the EvalBench test suite."""

import pytest


@pytest.fixture(autouse=True)
def _dummy_aws_environment(monkeypatch):
    """Ensure boto3 never resolves real credentials or region during tests."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
