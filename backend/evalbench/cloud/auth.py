"""Magic-link request/verify against DynamoDB + SES, matching the twin blog admin pattern."""

import secrets
import time

import boto3


def request_magic_link(
    *,
    email: str,
    owner_email: str,
    table_name: str,
    base_url: str,
    sender_email: str,
    ttl_seconds: int,
) -> None:
    """Email a one-time sign-in link if email matches the owner; silently no-op otherwise."""
    if email != owner_email:
        return

    token = secrets.token_hex(32)
    expires_at = int(time.time()) + ttl_seconds
    boto3.resource("dynamodb").Table(table_name).put_item(
        Item={"token": token, "expires_at": expires_at}
    )

    link = f"{base_url}?magic={token}"
    boto3.client("ses").send_email(
        Source=sender_email,
        Destination={"ToAddresses": [owner_email]},
        Message={
            "Subject": {"Data": "Your EvalBench sign-in link", "Charset": "UTF-8"},
            "Body": {
                "Text": {
                    "Data": (
                        "Sign in to run a suite:\n\n"
                        f"{link}\n\n"
                        "This link expires in 15 minutes."
                    ),
                    "Charset": "UTF-8",
                },
                "Html": {
                    "Data": (
                        f'<p><a href="{link}">Sign in to run a suite</a></p>'
                        "<p>This link expires in 15 minutes.</p>"
                    ),
                    "Charset": "UTF-8",
                },
            },
        },
    )


def verify_magic_link(*, token: str, table_name: str) -> bool:
    """Return True and consume the token if it exists and is unexpired."""
    table = boto3.resource("dynamodb").Table(table_name)
    item = table.get_item(Key={"token": token}, ConsistentRead=True).get("Item")
    if item is None:
        return False

    is_valid = int(item["expires_at"]) > int(time.time())
    table.delete_item(Key={"token": token})
    return is_valid
