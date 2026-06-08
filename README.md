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

Telegram Stars prediction-market MVP with a built-in Mini App frontend.

The implementation currently includes the bot/backend foundations plus a
vanilla Mini App frontend mounted at `/app` for the Telegram WebApp flow.

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

Set `MINI_APP_URL` to control where the `/start` message's Open button points.
If it is not set, the button falls back to `WEBHOOK_URL` with `/app` appended.
