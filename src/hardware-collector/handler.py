import json
import os
import hashlib
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import yaml

BUCKET = os.environ["BUCKET_NAME"]
TABLE = os.environ["TABLE_NAME"]

s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb").Table(TABLE)

BASE_DIR = os.path.dirname(__file__)


def _load_base():
    with open(os.path.join(BASE_DIR, "hardware_base.yaml")) as f:
        return yaml.safe_load(f)


def _runpod_live():
    query = '{"query":"{ gpuTypes { id displayName memoryInGb securePrice communityPrice lowestPrice { minimumBidPrice uninterruptablePrice } } }"}'
    req = urllib.request.Request(
        "https://api.runpod.io/graphql",
        data=query.encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; bull14-bot/1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    gpus = []
    for t in data["data"]["gpuTypes"]:
        # Skip entries with invalid gpu_type or vram
        if not t.get("displayName") or not t.get("memoryInGb"):
            continue
        secure = t.get("securePrice") or 0
        community = t.get("communityPrice") or 0
        lowest = t.get("lowestPrice") or {}
        spot = lowest.get("minimumBidPrice")
        gpus.append({
            "gpu_type": t["displayName"],
            "gpu_id": t["id"],
            "vram_gb": t["memoryInGb"],
            "price_per_hour": secure or community,
            "price_spot": spot,
            "price_community": community,
            "availability": "available" if (secure or community) else "unavailable",
            "interconnect": None,
        })
    return gpus


def _vastai_live():
    url = "https://console.vast.ai/api/v0/bundles/"
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.loads(r.read())

    # Aggregate by gpu_name: min price, count offers
    agg = {}
    for o in data.get("offers", []):
        name = o.get("gpu_name", "")
        if not name or not o.get("gpu_ram"):
            continue
        dph = o.get("dph_total") or 0
        num = o.get("num_gpus") or 1
        price_per_gpu = round(dph / num, 4) if num else dph
        interruptible = not o.get("rented", False)
        if name not in agg or price_per_gpu < agg[name]["price_per_hour"]:
            agg[name] = {
                "gpu_type": name,
                "vram_gb": round((o.get("gpu_ram") or 0) / 1024),
                "price_per_hour": price_per_gpu,
                "reliability": round(o.get("reliability2") or 0, 3),
                "interruptible": interruptible,
                "availability": "available",
                "interconnect": None,
            }
    return list(agg.values())


def _detect_changes(provider_id, gpus_now):
    changes = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for gpu in gpus_now:
        gpu_key = gpu["gpu_type"].replace(" ", "_").lower()
        pk = f"gpu#{provider_id}"
        sk = f"GPU_OFFER#{gpu_key}"
        resp = ddb.get_item(Key={"pk": pk, "sk": sk})
        prev = resp.get("Item")
        price_now = str(gpu["price_per_hour"])
        if prev:
            price_prev = str(prev.get("price_per_hour", ""))
            if price_prev and price_prev != price_now:
                try:
                    prev_f = float(price_prev)
                    now_f = float(price_now)
                    change_type = "price_dropped" if now_f < prev_f else "price_increased"
                except ValueError:
                    change_type = "price_change"
                h = hashlib.md5(f"{provider_id}{gpu_key}{today}".encode()).hexdigest()[:6]
                changes.append({
                    "pk": f"gpu#{provider_id}",
                    "sk": f"CHANGE#{today}#{h}",
                    "gsi1pk": f"CHANGE#hardware",
                    "gsi1sk": f"{today}#{provider_id}",
                    "type": change_type,
                    "provider": provider_id,
                    "gpu_type": gpu["gpu_type"],
                    "price_prev": Decimal(price_prev),
                    "price_now": Decimal(price_now),
                    "date": today,
                })
    return changes


def _save_provider(provider_id, gpus):
    for gpu in gpus:
        gpu_key = gpu["gpu_type"].replace(" ", "_").lower()
        item = {
            "pk": f"gpu#{provider_id}",
            "sk": f"GPU_OFFER#{gpu_key}",
            "gsi1pk": f"GPU#{gpu['gpu_type']}",
            "gsi1sk": provider_id,
            "provider": provider_id,
            **{k: Decimal(str(v)) if isinstance(v, float) else v
               for k, v in gpu.items()},
        }
        ddb.put_item(Item=item)


def lambda_handler(event, context):
    base = _load_base()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = {}

    # Static providers from YAML
    for p in base["providers"]:
        pid = p["id"]
        gpus = []
        for g in p["gpus"]:
            gpus.append({
                "gpu_type": g["gpu_type"],
                "vram_gb": g["vram_gb"],
                "price_per_hour": g["price_per_hour"],
                "availability": g.get("availability", "unknown"),
                "interconnect": g.get("interconnect"),
                "price_spot": None,
            })
        changes = _detect_changes(pid, gpus)
        _save_provider(pid, gpus)
        for c in changes:
            ddb.put_item(Item=c)
        result[pid] = {
            "name": p["name"],
            "category": p["category"],
            "url": p["url"],
            "notes": p.get("notes", ""),
            "source": "static",
            "updated": today,
            "gpus": gpus,
        }

    # RunPod live
    try:
        runpod_gpus = _runpod_live()
        changes = _detect_changes("runpod", runpod_gpus)
        _save_provider("runpod", runpod_gpus)
        for c in changes:
            ddb.put_item(Item=c)
        result["runpod"] = {
            "name": "RunPod",
            "category": "developer",
            "url": "https://www.runpod.io/gpu-instance/pricing",
            "notes": "Secure cloud + community cloud. Spot available.",
            "source": "live",
            "updated": today,
            "gpus": runpod_gpus,
        }
    except Exception as e:
        print(f"RunPod error: {e}")

    # Vast.ai live
    try:
        vastai_gpus = _vastai_live()
        changes = _detect_changes("vastai", vastai_gpus)
        _save_provider("vastai", vastai_gpus)
        for c in changes:
            ddb.put_item(Item=c)
        result["vastai"] = {
            "name": "Vast.ai",
            "category": "marketplace",
            "url": "https://vast.ai/pricing",
            "notes": "P2P marketplace. Spot/bid pricing. Min price per GPU shown.",
            "source": "live",
            "updated": today,
            "gpus": vastai_gpus,
        }
    except Exception as e:
        print(f"Vast.ai error: {e}")

    # Serialize Decimal for JSON
    def _serial(obj):
        if isinstance(obj, Decimal):
            return float(obj)
        raise TypeError

    payload = json.dumps(result, default=_serial)
    s3.put_object(Bucket=BUCKET, Key="data/hardware.json", Body=payload, ContentType="application/json")
    total_gpus = sum(len(v["gpus"]) for v in result.values())
    print(f"Hardware: {len(result)} providers, {total_gpus} GPU offers")
    return {"statusCode": 200, "body": f"{len(result)} providers, {total_gpus} GPU offers"}
