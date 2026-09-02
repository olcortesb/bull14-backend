# bull14-backend

Data pipeline for [bull14](https://github.com/olcortesb/bull14) — collects AI model metadata, pricing, tools and GPU offers daily.

## How it works

7 Lambda functions triggered daily via EventBridge Schedules:

```
06:00 UTC ┌─ ModelsCollector    → Hugging Face API + provider APIs
          ├─ PricingCollector   → Scraping pricing pages per provider
          ├─ ToolsCollector     → GitHub Releases + PyPI + npm
          └─ HardwareCollector  → vast.ai + RunPod + Lambda Labs
                │
                │ S3 ObjectCreated → EventBridge → trigger
                ▼
          ChangelogGenerator   → CHANGE items from DynamoDB → changelog.json
                │
                ▼
          AnalyticsGenerator   → hype index, price tracker, breakeven → analytics.json
                │
                ▼
          MetricsFunction      → pipeline metrics → metrics.json
```

Each collector writes JSON to S3 → served via CloudFront to the frontend.

## Data sources

| Collector | Sources |
|-----------|---------|
| ModelsCollector | Hugging Face API, OpenAI API, Mistral API, Google API |
| PricingCollector | Scraping: openai.com, anthropic.com, mistral.ai, cohere.com + AWS Price List API |
| ToolsCollector | GitHub Releases API, PyPI API, npm registry |
| HardwareCollector | vast.ai API, RunPod GraphQL API, Lambda Labs API |

## Stack

- AWS SAM
- Python 3.12, arm64
- DynamoDB single-table (PK/SK/GSI1)
- S3 + CloudFront
- EventBridge Schedules + S3 event chain

## Prerequisites

- AWS SAM CLI
- AWS credentials with appropriate permissions

## Deploy

```bash
cp samconfig.toml samconfig.local.toml
# Edit samconfig.local.toml with your values
sam build && sam deploy --config-file samconfig.local.toml
```

## Cost

~$1.50/month (within AWS free tier for Lambda, DynamoDB, S3, EventBridge).

## Related

- [bull14](https://github.com/olcortesb/bull14) — frontend
- [s3rv3rl3ss-backend](https://github.com/olcortesb/s3rv3rl3ss-backend) — same architecture pattern for serverless cloud services

## License

MIT
