import json
import os
import time
import hashlib
import urllib.request
import yaml
from datetime import datetime, timezone
from pathlib import Path

import boto3

BUCKET_NAME = os.environ["BUCKET_NAME"]
TABLE_NAME = os.environ["TABLE_NAME"]

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

FALLBACK_PATH = Path(__file__).parent / "pricing_fallback.yaml"
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"

# Providers served by OpenRouter (primary source)
OPENROUTER_PROVIDERS = {"openai", "anthropic", "mistral", "meta-llama", "google", "cohere",
                        "deepseek", "qwen", "microsoft", "x-ai", "nvidia"}


# ---------------------------------------------------------------------------
# OpenRouter ingestion
# ---------------------------------------------------------------------------

def _fetch_openrouter():
    req = urllib.request.Request(OPENROUTER_URL, headers={"User-Agent": "bull14-collector/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _to_1m(value):
    """Convert OpenRouter per-token price to $/1M tokens."""
    if value is None:
        return None
    f = float(value)
    return round(f * 1_000_000, 4) if f > 0 else 0


def _parse_openrouter(raw_models):
    """
    Parse OpenRouter response into normalized provider/model structure.
    Separates :batch variants into the batch pricing field of the base model.
    Returns dict: {provider_id: {model_id: pricing_entry}}
    """
    base = {}    # provider_id -> model_id -> entry
    batches = {} # provider_id -> model_id -> batch pricing

    for m in raw_models:
        mid = m["id"]           # e.g. "openai/gpt-4o" or "openai/gpt-4o:batch"
        p = m.get("pricing", {})

        is_batch = mid.endswith(":batch")
        clean_id = mid.replace(":batch", "")
        parts = clean_id.split("/", 1)
        if len(parts) != 2:
            continue

        provider_id, model_id = parts[0], parts[1]

        entry = {
            "id": model_id,
            "context_length": m.get("context_length"),
            "max_completion_tokens": (m.get("top_provider") or {}).get("max_completion_tokens"),
            "source": "openrouter",
            "pricing": {
                "standard": {
                    "input_per_1m": _to_1m(p.get("prompt")),
                    "output_per_1m": _to_1m(p.get("completion")),
                },
                "cached_input_per_1m": _to_1m(p.get("input_cache_read")),
                "cache_write_per_1m": _to_1m(p.get("input_cache_write")),
                "batch": None,  # filled below
                "image_per_image": _to_1m(p.get("image")),
                "audio_input_per_min": _to_1m(p.get("audio")),
                "audio_output_per_min": _to_1m(p.get("audio_output")),
                "internal_reasoning_per_1m": _to_1m(p.get("internal_reasoning")),
                "web_search_per_query": _to_1m(p.get("web_search")),
            },
        }

        if is_batch:
            batches.setdefault(provider_id, {})[model_id] = {
                "input_per_1m": _to_1m(p.get("prompt")),
                "output_per_1m": _to_1m(p.get("completion")),
            }
        else:
            base.setdefault(provider_id, {})[model_id] = entry

    # Merge batch pricing into base models
    for provider_id, models in batches.items():
        for model_id, batch_pricing in models.items():
            if provider_id in base and model_id in base[provider_id]:
                base[provider_id][model_id]["pricing"]["batch"] = batch_pricing

    return base


# ---------------------------------------------------------------------------
# Fallback YAML ingestion
# ---------------------------------------------------------------------------

def _load_fallback():
    """Load fallback YAML and return same structure as _parse_openrouter."""
    with open(FALLBACK_PATH) as f:
        data = yaml.safe_load(f)

    result = {}
    for provider in data.get("providers", []):
        pid = provider["id"]
        result[pid] = {}
        for m in provider.get("models", []):
            mid = m["id"]
            result[pid][mid] = {
                "id": mid,
                "context_length": m.get("context_length"),
                "max_completion_tokens": None,
                "source": "fallback",
                "pricing": {
                    "standard": {
                        "input_per_1m": m.get("input_per_1m"),
                        "output_per_1m": m.get("output_per_1m"),
                    },
                    "cached_input_per_1m": m.get("cached_input_per_1m"),
                    "cache_write_per_1m": m.get("cache_write_per_1m"),
                    "batch": m.get("batch"),
                    "image_per_image": m.get("image_per_image"),
                    "audio_input_per_min": m.get("audio_input_per_min"),
                    "audio_output_per_min": m.get("audio_output_per_min"),
                    "internal_reasoning_per_1m": m.get("internal_reasoning_per_1m"),
                    "web_search_per_query": m.get("web_search_per_query"),
                    "context_tier": m.get("context_tier"),
                },
            }
    return result


# ---------------------------------------------------------------------------
# DynamoDB persistence + change detection
# ---------------------------------------------------------------------------

def _get_previous(provider_id, model_id):
    try:
        resp = table.get_item(Key={
            "pk": f"pricing#{provider_id}",
            "sk": f"PRICING#{model_id}",
        })
        return resp.get("Item")
    except Exception:
        return None


def _put_pricing(provider_id, model_id, entry, provider_url=None):
    table.put_item(Item={
        "pk": f"pricing#{provider_id}",
        "sk": f"PRICING#{model_id}",
        "gsi1pk": f"PRICING#{provider_id}",
        "gsi1sk": model_id,
        "provider_id": provider_id,
        "model_id": model_id,
        "source": entry.get("source"),
        "input": str(entry["pricing"]["standard"]["input_per_1m"] or ""),
        "output": str(entry["pricing"]["standard"]["output_per_1m"] or ""),
        "cached_input": str(entry["pricing"].get("cached_input_per_1m") or ""),
        "updated": datetime.now(timezone.utc).isoformat(),
    })


def _put_change(provider_id, model_id, change_type, detail, old_value=None, new_value=None, url=None):
    suffix = hashlib.md5(f"{provider_id}{model_id}{change_type}{detail}".encode()).hexdigest()[:4]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    item = {
        "pk": f"pricing#{provider_id}",
        "sk": f"CHANGE#{date}#{suffix}",
        "gsi1pk": "CHANGE#pricing",
        "gsi1sk": f"{date}#{provider_id}#{model_id}",
        "type": change_type,
        "detail": detail,
        "old_value": str(old_value) if old_value is not None else None,
        "new_value": str(new_value) if new_value is not None else None,
        "date": date,
        "url": url,
    }
    table.put_item(Item=item)


def _detect_changes(provider_id, model_id, previous, entry, provider_url):
    inp = entry["pricing"]["standard"]["input_per_1m"]
    out = entry["pricing"]["standard"]["output_per_1m"]

    if not previous:
        inp_str = f"${inp}" if inp is not None else "free"
        out_str = f"${out}" if out is not None else "free"
        _put_change(provider_id, model_id, "pricing_added",
                    f"{provider_id}/{model_id} added — input: {inp_str}, output: {out_str}/1M tokens",
                    url=provider_url)
        return

    prev_input = float(previous["input"]) if previous.get("input") else None
    prev_output = float(previous["output"]) if previous.get("output") else None

    if prev_input is not None and inp is not None and prev_input != inp:
        change_type = "price_dropped" if inp < prev_input else "price_changed"
        _put_change(provider_id, model_id, change_type,
                    f"{provider_id}/{model_id} input: ${prev_input} → ${inp}/1M tokens",
                    old_value=prev_input, new_value=inp, url=provider_url)

    if prev_output is not None and out is not None and prev_output != out:
        change_type = "price_dropped" if out < prev_output else "price_changed"
        _put_change(provider_id, model_id, change_type,
                    f"{provider_id}/{model_id} output: ${prev_output} → ${out}/1M tokens",
                    old_value=prev_output, new_value=out, url=provider_url)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    now = datetime.now(timezone.utc).isoformat()

    # 1. Fetch OpenRouter (primary)
    print("Fetching OpenRouter API...")
    try:
        raw = _fetch_openrouter()
        or_data = _parse_openrouter(raw.get("data", []))
        print(f"OpenRouter: {sum(len(v) for v in or_data.values())} models from {len(or_data)} providers")
    except Exception as e:
        print(f"OpenRouter fetch failed: {e} — using fallback only")
        or_data = {}

    # 2. Load fallback YAML
    fallback_data = _load_fallback()
    print(f"Fallback: {sum(len(v) for v in fallback_data.values())} models from {len(fallback_data)} providers")

    # 3. Merge: fallback providers fill gaps not in OpenRouter
    merged = dict(or_data)
    for pid, models in fallback_data.items():
        if pid not in merged:
            merged[pid] = models
        else:
            # Add models from fallback that OpenRouter doesn't have
            for mid, entry in models.items():
                if mid not in merged[pid]:
                    merged[pid][mid] = entry

    # 4. Build output + persist to DynamoDB
    output_providers = []
    total_models = 0

    for provider_id, models in sorted(merged.items()):
        provider_models = []
        for model_id, entry in sorted(models.items()):
            previous = _get_previous(provider_id, model_id)
            _detect_changes(provider_id, model_id, previous, entry, None)
            _put_pricing(provider_id, model_id, entry)
            provider_models.append(entry)
            total_models += 1

        output_providers.append({
            "id": provider_id,
            "models": provider_models,
        })
        print(f"  {provider_id}: {len(provider_models)} models")

    # 5. Write pricing.json to S3
    payload = {
        "lastUpdated": now,
        "totalProviders": len(output_providers),
        "totalModels": total_models,
        "source": "openrouter+fallback",
        "providers": output_providers,
    }
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key="data/pricing.json",
        Body=json.dumps(payload, default=str),
        ContentType="application/json",
    )

    print(f"pricing.json written — {len(output_providers)} providers, {total_models} models")
    return {"statusCode": 200, "body": f"{len(output_providers)} providers, {total_models} models"}
