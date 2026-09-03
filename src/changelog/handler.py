import json
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

BUCKET = os.environ["BUCKET_NAME"]
TABLE = os.environ["TABLE_NAME"]
EXPECTED_KEYS = os.environ.get("EXPECTED_KEYS", "").split(",")

s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb").Table(TABLE)

DOMAINS = ["models", "pricing", "tools", "hardware"]
WINDOW_DAYS = 90


def _query_changes(domain):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    resp = ddb.query(
        IndexName="gsi1",
        KeyConditionExpression=Key("gsi1pk").eq(f"CHANGE#{domain}") & Key("gsi1sk").gte(cutoff),
        ScanIndexForward=False,
    )
    return resp.get("Items", [])


def _serial(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


def lambda_handler(event, context):
    # Check if all expected S3 keys exist before generating changelog
    if EXPECTED_KEYS and EXPECTED_KEYS[0]:
        for key in EXPECTED_KEYS:
            key = key.strip()
            if not key:
                continue
            try:
                s3.head_object(Bucket=BUCKET, Key=key)
            except Exception:
                print(f"Skipping changelog — {key} not ready yet")
                return {"statusCode": 202, "body": f"waiting for {key}"}

    changelog = {}
    total = 0
    for domain in DOMAINS:
        items = _query_changes(domain)
        entries = []
        for item in items:
            entry = {k: v for k, v in item.items() if k not in ("pk", "sk", "gsi1pk", "gsi1sk")}
            entries.append(entry)
        changelog[domain] = entries
        total += len(entries)
        print(f"  {domain}: {len(entries)} changes")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    changelog["_meta"] = {"generated": today, "window_days": WINDOW_DAYS, "total": total}

    payload = json.dumps(changelog, default=_serial)
    s3.put_object(Bucket=BUCKET, Key="data/changelog.json", Body=payload, ContentType="application/json")
    print(f"Changelog: {total} total changes across {len(DOMAINS)} domains")
    return {"statusCode": 200, "body": f"{total} changes"}
