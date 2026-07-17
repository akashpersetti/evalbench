"""Cached SSM Parameter Store reads for secrets checked on every request."""

from functools import lru_cache

import boto3


@lru_cache(maxsize=None)
def get_parameter(name: str) -> str:
    client = boto3.client("ssm")
    return client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
