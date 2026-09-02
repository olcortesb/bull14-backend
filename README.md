# bull14-backend

Data pipeline for bull14 — collects AI model metadata, pricing, tools and GPU cloud pricing daily.

Live data at [bull14.olcortesb.com](https://bull14.olcortesb.com)

## How it works

1. **EventBridge Schedules** trigger 4 collector Lambdas daily at 06:00 UTC in parallel
2. Each collector writes a JSON file to S3 and persists state + change items to DynamoDB
3. **S3 ObjectCreated → EventBridge → ChangelogFunction** — generates `changelog.json` once all 4 files are present
4. **AnalyticsFunction** runs at 06:30 UTC — hype index, price tracker, breakeven calculator
5. **MetricsFunction** runs at 06:45 UTC — pipeline health metrics
6. **AWS Amplify** serves the frontend from CloudFront

```
06:00 UTC ┌─ ModelsCollector    → HF API + curated base  → data/models.json
          ├─ PricingCollector   → provider pricing pages → data/pricing.json
          ├─ ToolsCollector     → GitHub + PyPI + npm    → data/tools.json
          └─ HardwareCollector  → vast.ai + RunPod       → data/hardware.json
                │
                │ S3 ObjectCreated → EventBridge
                ▼
          ChangelogFunction    → DynamoDB CHANGE items  → data/changelog.json
                │
06:30 UTC       ▼
          AnalyticsFunction    → hype index, breakeven  → data/analytics.json
                │
06:45 UTC       ▼
          MetricsFunction      → CloudWatch + DynamoDB  → data/metrics.json
```

## Data sources

| Collector | Sources |
|-----------|---------|
| ModelsCollector | HuggingFace Hub API (downloads, likes) + curated metadata base |
| PricingCollector | Provider pricing pages (static scraping) |
| ToolsCollector | GitHub Releases API, PyPI API, npm registry |
| HardwareCollector | vast.ai API, RunPod GraphQL API, Lambda Labs API |

## Output files (S3 + CloudFront)

| File | Description |
|------|-------------|
| `data/models.json` | 10 curated models with metadata and live HF stats |
| `data/pricing.json` | Pricing per token by provider and model |
| `data/tools.json` | Framework versions and release dates |
| `data/hardware.json` | GPU cloud pricing by platform and GPU type |
| `data/changelog.json` | Detected changes (model added, price dropped, new version) |
| `data/analytics.json` | Hype index, price tracker, model velocity, breakeven |
| `data/metrics.json` | Pipeline health and Lambda invocation metrics |
| `data/deployments/{model_id}.json` | Per-model deployment options (loaded on-demand) |

## Models (v1 — 10 curated)

| Model | Provider | Access | Params |
|-------|----------|--------|--------|
| GPT-4o | OpenAI | api-only | — |
| Claude Sonnet 4 | Anthropic | api-only | — |
| Gemini 2.0 Flash | Google | api-only | — |
| Llama 3.1 8B | Meta | both | 8B |
| Llama 3.1 70B | Meta | both | 70B |
| Mistral 7B v0.3 | Mistral | both | 7B |
| DeepSeek R1 | DeepSeek | both | 671B |
| Qwen 2.5 72B | Alibaba | both | 72B |
| Phi-4 | Microsoft | both | 14B |
| Gemma 3 27B | Google | both | 27B |

## Persistence (DynamoDB single-table)

Same PK/SK/GSI1 pattern as s3rv3rl3ss.

```
PK                      SK                        GSI1PK              GSI1SK
model#llama-3.1-70b     MODEL                     MODEL#meta          active#llama-3.1-70b
model#llama-3.1-70b     DEPLOYMENT#together-ai    DEPLOYMENT#api      0.18#llama-3.1-70b
model#llama-3.1-70b     CHANGE#2026-09-02#a3f2    CHANGE#model        2026-09-02#llama-3.1-70b
tool#langchain          TOOL                      TOOL#orchestration  langchain
gpu#vast-ai             GPU_OFFER#H100            GPU#vast-ai         2.49#H100
```

## Stack

- AWS SAM, Python 3.12, arm64
- DynamoDB on-demand, single-table
- S3 (versioned) + CloudFront (OAC, HTTPS-only)
- EventBridge Schedules

## Prerequisites

- AWS SAM CLI
- AWS profile with deploy permissions

## Deploy

```bash
cp samconfig.toml samconfig.local.toml
# edit samconfig.local.toml if needed

sam build && sam deploy --config-file samconfig.local.toml
```

## Invoke manually

```bash
export AWS_PROFILE=<your-profile>

# List function names
aws lambda list-functions --region us-east-1 \
  --query 'Functions[?starts_with(FunctionName, `bull14`)].FunctionName' \
  --output text

# Invoke a function
aws lambda invoke \
  --function-name <function-name> \
  --region us-east-1 \
  --log-type Tail \
  --query 'LogResult' \
  --output text \
  /tmp/out.json | base64 -d
```

## Security

- S3 bucket: public access blocked, accessible only via CloudFront OAC
- CloudFront: HTTPS-only, CORS restricted to `bull14.olcortesb.com` (+ localhost for dev)
- IAM: least privilege per function — read-only functions use `DynamoDBReadPolicy`
- `cloudfront:CreateInvalidation` scoped to the specific distribution ARN
- No secrets in code or environment variables

## Cost

~$1.50/month total (same profile as s3rv3rl3ss).

| Service | Cost |
|---------|------|
| Lambda (7 functions, arm64) | $0.00 (free tier) |
| DynamoDB (on-demand) | $0.00 (free tier) |
| S3 (versioned) | $0.00 (free tier) |
| EventBridge | $0.00 (free tier) |
| CloudFront | $0.00 (free tier) |
| Secrets Manager | $0.00 (no secrets yet) |

## License

MIT
