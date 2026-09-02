import json
import os
import time
import hashlib
import urllib.request
import urllib.error
import yaml
from datetime import datetime, timezone
from pathlib import Path

import boto3

BUCKET_NAME = os.environ["BUCKET_NAME"]
TABLE_NAME = os.environ["TABLE_NAME"]
CLOUDFRONT_DISTRIBUTION_ID = os.environ["CLOUDFRONT_DISTRIBUTION_ID"]
GITHUB_TOKEN_SECRET_ARN = os.environ.get("GITHUB_TOKEN_SECRET_ARN", "")

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
cloudfront = boto3.client("cloudfront")
secretsmanager = boto3.client("secretsmanager")
table = dynamodb.Table(TABLE_NAME)

BASE_PATH = Path(__file__).parent / "tools_base.yaml"
GITHUB_API = "https://api.github.com"


def _get_github_token():
    if not GITHUB_TOKEN_SECRET_ARN:
        return None
    try:
        resp = secretsmanager.get_secret_value(SecretId=GITHUB_TOKEN_SECRET_ARN)
        return json.loads(resp["SecretString"])["token"]
    except Exception as e:
        print(f"Could not fetch GitHub token: {e}")
        return None


GITHUB_TOKEN = _get_github_token()
PYPI_API = "https://pypi.org/pypi"
NPM_API = "https://registry.npmjs.org"
NPM_DOWNLOADS_API = "https://api.npmjs.org/downloads/point/last-month"


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _fetch(url, timeout=10):
    headers = {"User-Agent": "bull14-collector/1.0", "Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} — {url}")
        return None
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Live data fetchers
# ---------------------------------------------------------------------------

def _github_live(repo, use_tags=False):
    """Fetch repo metadata + latest release/tag from GitHub public API."""
    repo_data = _fetch(f"{GITHUB_API}/repos/{repo}")
    if not repo_data:
        return {}

    version = None
    release_date = None
    changelog_url = None

    if use_tags:
        tags_data = _fetch(f"{GITHUB_API}/repos/{repo}/tags?per_page=1")
        if tags_data and isinstance(tags_data, list) and tags_data:
            version = tags_data[0].get("name", "").lstrip("v")
            changelog_url = f"https://github.com/{repo}/releases/tag/{tags_data[0].get('name')}"
    else:
        release_data = _fetch(f"{GITHUB_API}/repos/{repo}/releases/latest")
        if release_data and not release_data.get("message"):
            tag = release_data.get("tag_name", "")
            version = tag.lstrip("v")
            release_date = (release_data.get("published_at") or "")[:10]
            changelog_url = release_data.get("html_url")

    commits_data = _fetch(f"{GITHUB_API}/repos/{repo}/commits?per_page=1")
    last_commit = None
    if commits_data and isinstance(commits_data, list) and commits_data:
        last_commit = (commits_data[0].get("commit", {}).get("committer", {}).get("date") or "")[:10]

    return {
        "stars": repo_data.get("stargazers_count"),
        "forks": repo_data.get("forks_count"),
        "open_issues": repo_data.get("open_issues_count"),
        "license": (repo_data.get("license") or {}).get("spdx_id"),
        "version": version,
        "released": release_date,
        "last_commit": last_commit,
        "changelog_url": changelog_url,
    }


def _pypi_live(package):
    """Fetch latest version and monthly downloads from PyPI."""
    data = _fetch(f"{PYPI_API}/{package}/json")
    if not data:
        return {}

    version = data.get("info", {}).get("version")

    # Monthly downloads from pypistats (public, no auth)
    stats = _fetch(f"https://pypistats.org/api/packages/{package}/recent?period=month")
    downloads = None
    if stats and "data" in stats:
        downloads = stats["data"].get("last_month")

    return {
        "version": version,
        "pypi_downloads_month": downloads,
    }


def _npm_live(package):
    """Fetch latest version and monthly downloads from npm."""
    pkg_name = package.lstrip("@").replace("/", "%2F") if package.startswith("@") else package
    data = _fetch(f"{NPM_API}/{package}/latest")
    if not data:
        return {}

    version = data.get("version")

    downloads_data = _fetch(f"{NPM_DOWNLOADS_API}/{package}")
    downloads = downloads_data.get("downloads") if downloads_data else None

    return {
        "version": version,
        "npm_downloads_month": downloads,
    }


# ---------------------------------------------------------------------------
# Activity badge
# ---------------------------------------------------------------------------

