"""DynamoDB-backed progress tracking for asynchronous suite runs."""

from typing import Any

import boto3


def _table(table_name: str):
    return boto3.resource("dynamodb").Table(table_name)


def create_status(table_name: str, run_id: str, total: int) -> None:
    _table(table_name).put_item(
        Item={"run_id": run_id, "status": "pending", "completed": 0, "total": total}
    )


def set_running(table_name: str, run_id: str) -> None:
    _table(table_name).update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": "running"},
    )


def increment_completed(table_name: str, run_id: str) -> None:
    _table(table_name).update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET completed = completed + :one",
        ExpressionAttributeValues={":one": 1},
    )


def set_done(table_name: str, run_id: str) -> None:
    _table(table_name).update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": "done"},
    )


def set_error(table_name: str, run_id: str, message: str) -> None:
    _table(table_name).update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET #s = :status, #e = :error",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={":status": "error", ":error": message},
    )


def get_status(table_name: str, run_id: str) -> dict[str, Any] | None:
    return _table(table_name).get_item(Key={"run_id": run_id}).get("Item")
