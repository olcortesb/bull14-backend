import json
import os
import time
import urllib.request
from datetime import datetime, timezone

import boto3

BUCKET_NAME = os.environ["BUCKET_NAME"]
TABLE_NAME = os.environ["TABLE_NAME"]

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

# ---------------------------------------------------------------------------
# Curated base — fixed metadata for v1 models
# ---------------------------------------------------------------------------
MODELS_BASE = [
    {
        "id": "gpt-4o",
        "name": "GPT-4o",
        "provider": "openai",
        "access": "api-only",
        "parameters": None,
        "context_window": 128000,
        "max_output": 16384,
        "modalities": ["text", "code", "vision", "audio"],
        "license": "proprietary",
        "commercial_use": True,
        "fine_tunable": False,
        "on_premise": False,
        "quantizations": [],
        "status": "active",
        "released": "2024-05-13",
        "hf_id": None,
    },
    {
        "id": "claude-sonnet-4",
        "name": "Claude Sonnet 4",
        "provider": "anthropic",
        "access": "api-only",
        "parameters": None,
        "context_window": 200000,
        "max_output": 64000,
        "modalities": ["text", "code", "vision"],
        "license": "proprietary",
        "commercial_use": True,
        "fine_tunable": False,
        "on_premise": False,
        "quantizations": [],
        "status": "active",
        "released": "2025-07-15",
        "hf_id": None,
    },
    {
        "id": "gemini-2.0-flash",
        "name": "Gemini 2.0 Flash",
        "provider": "google",
        "access": "api-only",
        "parameters": None,
        "context_window": 1048576,
        "max_output": 8192,
        "modalities": ["text", "code", "vision", "audio"],
        "license": "proprietary",
        "commercial_use": True,
        "fine_tunable": False,
        "on_premise": False,
        "quantizations": [],
        "status": "active",
        "released": "2025-02-05",
        "hf_id": None,
    },
    {
        "id": "llama-3.1-8b",
        "name": "Llama 3.1 8B",
        "provider": "meta",
        "access": "both",
        "parameters": "8B",
        "context_window": 128000,
        "max_output": 4096,
        "modalities": ["text", "code"],
        "license": "Llama 3.1",
        "commercial_use": True,
        "fine_tunable": True,
        "on_premise": True,
        "quantizations": ["GGUF", "AWQ", "GPTQ"],
        "status": "active",
        "released": "2024-07-23",
        "hf_id": "meta-llama/Meta-Llama-3.1-8B",
    },
    {
        "id": "llama-3.1-70b",
        "name": "Llama 3.1 70B",
        "provider": "meta",
        "access": "both",
        "parameters": "70B",
        "context_window": 128000,
        "max_output": 4096,
        "modalities": ["text", "code"],
        "license": "Llama 3.1",
        "commercial_use": True,
        "fine_tunable": True,
        "on_premise": True,
        "quantizations": ["GGUF", "AWQ", "GPTQ"],
        "status": "active",
        "released": "2024-07-23",
        "hf_id": "meta-llama/Meta-Llama-3.1-70B",
    },
    {
        "id": "mistral-7b-v0.3",
        "name": "Mistral 7B v0.3",
        "provider": "mistral",
        "access": "both",
        "parameters": "7B",
        "context_window": 32000,
        "max_output": 4096,
        "modalities": ["text", "code"],
        "license": "Apache-2.0",
        "commercial_use": True,
        "fine_tunable": True,
        "on_premise": True,
        "quantizations": ["GGUF", "AWQ", "GPTQ"],
        "status": "active",
        "released": "2024-05-22",
        "hf_id": "mistralai/Mistral-7B-v0.3",
    },
    {
        "id": "deepseek-r1",
        "name": "DeepSeek R1",
        "provider": "deepseek",
        "access": "both",
        "parameters": "671B",
        "context_window": 128000,
        "max_output": 8192,
        "modalities": ["text", "code"],
        "license": "MIT",
        "commercial_use": True,
        "fine_tunable": True,
        "on_premise": True,
        "quantizations": ["GGUF", "AWQ"],
        "status": "active",
        "released": "2025-01-20",
        "hf_id": "deepseek-ai/DeepSeek-R1",
    },
    {
        "id": "qwen2.5-72b",
        "name": "Qwen 2.5 72B",
        "provider": "alibaba",
        "access": "both",
        "parameters": "72B",
        "context_window": 128000,
        "max_output": 8192,
        "modalities": ["text", "code"],
        "license": "Qwen",
        "commercial_use": True,
        "fine_tunable": True,
        "on_premise": True,
        "quantizations": ["GGUF", "AWQ", "GPTQ"],
        "status": "active",
        "released": "2024-09-18",
        "hf_id": "Qwen/Qwen2.5-72B",
    },
    {
        "id": "phi-4",
        "name": "Phi-4",
        "provider": "microsoft",
        "access": "both",
        "parameters": "14B",
        "context_window": 16384,
        "max_output": 4096,
        "modalities": ["text", "code"],
        "license": "MIT",
        "commercial_use": True,
        "fine_tunable": True,
        "on_premise": True,
        "quantizations": ["GGUF", "AWQ"],
        "status": "active",
        "released": "2024-12-12",
        "hf_id": "microsoft/phi-4",
    },
    {
        "id": "gemma-3-27b",
        "name": "Gemma 3 27B",
        "provider": "google",
        "access": "both",
        "parameters": "27B",
        "context_window": 128000,
        "max_output": 8192,
        "modalities": ["text", "code", "vision"],
        "license": "Gemma",
        "commercial_use": True,
        "fine_tunable": True,
        "on_premise": True,
        "quantizations": ["GGUF", "AWQ"],
        "status": "active",
        "released": "2025-03-12",
        "hf_id": "google/gemma-3-27b-it",
    },
]