def _activity_badge(last_commit):
    if not last_commit:
        return "unknown"
    try:
        delta = (datetime.now(timezone.utc).date() -
                 datetime.strptime(last_commit, "%Y-%m-%d").date()).days
        if delta <= 7:
            return "active"
        if delta <= 30:
            return "slow"
        return "inactive"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# DynamoDB persistence + change detection
# ---------------------------------------------------------------------------

def _get_previous(tool_id):
    try:
        resp = table.get_item(Key={"pk": f"tool#{tool_id}", "sk": "TOOL"})
        return resp.get("Item")
    except Exception:
        return None


def _put_tool(tool_id, entry):
    table.put_item(Item={
        "pk": f"tool#{tool_id}",
        "sk": "TOOL",
        "gsi1pk": f"TOOL#{entry['category']}",
        "gsi1sk": tool_id,
        **{k: v for k, v in entry.items() if v is not None},
    })


def _put_change(tool_id, change_type, detail, old_value=None, new_value=None, url=None):
    suffix = hashlib.md5(f"{tool_id}{change_type}{detail}".encode()).hexdigest()[:4]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    table.put_item(Item={
        "pk": f"tool#{tool_id}",
        "sk": f"CHANGE#{date}#{suffix}",
        "gsi1pk": "CHANGE#tool",
        "gsi1sk": f"{date}#{tool_id}",
        "type": change_type,
        "detail": detail,
        "old_value": str(old_value) if old_value is not None else None,
        "new_value": str(new_value) if new_value is not None else None,
        "date": date,
        "url": url,
    })


def _detect_changes(tool_id, previous, entry):
    if not previous:
        _put_change(tool_id, "tool_added",
                    f"{entry['name']} added to bull14",
                    url=f"https://github.com/{entry.get('github', '')}")
        return

    prev_version = previous.get("version")
    curr_version = entry.get("version")
    if prev_version and curr_version and prev_version != curr_version:
        _put_change(tool_id, "new_version",
                    f"{entry['name']} {prev_version} → {curr_version}",
                    old_value=prev_version, new_value=curr_version,
                    url=entry.get("changelog_url"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    now = datetime.now(timezone.utc).isoformat()

    with open(BASE_PATH) as f:
        base_tools = yaml.safe_load(f)["tools"]

    tools = []
    for base in base_tools:
        tid = base["id"]
        entry = {
            "id": tid,
            "name": base["name"],
            "category": base["category"],
            "description": base.get("description"),
            "github": base.get("github"),
            "pypi": base.get("pypi"),
            "npm": base.get("npm"),
            "capabilities": base.get("capabilities", {}),
            # live fields — filled below
            "version": None,
            "released": None,
            "last_commit": None,
            "stars": None,
            "forks": None,
            "open_issues": None,
            "license": None,
            "changelog_url": None,
            "pypi_downloads_month": None,
            "npm_downloads_month": None,
            "activity": "unknown",
        }

        # GitHub live data
        if base.get("github"):
            gh = _github_live(base["github"], use_tags=base.get("github_use_tags", False))
            entry.update(gh)
            time.sleep(0.3)  # GitHub rate limit: 60 req/hour unauthenticated

        # PyPI live data (version takes precedence over GitHub tag if available)
        if base.get("pypi"):
            py = _pypi_live(base["pypi"])
            if py.get("version"):
                entry["version"] = py["version"]
            entry["pypi_downloads_month"] = py.get("pypi_downloads_month")
            time.sleep(0.2)

        # npm live data
        if base.get("npm") and not base.get("pypi"):
            npm = _npm_live(base["npm"])
            if npm.get("version"):
                entry["version"] = npm["version"]
            entry["npm_downloads_month"] = npm.get("npm_downloads_month")
            time.sleep(0.2)

        entry["activity"] = _activity_badge(entry.get("last_commit"))

        # Persist + detect changes
        previous = _get_previous(tid)
        _detect_changes(tid, previous, entry)
        _put_tool(tid, entry)

        tools.append(entry)
        print(f"processed {tid}: v{entry['version']}, stars={entry['stars']}, activity={entry['activity']}")

    # Write tools.json to S3
    payload = {
        "lastUpdated": now,
        "total": len(tools),
        "tools": tools,
    }
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key="data/tools.json",
        Body=json.dumps(payload, default=str),
        ContentType="application/json",
    )

    # CloudFront invalidation
    cloudfront.create_invalidation(
        DistributionId=CLOUDFRONT_DISTRIBUTION_ID,
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/data/tools.json"]},
            "CallerReference": str(int(time.time())),
        },
    )

    print(f"tools.json written — {len(tools)} tools")
    return {"statusCode": 200, "body": f"{len(tools)} tools processed"}
