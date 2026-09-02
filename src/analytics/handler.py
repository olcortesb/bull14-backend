import json
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

BUCKET = os.environ["BUCKET_NAME"]
TABLE = os.environ["TABLE_NAME"]
CF_DIST = os.environ["CLOUDFRONT_DISTRIBUTION_ID"]

s3 = boto3.client("s3")
cf = boto3.client("cloudfront")
ddb = boto3.resource("dynamodb").Table(TABLE)


def _load_json(key):
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def _serial(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


def _hype_index(models):
    """Score models by downloads + likes + recency of updates."""
    scored = []
    for m in models:
        downloads = m.get("hf_downloads") or 0
        likes = m.get("hf_likes") or 0
        # Normalize: downloads weight 0.6, likes 0.4, cap at 10M / 100k
        dl_score = min(downloads / 10_000_000, 1.0) * 60
        like_score = min(likes / 100_000, 1.0) * 40
        hype = round(dl_score + like_score, 1)
        scored.append({
            "model_id": m.get("id"),
            "name": m.get("name"),
            "provider": m.get("provider"),
            "hype_score": hype,
            "hf_downloads": downloads,
            "hf_likes": likes,
        })
    scored.sort(key=lambda x: x["hype_score"], reverse=True)
    return scored[:20]


def _price_trends(changelog):
    """Extract pricing CHANGE items and compute trend per model."""
    pricing_changes = changelog.get("pricing", [])
    trends = {}
    for c in pricing_changes:
        if c.get("type") != "price_change":
            continue
        mid = c.get("model_id") or c.get("provider", "unknown")
        if mid not in trends:
            trends[mid] = []
        trends[mid].append({
            "date": c.get("date"),
            "field": c.get("field"),
            "prev": float(c["price_prev"]) if c.get("price_prev") else None,
            "now": float(c["price_now"]) if c.get("price_now") else None,
        })
    # Sort each by date desc
    for mid in trends:
        trends[mid].sort(key=lambda x: x["date"] or "", reverse=True)
    return trends


def _breakeven(models, hardware):
    """
    Estimate breakeven: at what request volume does self-hosting beat API pricing?
    Uses cheapest H100 offer vs cheapest API input price per model.
    """
    # Find cheapest H100 per hour across all providers
    h100_price = None
    for provider_data in hardware.values():
        for gpu in provider_data.get("gpus", []):
            if "H100" in gpu.get("gpu_type", ""):
                p = gpu.get("price_per_hour") or 0
                if p and (h100_price is None or p < h100_price):
                    h100_price = p

    if not h100_price:
        return []

    results = []
    for m in models:
        input_price = m.get("pricing", {}).get("input")  # $ per 1M tokens
        if not input_price or input_price <= 0:
            continue
        # Assume H100 processes ~1M tokens/min for a 7B model (rough estimate)
        # tokens_per_hour = 60M, cost_per_1M_via_api = input_price
        tokens_per_hour = 60_000_000
        api_cost_per_hour = (tokens_per_hour / 1_000_000) * input_price
        if api_cost_per_hour <= 0:
            continue
        breakeven_util = h100_price / api_cost_per_hour  # fraction of hour needed
        results.append({
            "model_id": m.get("id"),
            "name": m.get("name"),
            "provider": m.get("provider"),
            "api_input_per_1m": input_price,
            "h100_price_per_hour": h100_price,
            "breakeven_utilization": round(min(breakeven_util, 1.0), 3),
            "self_host_cheaper_above": f"{round(breakeven_util * 100, 1)}% utilization",
        })
    results.sort(key=lambda x: x["breakeven_utilization"])
    return results[:15]


def lambda_handler(event, context):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    raw_models = _load_json("data/models.json") or {}
    models_data = raw_models.get("models", []) if isinstance(raw_models, dict) else raw_models
    hardware_data = _load_json("data/hardware.json") or {}
    changelog_data = _load_json("data/changelog.json") or {}

    analytics = {
        "_meta": {"generated": today},
        "hype_index": _hype_index(models_data),
        "price_trends": _price_trends(changelog_data),
        "breakeven": _breakeven(models_data, hardware_data),
    }

    payload = json.dumps(analytics, default=_serial)
    s3.put_object(Bucket=BUCKET, Key="data/analytics.json", Body=payload, ContentType="application/json")
    try:
        cf.create_invalidation(
            DistributionId=CF_DIST,
            InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/data/analytics.json"]}, "CallerReference": datetime.now(timezone.utc).isoformat()},
        )
    except Exception as e:
        print(f"CF invalidation skipped: {e}")

    print(f"Analytics: {len(analytics['hype_index'])} hype, {len(analytics['breakeven'])} breakeven entries")
    return {"statusCode": 200, "body": "ok"}
