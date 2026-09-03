import json
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

BUCKET = os.environ["BUCKET_NAME"]
TABLE = os.environ["TABLE_NAME"]

s3 = boto3.client("s3")
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
    """Score open-weight models by downloads + likes. Excludes api-only."""
    scored = []
    for m in models:
        if m.get("access") == "api-only":
            continue
        downloads = m.get("hf_downloads") or 0
        likes = m.get("hf_likes") or 0
        dl_score = min(downloads / 10_000_000, 1.0) * 60
        like_score = min(likes / 100_000, 1.0) * 40
        hype = round(dl_score + like_score, 1)
        scored.append({
            "model_id": m.get("id"),
            "name": m.get("name"),
            "provider": m.get("provider"),
            "access": m.get("access"),
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
        if c.get("type") not in ("price_change", "price_dropped", "price_increased"):
            continue
        # Key by provider/model from gsi1sk: date#provider#model
        gsi1sk = c.get("gsi1sk", "")
        parts = gsi1sk.split("#")
        mid = "#".join(parts[1:]) if len(parts) >= 2 else c.get("pk", "unknown").replace("pricing#", "")
        old_val = c.get("old_value")
        new_val = c.get("new_value")
        try:
            prev = float(old_val) if old_val not in (None, "", "None") else None
            now = float(new_val) if new_val not in (None, "", "None") else None
        except (ValueError, TypeError):
            prev, now = None, None
        if prev is None or now is None:
            continue
        if mid not in trends:
            trends[mid] = []
        trends[mid].append({
            "date": c.get("date"),
            "prev": prev,
            "now": now,
            "pct": round((now - prev) / prev * 100, 1) if prev else None,
            "type": c.get("type"),
        })
    for mid in trends:
        trends[mid].sort(key=lambda x: x["date"] or "", reverse=True)
    return trends


def _breakeven(models, pricing_data, hardware):
    """
    Breakeven: tokens/month at which self-hosting beats API pricing.
    Formula: (h100_price * 730) / (avg_api_price_per_1M / 1_000_000)
    """
    # Find cheapest H100 per hour
    h100_price = None
    for provider_data in hardware.values():
        for gpu in provider_data.get("gpus", []):
            if "H100" in gpu.get("gpu_type", ""):
                p = gpu.get("price_per_hour") or 0
                if p and (h100_price is None or p < h100_price):
                    h100_price = p
    if not h100_price:
        return []

    # Build cheapest API input price per model id from pricing.json
    api_prices = {}
    for provider in (pricing_data.get("providers") or []):
        for m in provider.get("models", []):
            mid = m.get("id", "")
            inp = (m.get("pricing") or {}).get("standard", {}).get("input_per_1m")
            if inp and inp > 0:
                if mid not in api_prices or inp < api_prices[mid]:
                    api_prices[mid] = inp

    results = []
    for m in models:
        if m.get("access") not in ("both", "open-weight"):
            continue
        model_id = m.get("id", "")
        # Try exact match or partial match
        api_price = api_prices.get(model_id)
        if not api_price:
            for pid, price in api_prices.items():
                if model_id.replace("-", "") in pid.replace("-", "").replace(".", ""):
                    api_price = price
                    break
        if not api_price:
            continue

        monthly_cost = h100_price * 730
        breakeven_tokens = (monthly_cost / api_price) * 1_000_000
        results.append({
            "model_id": model_id,
            "name": m.get("name"),
            "provider": m.get("provider"),
            "api_input_per_1m": api_price,
            "h100_price_per_hour": h100_price,
            "monthly_gpu_cost": round(monthly_cost, 2),
            "breakeven_tokens_month": round(breakeven_tokens),
            "breakeven_tokens_readable": _fmt_tokens(breakeven_tokens),
        })
    results.sort(key=lambda x: x["breakeven_tokens_month"])
    return results[:15]


def _fmt_tokens(n):
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.0f}M"
    return f"{n/1_000:.0f}K"


def lambda_handler(event, context):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    raw_models = _load_json("data/models.json") or {}
    models_data = raw_models.get("models", []) if isinstance(raw_models, dict) else raw_models
    hardware_data = _load_json("data/hardware.json") or {}
    pricing_data = _load_json("data/pricing.json") or {}
    changelog_data = _load_json("data/changelog.json") or {}

    analytics = {
        "_meta": {"generated": today},
        "hype_index": _hype_index(models_data),
        "price_trends": _price_trends(changelog_data),
        "breakeven": _breakeven(models_data, pricing_data, hardware_data),
    }

    payload = json.dumps(analytics, default=_serial)
    s3.put_object(Bucket=BUCKET, Key="data/analytics.json", Body=payload, ContentType="application/json")
    print(f"Analytics: {len(analytics['hype_index'])} hype, {len(analytics['breakeven'])} breakeven entries")
    return {"statusCode": 200, "body": "ok"}