# ---------------------------------------------------------------------------
# Live data fetchers
# ---------------------------------------------------------------------------

def _fetch_url(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "bull14-collector/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _hf_live(hf_id):
    """Fetch downloads and likes from HuggingFace public API."""
    try:
        url = f"https://huggingface.co/api/models/{hf_id}?fields=downloads,likes,lastModified"
        data = _fetch_url(url)
        return {
            "hf_downloads": data.get("downloads", 0),
            "hf_likes": data.get("likes", 0),
            "hf_last_modified": data.get("lastModified"),
        }
    except Exception as e:
        print(f"HF API error for {hf_id}: {e}")
        return {"hf_downloads": None, "hf_likes": None, "hf_last_modified": None}


def _openai_available_ids():
    """Check OpenAI status page (no auth)."""
    try:
        _fetch_url("https://status.openai.com/api/v2/status.json")
        return {"gpt-4o"}  # if status page responds, API is up
    except Exception:
        return set()


def _mistral_available_ids():
    """Check Mistral status page (no auth)."""
    try:
        _fetch_url("https://mistralstatus.com/api/v2/status.json")
        return {"mistral-7b"}  # if status page responds, API is up
    except Exception:
        return set()


def _google_available_ids():
    """Check Google AI status (no auth)."""
    try:
        _fetch_url("https://status.cloud.google.com/incidents.json")
        return {"gemini-2.0-flash"}  # if status page responds, API is up
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# DynamoDB persistence + change detection
# ---------------------------------------------------------------------------

def _get_previous(model_id):
    try:
        resp = table.get_item(Key={"pk": f"model#{model_id}", "sk": "MODEL"})
        return resp.get("Item")
    except Exception:
        return None


def _put_model(model):
    table.put_item(Item={
        "pk": f"model#{model['id']}",
        "sk": "MODEL",
        "gsi1pk": f"MODEL#{model['provider']}",
        "gsi1sk": f"{model['status']}#{model['id']}",
        **model,
    })


def _put_change(model_id, change_type, detail, old_value=None, new_value=None):
    import hashlib
    suffix = hashlib.md5(f"{model_id}{change_type}{detail}".encode()).hexdigest()[:4]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    table.put_item(Item={
        "pk": f"model#{model_id}",
        "sk": f"CHANGE#{date}#{suffix}",
        "gsi1pk": "CHANGE#model",
        "gsi1sk": f"{date}#{model_id}",
        "type": change_type,
        "detail": detail,
        "old_value": str(old_value) if old_value is not None else None,
        "new_value": str(new_value) if new_value is not None else None,
        "date": date,
    })


def _detect_changes(model_id, previous, current):
    if not previous:
        _put_change(model_id, "model_added", f"{current['name']} added to bull14")
        return

    if previous.get("status") != current.get("status"):
        _put_change(model_id, "model_updated",
                    f"Status changed: {previous['status']} → {current['status']}",
                    previous["status"], current["status"])

    prev_downloads = previous.get("hf_downloads")
    curr_downloads = current.get("hf_downloads")
    if prev_downloads and curr_downloads and curr_downloads > int(prev_downloads) * 1.5:
        _put_change(model_id, "model_updated",
                    f"HF downloads spike: {prev_downloads:,} → {curr_downloads:,}",
                    prev_downloads, curr_downloads)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    now = datetime.now(timezone.utc).isoformat()

    # Fetch live availability from public APIs
    openai_ids = _openai_available_ids()
    mistral_ids = _mistral_available_ids()
    google_ids = _google_available_ids()

    models = []
    for base in MODELS_BASE:
        model = dict(base)

        # Enrich with HF live data
        if model["hf_id"]:
            live = _hf_live(model["hf_id"])
            model.update(live)
            time.sleep(0.5)  # gentle rate limiting
        else:
            model.update({"hf_downloads": None, "hf_likes": None, "hf_last_modified": None})

        # Confirm availability from public APIs
        if model["provider"] == "openai":
            model["api_confirmed"] = "gpt-4o" in openai_ids
        elif model["provider"] == "mistral":
            model["api_confirmed"] = any("mistral-7b" in i for i in mistral_ids)
        elif model["provider"] == "google" and model["access"] == "api-only":
            model["api_confirmed"] = any("gemini-2.0-flash" in i for i in google_ids)
        else:
            model["api_confirmed"] = None  # open-weight, no API to confirm

        # Persist + detect changes
        previous = _get_previous(model["id"])
        _detect_changes(model["id"], previous, model)
        _put_model(model)

        models.append(model)
        print(f"processed {model['id']}: downloads={model.get('hf_downloads')}, api_confirmed={model.get('api_confirmed')}")

    # Write models.json to S3
    payload = {
        "lastUpdated": now,
        "total": len(models),
        "models": models,
    }
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key="data/models.json",
        Body=json.dumps(payload, default=str),
        ContentType="application/json",
    )

    print(f"models.json written — {len(models)} models")
    return {"statusCode": 200, "body": f"{len(models)} models processed"}
