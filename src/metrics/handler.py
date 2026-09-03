import json
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3

BUCKET = os.environ["BUCKET_NAME"]
TABLE = os.environ["TABLE_NAME"]

s3 = boto3.client("s3")
cw = boto3.client("cloudwatch")
ddb = boto3.resource("dynamodb").Table(TABLE)
lmb = boto3.client("lambda")

COLLECTOR_PREFIXES = [
    "bull14-backend-ModelsCollectorFunction",
    "bull14-backend-PricingCollectorFunction",
    "bull14-backend-ToolsCollectorFunction",
    "bull14-backend-HardwareCollectorFunction",
    "bull14-backend-ChangelogFunction",
    "bull14-backend-AnalyticsFunction",
    "bull14-backend-MetricsFunction",
]


def _resolve_function_names():
    """Resolve actual Lambda function names by matching prefixes."""
    resolved = {}
    paginator = lmb.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page["Functions"]:
            name = fn["FunctionName"]
            for prefix in COLLECTOR_PREFIXES:
                if name.startswith(prefix):
                    resolved[prefix] = name
                    break
    return resolved


def _cw_sum(fn_name, metric, days=7):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    resp = cw.get_metric_statistics(
        Namespace="AWS/Lambda",
        MetricName=metric,
        Dimensions=[{"Name": "FunctionName", "Value": fn_name}],
        StartTime=start,
        EndTime=end,
        Period=86400 * days,
        Statistics=["Sum"],
    )
    points = resp.get("Datapoints", [])
    return int(points[0]["Sum"]) if points else 0


def _cw_avg(fn_name, metric, days=7):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    resp = cw.get_metric_statistics(
        Namespace="AWS/Lambda",
        MetricName=metric,
        Dimensions=[{"Name": "FunctionName", "Value": fn_name}],
        StartTime=start,
        EndTime=end,
        Period=86400 * days,
        Statistics=["Average"],
    )
    points = resp.get("Datapoints", [])
    return round(points[0]["Average"], 2) if points else 0


def lambda_handler(event, context):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # DynamoDB item counts by entity prefix
    table_meta = ddb.meta.client.describe_table(TableName=TABLE)
    item_count = table_meta["Table"].get("ItemCount", 0)

    # Per-function metrics (last 7 days)
    resolved = _resolve_function_names()
    functions = []
    total_invocations = 0
    total_errors = 0
    for prefix in COLLECTOR_PREFIXES:
        fn = resolved.get(prefix, prefix)  # fallback to prefix if not found
        invocations = _cw_sum(fn, "Invocations")
        errors = _cw_sum(fn, "Errors")
        duration_avg = _cw_avg(fn, "Duration")
        total_invocations += invocations
        total_errors += errors
        functions.append({
            "name": prefix.replace("bull14-backend-", "").replace("Function", ""),
            "full_name": fn,
            "invocations_7d": invocations,
            "errors_7d": errors,
            "avg_duration_ms": duration_avg,
            "error_rate": round(errors / invocations, 3) if invocations else 0,
            "resolved": fn != prefix,
        })

    # S3 data file sizes
    data_files = ["models.json", "pricing.json", "tools.json", "hardware.json", "changelog.json"]
    s3_files = []
    for f in data_files:
        try:
            head = s3.head_object(Bucket=BUCKET, Key=f"data/{f}")
            s3_files.append({
                "file": f,
                "size_kb": round(head["ContentLength"] / 1024, 1),
                "last_modified": head["LastModified"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        except Exception:
            s3_files.append({"file": f, "size_kb": 0, "last_modified": None})

    metrics = {
        "_meta": {"generated": today},
        "pipeline": {
            "total_invocations_7d": total_invocations,
            "total_errors_7d": total_errors,
            "error_rate": round(total_errors / total_invocations, 3) if total_invocations else 0,
        },
        "dynamodb": {"item_count": item_count},
        "functions": functions,
        "s3_files": s3_files,
    }

    payload = json.dumps(metrics)
    s3.put_object(Bucket=BUCKET, Key="data/metrics.json", Body=payload, ContentType="application/json")
    print(f"Metrics: {total_invocations} invocations, {total_errors} errors (7d), {item_count} DDB items")
    return {"statusCode": 200, "body": "ok"}
