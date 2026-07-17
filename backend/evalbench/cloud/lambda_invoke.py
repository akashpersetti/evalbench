"""Fire-and-forget async invocation of the runner Lambda."""

import json

import boto3

from evalbench.models import RunConfig


def invoke_runner_async(function_name: str, run_id: str, config: RunConfig) -> None:
    boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"run_id": run_id, "config": config.model_dump()}).encode(),
    )
