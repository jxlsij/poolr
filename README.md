---
title: Poolr
emoji: 🎯
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# Poolr

Telegram Stars prediction-market MVP.

The implementation currently contains Module 1 from
`prediction_market_mvp_plan.md`: configuration loading, webhook setup, async
database engine creation, local PostgreSQL compose config, and Hugging Face
Docker scaffolding.

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
docker compose up -d
```

## Checks

```bash
python -m compileall bot tests
python -m pytest -q
```

## Hugging Face Notes

This project uses Docker mode and exposes port `7860`. The current `main.py`
starts a health endpoint, registers the Telegram webhook, and mounts an aiogram
webhook handler when environment variables are present. Full bot routes will be
implemented in later modules.
